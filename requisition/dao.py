"""
dao.py
======
Every SharePoint and Graph call in the application lives here.

Contract with the rest of the app:
  - Functions take and return plain Python dicts and lists.
  - Any failure raises DaoError with a message safe to log.
  - This module imports config only. It never imports workflow,
    notifications or app — data access must not depend on business rules.

SharePoint REST quirks handled here so callers never have to think about them:
  - All requests use application/json;odata=verbose
  - Responses unwrap from d / d.results
  - Writes need a fresh X-RequestDigest
  - Lookup columns take an integer SP ID and the field name gains an Id suffix
"""

import os
import logging
import datetime
import urllib.parse

import msal
import requests

import config

logger = logging.getLogger(__name__)

# Network calls get a ceiling so a hung SharePoint never hangs a worker.
HTTP_TIMEOUT = 30


class DaoError(Exception):
    """Raised for any data-access failure. Carries a message fit for logging."""


# ── Token acquisition ─────────────────────────────────────────────────────────

def _load_token_cache():
    """Read the MSAL cache primed by prime_cache.py. Missing file is not fatal —
    MSAL will simply find no accounts and the caller will get a clear error."""
    cache = msal.SerializableTokenCache()
    try:
        if os.path.exists(config.MSAL_CACHE_PATH):
            with open(config.MSAL_CACHE_PATH, "r") as f:
                cache.deserialize(f.read())
    except OSError as e:
        raise DaoError(f"Could not read token cache at {config.MSAL_CACHE_PATH}: {e}")
    return cache


def _save_token_cache(cache):
    """Persist the cache only when MSAL actually refreshed something."""
    if not cache.has_state_changed:
        return
    try:
        with open(config.MSAL_CACHE_PATH, "w") as f:
            f.write(cache.serialize())
    except OSError as e:
        # A failed write is worth knowing about but must not kill the request —
        # the token in memory is still valid for this call.
        logger.warning(f"Could not persist token cache: {e}")


def _acquire_token(scopes):
    """Get a delegated token silently from the primed cache.

    The app runs unattended, so there is no interactive fallback. If the cache
    is empty or the refresh token has expired, the operator must re-run
    prime_cache.py — the error message says so.
    """
    cache = _load_token_cache()
    client = msal.PublicClientApplication(
        client_id=config.CLIENT_ID,
        authority=config.AUTHORITY,
        token_cache=cache,
    )

    accounts = client.get_accounts()
    if not accounts:
        raise DaoError(
            "No cached credentials. Run prime_cache.py and upload the cache file."
        )

    result = client.acquire_token_silent(scopes=scopes, account=accounts[0])
    _save_token_cache(cache)

    if not result or "access_token" not in result:
        raise DaoError(
            "Cached credentials have expired. Re-run prime_cache.py and upload "
            "the refreshed cache file."
        )
    return result["access_token"]


def _sharepoint_token():
    return _acquire_token(config.SHAREPOINT_SCOPES)


def _graph_token():
    return _acquire_token(config.GRAPH_SCOPES)


# ── Low-level SharePoint REST ─────────────────────────────────────────────────

