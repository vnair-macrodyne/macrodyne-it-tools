"""
update_employee_master.py
--------------------------
Reads the Kronos CSV export and updates Employee Master in CRD.
Resolves M365 UPNs via Entra ID using a multi-tier matching strategy.

UPN resolution priority:
  1. Kronos username prefix → exact Entra UPN prefix match
  2. Kronos email → confirmed exists in Entra
  3. Entra display name search → "First Last" single match
  4. Entra last name search → disambiguate with first name/initial
  5. No match → blank (never constructed)

Run:
  py update_employee_master.py EmployeeInformation_Aug2026.csv

Requires: pip install msal requests
"""

import sys
import re
import csv
import os
import msal
import requests
from datetime import datetime

# ── CONFIG ────────────────────────────────────────────────────────────────────

TENANT_ID  = "4c9a50a1-c27f-4044-8025-b59b5b804d16"
CLIENT_ID  = "6bfa3adb-1f7d-44e4-8cb9-38be4a107549"
CACHE_PATH = r"C:\Users\vnair\.msalcache_survey"

CRD_SITE   = "https://macrodyne.sharepoint.com/sites/CorporateReferenceData"
LIST_NAME  = "Employee Master"

SP_SCOPES    = [
    "https://macrodyne.sharepoint.com/AllSites.Write",
    "https://macrodyne.sharepoint.com/AllSites.Read",
]
GRAPH_SCOPES = [
    "https://graph.microsoft.com/User.ReadBasic.All",
    "https://graph.microsoft.com/User.Read",
]

SKIP_EMP_NOS = {"JOBCOST", "Reports"}
SKIP_NAMES   = {"Reports Reports", "JOBCOST JOBCOST"}

# ── AUTH ──────────────────────────────────────────────────────────────────────

def get_token(scopes):
    cache = msal.SerializableTokenCache()
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            cache.deserialize(f.read())

    app = msal.PublicClientApplication(
        CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        token_cache=cache,
    )

    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(scopes, account=accounts[0])
        if result and "access_token" in result:
            _save_cache(cache)
            return result["access_token"]

    result = app.acquire_token_interactive(scopes=scopes)
    if "access_token" not in result:
        raise RuntimeError(f"Auth failed: {result.get('error_description')}")
    _save_cache(cache)
    return result["access_token"]

def _save_cache(cache):
    if cache.has_state_changed:
        with open(CACHE_PATH, "w") as f:
            f.write(cache.serialize())

# ── ENTRA ID ──────────────────────────────────────────────────────────────────

def get_entra_users(token):
    """
    Pull all M365 users from Entra.
    Returns:
      by_prefix  : dict  lowercase_username_prefix → upn
      by_upn     : set   all upns lowercase
      by_lastname: dict  lowercase_lastname → [list of user dicts]
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    url = (
        "https://graph.microsoft.com/v1.0/users"
        "?$select=userPrincipalName,displayName,givenName,surname"
        "&$top=999"
    )
    by_prefix   = {}
    by_upn      = set()
    by_lastname = {}

    while url:
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        for user in data.get("value", []):
            upn = user.get("userPrincipalName", "")
            if not upn or "#EXT#" in upn:
                continue

            upn_lower = upn.lower()
            prefix    = upn_lower.split("@")[0]
            surname   = (user.get("surname") or "").strip().lower()
            given     = (user.get("givenName") or "").strip().lower()
            display   = (user.get("displayName") or "").strip().lower()

            by_upn.add(upn_lower)
            by_prefix[prefix] = upn

            # Index by last name
            if surname:
                by_lastname.setdefault(surname, []).append({
                    "upn":     upn,
                    "given":   given,
                    "display": display,
                })
            # Also index by last word of display name (catches compound surnames)
            last_word = display.split()[-1] if display else ""
            if last_word and last_word != surname:
                by_lastname.setdefault(last_word, []).append({
                    "upn":     upn,
                    "given":   given,
                    "display": display,
                })

        url = data.get("@odata.nextLink")

    return by_prefix, by_upn, by_lastname


def resolve_upn(row, by_prefix, by_upn, by_lastname):
    """
    Multi-tier UPN resolution. Returns (upn, source, note).
    source: 'prefix' | 'email' | 'display' | 'lastname' | 'none'
    """
    username  = (row.get("Username") or "").strip().lower()
    email     = (row.get("Primary Email") or "").strip().lower()
    first     = (row.get("First Name") or "").strip()
    last      = (row.get("Last Name") or "").strip()
    first_low = first.lower()
    last_low  = last.lower()

    # 1. Username prefix → exact Entra match
    if username and username in by_prefix:
        return by_prefix[username], "prefix", ""

    # 2. Kronos email → confirmed in Entra
    if email and email in by_upn:
        return email, "email", ""

    # 3. Display name → "First Last" exact match (case-insensitive)
    full_display = f"{first_low} {last_low}"
    # Search all users for display name match
    display_matches = []
    for candidates in by_lastname.values():
        for u in candidates:
            if u["display"] == full_display:
                if u["upn"] not in [m["upn"] for m in display_matches]:
                    display_matches.append(u)

    if len(display_matches) == 1:
        return display_matches[0]["upn"], "display", ""
    if len(display_matches) > 1:
        # Ambiguous display name — fall through to last name search
        pass

    # 4. Last name search → disambiguate with first name or initial
    candidates = by_lastname.get(last_low, [])

    # Deduplicate
    seen = set()
    unique_candidates = []
    for c in candidates:
        if c["upn"] not in seen:
            seen.add(c["upn"])
            unique_candidates.append(c)
    candidates = unique_candidates

    if len(candidates) == 0:
        # Try compound last name (e.g. "De Oliveira" → search "oliveira")
        last_parts = last_low.split()
        for part in last_parts[1:]:  # skip first word of compound
            sub = by_lastname.get(part, [])
            if sub:
                candidates = [c for c in sub if c["upn"] not in seen]
                break

    if len(candidates) == 1:
        return candidates[0]["upn"], "lastname", ""

    if len(candidates) > 1:
        # Disambiguate by first name exact match
        first_matches = [c for c in candidates if c["given"] == first_low]
        if len(first_matches) == 1:
            return first_matches[0]["upn"], "lastname+first", ""

        # Disambiguate by first initial
        initial = first_low[0] if first_low else ""
        initial_matches = [c for c in candidates if c["given"].startswith(initial)]
        if len(initial_matches) == 1:
            return initial_matches[0]["upn"], "lastname+initial", ""

        # Still ambiguous
        note = f"ambiguous: {len(candidates)} matches for surname '{last}'"
        return "", "none", note

    # 5. No match → blank
    return "", "none", "not found in Entra"


# ── SHAREPOINT ────────────────────────────────────────────────────────────────

def get_request_digest(token):
    r = requests.post(
        f"{CRD_SITE}/_api/contextinfo",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json;odata=verbose",
        },
        timeout=30
    )
    r.raise_for_status()
    return r.json()["d"]["GetContextWebInformation"]["FormDigestValue"]

def get_entity_type(token):
    r = requests.get(
        f"{CRD_SITE}/_api/web/lists/getbytitle('{LIST_NAME}')"
        f"?$select=ListItemEntityTypeFullName",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json;odata=verbose",
        },
        timeout=30
    )
    r.raise_for_status()
    return r.json()["d"]["ListItemEntityTypeFullName"]

def get_all_employees(token):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json;odata=verbose",
    }
    url = (
        f"{CRD_SITE}/_api/web/lists/getbytitle('{LIST_NAME}')/items"
        f"?$top=500"
        f"&$select=ID,Employee_x0020_Number,Full_x0020_Name,"
        f"M365_x0020_UPN,Department_x0020_Code,Department_x0020_Name,"
        f"ManagerEmpNo,Reporting_x0020_Manager,"
        f"Employment_x0020_Status,Date_x0020_Hired,"
        f"Pay_x0020_Type,Kronos_x0020_Username,Kronos_x0020_Email"
    )
    rows = {}
    while url:
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()["d"]
        for item in data["results"]:
            emp_no = (item.get("Employee_x0020_Number") or "").strip()
            if emp_no:
                rows[emp_no] = item
        url = data.get("__next")
    return rows

def sp_update(token, digest, entity_type, item_id, fields):
    headers = {
        "Authorization":   f"Bearer {token}",
        "Accept":          "application/json;odata=verbose",
        "Content-Type":    "application/json;odata=verbose",
        "IF-MATCH":        "*",
        "X-HTTP-Method":   "MERGE",
        "X-RequestDigest": digest,
    }
    body = {"__metadata": {"type": entity_type}, **fields}
    r = requests.post(
        f"{CRD_SITE}/_api/web/lists/getbytitle('{LIST_NAME}')/items({item_id})",
        headers=headers, json=body, timeout=30
    )
    if not r.ok:
        raise RuntimeError(f"Update {item_id} failed: {r.status_code} {r.text[:300]}")

def sp_create(token, digest, entity_type, fields):
    headers = {
        "Authorization":   f"Bearer {token}",
        "Accept":          "application/json;odata=verbose",
        "Content-Type":    "application/json;odata=verbose",
        "X-RequestDigest": digest,
    }
    body = {"__metadata": {"type": entity_type}, **fields}
    r = requests.post(
        f"{CRD_SITE}/_api/web/lists/getbytitle('{LIST_NAME}')/items",
        headers=headers, json=body, timeout=30
    )
    if not r.ok:
        raise RuntimeError(f"Create failed: {r.status_code} {r.text[:300]}")
    return r.json()["d"]["ID"]

# ── KRONOS PARSING ────────────────────────────────────────────────────────────

def extract_manager_emp_no(manager_str):
    """Pull the employee number out of Kronos's 'Name (12345)' format."""
    if not manager_str:
        return ""
    m = re.search(r'\((\w+)\)$', manager_str.strip())
    if not m:
        return ""
    emp_no = m.group(1)
    if emp_no.lower() in ("reports", "jobcost"):
        return ""
    return emp_no