def _verbose_headers(token, with_content_type=False):
    """Standard header set. SharePoint REST rejects requests that omit
    the odata=verbose accept header."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json;odata=verbose",
    }
    if with_content_type:
        headers["Content-Type"] = "application/json;odata=verbose"
    return headers


def _list_url(site, list_name, suffix=""):
    quoted = urllib.parse.quote(list_name)
    return f"{site}/_api/web/lists/getbytitle('{quoted}')/items{suffix}"


def _request_digest(site, token):
    """SharePoint requires a form digest on every write. It is short-lived,
    so we fetch a fresh one per write rather than caching it."""
    try:
        r = requests.post(
            f"{site}/_api/contextinfo",
            headers=_verbose_headers(token),
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()["d"]["GetContextWebInformation"]["FormDigestValue"]
    except (requests.RequestException, KeyError, ValueError) as e:
        raise DaoError(f"Could not obtain request digest for {site}: {e}")


def _entity_type(site, list_name, token):
    """Every write body needs the list's entity type in __metadata.
    SharePoint derives it from the list name and it is not guessable."""
    quoted = urllib.parse.quote(list_name)
    url = (
        f"{site}/_api/web/lists/getbytitle('{quoted}')"
        f"?$select=ListItemEntityTypeFullName"
    )
    try:
        r = requests.get(url, headers=_verbose_headers(token), timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()["d"]["ListItemEntityTypeFullName"]
    except (requests.RequestException, KeyError, ValueError) as e:
        raise DaoError(f"Could not read entity type for list '{list_name}': {e}")


def get_items(site, list_name, filter_query="", select="", expand="", top=0):
    """Read list items, following pagination until exhausted.

    filter_query, select and expand are raw OData fragments — the caller owns
    getting the internal column names right.
    """
    token = _sharepoint_token()
    headers = _verbose_headers(token)

    params = []
    if filter_query:
        params.append(f"$filter={urllib.parse.quote(filter_query)}")
    if select:
        params.append(f"$select={urllib.parse.quote(select)}")
    if expand:
        params.append(f"$expand={urllib.parse.quote(expand)}")
    if top:
        params.append(f"$top={top}")

    url = _list_url(site, list_name)
    if params:
        url += "?" + "&".join(params)

    items = []
    try:
        while url:
            r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
            if not r.ok:
                raise DaoError(
                    f"Read failed on '{list_name}' ({r.status_code}): {r.text[:300]}"
                )
            payload = r.json().get("d", {})
            items.extend(payload.get("results", []))
            url = payload.get("__next")
    except requests.RequestException as e:
        raise DaoError(f"Network error reading '{list_name}': {e}")
    except ValueError as e:
        raise DaoError(f"Malformed response reading '{list_name}': {e}")

    return items


def create_item(site, list_name, fields):
    """Create one list item. Returns the created row including its SP integer ID."""
    token       = _sharepoint_token()
    entity_type = _entity_type(site, list_name, token)
    digest      = _request_digest(site, token)

    headers = _verbose_headers(token, with_content_type=True)
    headers["X-RequestDigest"] = digest
    body = {"__metadata": {"type": entity_type}, **fields}

    try:
        r = requests.post(
            _list_url(site, list_name),
            headers=headers, json=body, timeout=HTTP_TIMEOUT,
        )
        if not r.ok:
            raise DaoError(
                f"Create failed on '{list_name}' ({r.status_code}): {r.text[:300]}"
            )
        return r.json()["d"]
    except requests.RequestException as e:
        raise DaoError(f"Network error creating item in '{list_name}': {e}")
    except (KeyError, ValueError) as e:
        raise DaoError(f"Malformed create response from '{list_name}': {e}")


def update_item(site, list_name, item_id, fields):
    """Patch one list item. Only the supplied fields are touched."""
    token       = _sharepoint_token()
    entity_type = _entity_type(site, list_name, token)
    digest      = _request_digest(site, token)

    headers = _verbose_headers(token, with_content_type=True)
    headers.update({
        "IF-MATCH":        "*",       # last write wins; no optimistic locking
        "X-HTTP-Method":   "MERGE",   # MERGE patches, PUT would blank omitted fields
        "X-RequestDigest": digest,
    })
    body = {"__metadata": {"type": entity_type}, **fields}

    try:
        r = requests.post(
            _list_url(site, list_name, f"({item_id})"),
            headers=headers, json=body, timeout=HTTP_TIMEOUT,
        )
        if not r.ok:
            raise DaoError(
                f"Update failed on '{list_name}({item_id})' "
                f"({r.status_code}): {r.text[:300]}"
            )
    except requests.RequestException as e:
        raise DaoError(f"Network error updating '{list_name}({item_id})': {e}")


# ── Reference data ────────────────────────────────────────────────────────────

def load_config_values():
    """Return the Requisition Config list as a key/value dict.

    The ConfigKey column is the renamed Title column, so we read Title.
    """
    rows = get_items(config.REQ_SITE, config.LIST_CONFIG)
    return {
        row.get(config.COL_CONFIG_KEY, ""): row.get("ConfigValue", "")
        for row in rows
    }


def load_roles():
    """Return the Requisition Roles list keyed by role code.

    The RoleCode column is the renamed Title column.
    """
    rows = get_items(config.REQ_SITE, config.LIST_ROLES)
    return {row.get(config.COL_ROLE_CODE, ""): row for row in rows}


def get_catalogue():
    """Return catalogue rows for the submission form's item picker."""
    return get_items(config.REQ_SITE, config.LIST_CATALOGUE)


def get_catalogue_id(item_code):
    """Resolve a catalogue ItemCode to its SP integer ID.

    Returns None for free-text items so the caller can omit the lookup field
    entirely — SharePoint rejects a Lookup set to an empty string.
    """
    if not item_code or item_code == "OTHER":
        return None
    rows = get_items(
        config.REQ_SITE, config.LIST_CATALOGUE,
        filter_query=f"{config.COL_CATALOGUE_CODE} eq '{item_code}'",
        top=1,
    )
    return rows[0]["ID"] if rows else None


# ── Employee lookups ──────────────────────────────────────────────────────────

def get_employee_by_upn(upn):
    """Find one employee by M365 UPN. Returns None when not found."""
    if not upn:
        return None
    rows = get_items(
        config.CRD_SITE, config.LIST_EMPLOYEE,
        filter_query=f"{config.COL_EMP_UPN} eq '{upn}'",
        top=1,
    )
    return rows[0] if rows else None


def get_employee_by_emp_no(emp_no):
    """Find one employee by employee number. Returns None when not found."""
    if not emp_no:
        return None
    rows = get_items(
        config.CRD_SITE, config.LIST_EMPLOYEE,
        filter_query=f"{config.COL_EMP_NUMBER} eq '{emp_no}'",
        top=1,
    )
    return rows[0] if rows else None


def get_upn_for_emp_no(emp_no):
    """Convenience wrapper — returns the UPN string or empty string."""
    employee = get_employee_by_emp_no(emp_no)
    return employee.get(config.COL_EMP_UPN, "") if employee else ""


# ── Requisition reads ─────────────────────────────────────────────────────────

def get_requisition(requisition_id):
    """Fetch one requisition by its business ID (REQ-YYYY-NNNN).

    Filtering on the RequisitionID display name 500s — SharePoint only accepts
    the internal name, which is Title.
    """
    rows = get_items(
        config.REQ_SITE, config.LIST_REQ,
        filter_query=f"{config.COL_REQ_ID} eq '{requisition_id}'",
        top=1,
    )
    return rows[0] if rows else None


def get_requisitions_by_requestor(requestor_upn):
    """All requisitions submitted by one person, newest first."""
    if not requestor_upn:
        return []
    rows = get_items(
        config.REQ_SITE, config.LIST_REQ,
        filter_query=f"RequestorUPN eq '{requestor_upn}'",
    )
    return _sort_newest_first(rows)


def get_requisitions_by_statuses(statuses):
    """All requisitions currently in any of the given statuses, newest first.

    SharePoint has no IN operator, so statuses are OR-ed together.
    """
    if not statuses:
        return []
    clause = " or ".join(f"Status eq '{s}'" for s in statuses)
    rows = get_items(config.REQ_SITE, config.LIST_REQ, filter_query=clause)
    return _sort_newest_first(rows)


def _sort_newest_first(rows):
    """Sort by SP ID descending. ID is monotonic, so it is a reliable proxy for
    submission order and avoids parsing date strings."""
    return sorted(rows, key=_row_id, reverse=True)


def _row_id(row):
    return row.get("ID", 0)


def get_line_items(requisition_id):
    """Fetch the line items for one requisition.

    RequisitionID on the Lines list is a Lookup, so the filter must traverse
    into the parent's Title column.
    """
    return get_items(
        config.REQ_SITE, config.LIST_LINES,
        filter_query=f"RequisitionID/{config.COL_REQ_ID} eq '{requisition_id}'",
    )


def get_history(requisition_id):
    """Fetch the audit trail for one requisition, oldest first."""
    rows = get_items(
        config.REQ_SITE, config.LIST_HISTORY,
        filter_query=f"RequisitionID/{config.COL_REQ_ID} eq '{requisition_id}'",
    )
    return sorted(rows, key=_row_id)


# ── Requisition writes ────────────────────────────────────────────────────────

def create_requisition(fields):
    """Create the parent requisition row. Returns the created row."""
    return create_item(config.REQ_SITE, config.LIST_REQ, fields)


def update_requisition(sp_item_id, fields):
    """Patch a requisition row by its SP integer ID."""
    update_item(config.REQ_SITE, config.LIST_REQ, sp_item_id, fields)


def create_line_item(fields):
    """Create one requisition line."""
    return create_item(config.REQ_SITE, config.LIST_LINES, fields)


def update_line_item(sp_item_id, fields):
    """Patch one requisition line by its SP integer ID."""
    update_item(config.REQ_SITE, config.LIST_LINES, sp_item_id, fields)


def create_history_entry(fields):
    """Append one row to the status history audit trail."""
    return create_item(config.REQ_SITE, config.LIST_HISTORY, fields)


# ── Mail ──────────────────────────────────────────────────────────────────────

def send_mail(subject, html_body, to_addresses, cc_addresses=None):
    """Send one HTML email through Graph as the cached delegated user.

    to_addresses and cc_addresses are lists of strings. Empty recipients are
    dropped. An empty To list is a caller bug, so it raises rather than
    silently doing nothing.
    """
    recipients = [a.strip() for a in (to_addresses or []) if a and a.strip()]
    if not recipients:
        raise DaoError(f"send_mail called with no recipients. Subject: {subject}")

    cc = [a.strip() for a in (cc_addresses or []) if a and a.strip()]

    token = _graph_token()
    message = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": a}} for a in recipients],
            "ccRecipients": [{"emailAddress": {"address": a}} for a in cc],
        }
    }

    try:
        r = requests.post(
            "https://graph.microsoft.com/v1.0/me/sendMail",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=message,
            timeout=HTTP_TIMEOUT,
        )
        if not r.ok:
            raise DaoError(f"Mail send failed ({r.status_code}): {r.text[:300]}")
    except requests.RequestException as e:
        raise DaoError(f"Network error sending mail '{subject}': {e}")


# ── Helpers shared by callers ─────────────────────────────────────────────────

def utc_now_iso():
    """Timestamp in the format SharePoint DateTime columns accept."""
    return datetime.datetime.utcnow().isoformat() + "Z"