def extract_manager_name(manager_str):
    """Pull just the name out of Kronos's 'Name (12345)' format.

    The employee number is stored separately in ManagerEmpNo, so the display
    column holds the name alone to match how the rest of the list reads.
    """
    if not manager_str:
        return ""
    name = re.sub(r'\s*\(\w+\)$', '', manager_str.strip())
    # Kronos uses placeholder rows for its own system accounts.
    if name.lower() in ("reports reports", "jobcost jobcost"):
        return ""
    return name

def parse_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%m/%d/%Y").strftime("%Y-%m-%dT00:00:00Z")
    except ValueError:
        return None

def parse_department(dept_str):
    if not dept_str:
        return "", ""
    parts = dept_str.split(" - ", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return dept_str.strip(), dept_str.strip()

def parse_kronos_row(row, by_prefix, by_upn, by_lastname):
    emp_no   = (row.get("Employee Id") or "").strip()
    first    = (row.get("First Name") or "").strip()
    last     = (row.get("Last Name") or "").strip()
    username = (row.get("Username") or "").strip()
    status   = (row.get("Employee Status") or "Active").strip()
    pay_type = (row.get("Pay Type") or "").strip()
    dept_str = (row.get("Default Department") or "").strip()
    date_str = (row.get("Date Hired") or "").strip()
    mgr1     = (row.get("Manager 1 Name") or "").strip()
    email    = (row.get("Primary Email") or "").strip()

    dept_code, dept_name   = parse_department(dept_str)
    manager_emp_no         = extract_manager_emp_no(mgr1)
    manager_name           = extract_manager_name(mgr1)
    upn, upn_source, note  = resolve_upn(row, by_prefix, by_upn, by_lastname)
    date_hired             = parse_date(date_str)

    return {
        "emp_no":         emp_no,
        "full_name":      f"{first} {last}".strip(),
        "upn":            upn,
        "upn_source":     upn_source,
        "upn_note":       note,
        "username":       username,
        "kronos_email":   email,
        "dept_code":      dept_code,
        "dept_name":      dept_name,
        "manager_emp_no": manager_emp_no,
        "manager_name":   manager_name,
        "status":         status,
        "pay_type":       pay_type,
        "date_hired":     date_hired,
    }

def to_sp_fields(parsed):
    fields = {
        "Title":                   parsed["full_name"],
        "Full_x0020_Name":         parsed["full_name"],
        "Kronos_x0020_Username":   parsed["username"],
        "Kronos_x0020_Email":      parsed["kronos_email"],
        "Department_x0020_Code":   parsed["dept_code"],
        "Department_x0020_Name":   parsed["dept_name"],
        "ManagerEmpNo":            parsed["manager_emp_no"],
        "Reporting_x0020_Manager": parsed["manager_name"],
        "Employment_x0020_Status": parsed["status"],
        "Pay_x0020_Type":          parsed["pay_type"],
        "M365_x0020_UPN":          parsed["upn"],  # blank string if not found
    }
    if parsed["date_hired"]:
        fields["Date_x0020_Hired"] = parsed["date_hired"]
    return fields

def fields_changed(existing, new_fields):
    check = [
        ("Title",                   "Title"),
        ("Full_x0020_Name",         "Full_x0020_Name"),
        ("Kronos_x0020_Username",   "Kronos_x0020_Username"),
        ("Kronos_x0020_Email",      "Kronos_x0020_Email"),
        ("Department_x0020_Code",   "Department_x0020_Code"),
        ("Department_x0020_Name",   "Department_x0020_Name"),
        ("ManagerEmpNo",            "ManagerEmpNo"),
        ("Reporting_x0020_Manager", "Reporting_x0020_Manager"),
        ("Employment_x0020_Status", "Employment_x0020_Status"),
        ("Pay_x0020_Type",          "Pay_x0020_Type"),
        ("M365_x0020_UPN",          "M365_x0020_UPN"),
    ]
    for new_key, ex_key in check:
        if new_key in new_fields:
            ex_val  = (existing.get(ex_key) or "").strip()
            new_val = (new_fields[new_key] or "").strip()
            if ex_val != new_val:
                return True
    return False

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: py update_employee_master.py <kronos_csv_file>")
        sys.exit(1)

    csv_path = sys.argv[1]
    if not os.path.exists(csv_path):
        print(f"File not found: {csv_path}")
        sys.exit(1)

    # Step 1: Pull Entra users
    print("\nAuthenticating to Microsoft Graph...")
    graph_token = get_token(GRAPH_SCOPES)
    print("Pulling M365 user list from Entra ID...")
    by_prefix, by_upn, by_lastname = get_entra_users(graph_token)
    print(f"  {len(by_upn)} real M365 accounts found")

    # Step 2: Parse Kronos CSV
    print(f"\nReading {csv_path}...")
    kronos_rows = {}
    skipped     = []
    upn_report  = {
        "prefix": [], "email": [], "display": [],
        "lastname": [], "lastname+first": [], "lastname+initial": [],
        "none": []
    }

    with open(csv_path, newline="", encoding="latin-1") as f:
        reader = csv.DictReader(f)
        for row in reader:
            emp_no = (row.get("Employee Id") or "").strip()
            first  = (row.get("First Name") or "").strip()
            last   = (row.get("Last Name") or "").strip()
            full   = f"{first} {last}".strip()

            if emp_no in SKIP_EMP_NOS or full in SKIP_NAMES:
                skipped.append(f"{emp_no} — {full}")
                continue

            parsed = parse_kronos_row(row, by_prefix, by_upn, by_lastname)
            kronos_rows[emp_no] = parsed

            src = parsed["upn_source"]
            upn_report.setdefault(src, []).append(
                f"  {emp_no:8} {parsed['full_name']:<32} → {parsed['upn'] or '(blank)'}"
                + (f"  [{parsed['upn_note']}]" if parsed["upn_note"] else "")
            )

    print(f"  {len(kronos_rows)} employees to process")
    print(f"  {len(skipped)} system accounts skipped")

    print(f"\nUPN resolution summary:")
    src_labels = {
        "prefix":          "Username prefix match",
        "email":           "Kronos email confirmed",
        "display":         "Display name match",
        "lastname":        "Last name (single match)",
        "lastname+first":  "Last name + first name",
        "lastname+initial":"Last name + initial",
        "none":            "No match → blank",
    }
    for src, label in src_labels.items():
        count = len(upn_report.get(src, []))
        if count:
            marker = "✅" if src != "none" else "—"
            print(f"  {marker} {label}: {count}")

    if upn_report.get("none"):
        print(f"\n  Employees with no M365 account (UPN will be blank):")
        for line in upn_report["none"]:
            print(line)

    # Step 3: Authenticate to SharePoint
    print("\nAuthenticating to SharePoint...")
    sp_token    = get_token(SP_SCOPES)
    digest      = get_request_digest(sp_token)
    entity_type = get_entity_type(sp_token)
    print("  ✅ Authenticated")

    # Step 4: Read existing Employee Master
    print("\nReading existing Employee Master...")
    existing = get_all_employees(sp_token)
    print(f"  {len(existing)} existing rows")

    # Step 5: Update / Create
    created   = 0
    updated   = 0
    unchanged = 0
    errors    = []

    print(f"\nProcessing {len(kronos_rows)} employees...")
    for i, (emp_no, parsed) in enumerate(kronos_rows.items(), start=1):
        if i % 25 == 0:
            try:
                digest = get_request_digest(sp_token)
            except Exception:
                pass

        try:
            sp_fields = to_sp_fields(parsed)

            if emp_no in existing:
                ex = existing[emp_no]
                if fields_changed(ex, sp_fields):
                    sp_update(sp_token, digest, entity_type, ex["ID"], sp_fields)
                    updated += 1
                    print(f"  UPDATED   {emp_no:8} {parsed['full_name']}")
                else:
                    unchanged += 1
            else:
                sp_fields["Employee_x0020_Number"] = emp_no
                new_id = sp_create(sp_token, digest, entity_type, sp_fields)
                created += 1
                print(f"  CREATED   {emp_no:8} {parsed['full_name']} (SP ID: {new_id})")

        except Exception as e:
            errors.append(f"{emp_no} — {parsed['full_name']}: {e}")
            print(f"  ERROR     {emp_no} — {e}")

    # Step 6: Deactivate departed employees
    departed    = set(existing.keys()) - set(kronos_rows.keys()) - SKIP_EMP_NOS
    deactivated = 0
    if departed:
        print(f"\nDeactivating {len(departed)} employees not in Kronos...")
        try:
            digest = get_request_digest(sp_token)
        except Exception:
            pass
        for j, emp_no in enumerate(sorted(departed), start=1):
            if j % 25 == 0:
                try:
                    digest = get_request_digest(sp_token)
                except Exception:
                    pass
            ex        = existing[emp_no]
            full_name = ex.get("Full_x0020_Name") or ex.get("Title", "")
            try:
                sp_update(sp_token, digest, entity_type, ex["ID"], {
                    "Employment_x0020_Status": "Inactive"
                })
                deactivated += 1
                print(f"  DEACTIVATED {emp_no:8} {full_name}")
            except Exception as e:
                errors.append(f"Deactivate {emp_no}: {e}")

    # Summary
    no_upn_count = len(upn_report.get("none", []))
    print(f"""
{'='*60}
IMPORT COMPLETE — {datetime.now().strftime('%Y-%m-%d %H:%M')}
{'='*60}
  Created:      {created}
  Updated:      {updated}
  Unchanged:    {unchanged}
  Deactivated:  {deactivated}
  Errors:       {len(errors)}
  No M365 acct: {no_upn_count} (UPN left blank — correct)
{'='*60}""")

    if errors:
        print("\nErrors to resolve manually:")
        for e in errors:
            print(f"  ✗ {e}")

if __name__ == "__main__":
    main()
