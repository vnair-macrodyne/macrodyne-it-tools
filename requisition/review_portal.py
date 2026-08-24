"""
review_portal.py
================
Static review of the requisition portal against the backend contract.

Checks four things that would otherwise only surface at runtime:
  1. Every event handler in the markup resolves to a defined function
  2. Every getElementById target exists in the markup
  3. Every field read off backend data is one the API actually returns
  4. Action names and status codes match what the backend uses

Run from the folder holding index.html:
  python3 review_portal.py
"""

import re
import sys
from pathlib import Path

# ── The backend contract, mirrored from workflow.py and config.py ────────────
# When the API changes, change these first — the review will then tell you
# what in the page needs updating.

PORTAL_FIELDS  = {"actor", "active", "history", "queue"}
ACTOR_FIELDS   = {"upn", "empNo", "fullName", "department", "managerEmpNo",
                  "isFinanceLow", "isFinanceHigh", "isFulfillment"}
SUMMARY_FIELDS = {"requisitionID", "status", "requestorName", "requestorUPN",
                  "reason", "currency", "totalAmount", "submittedUtc"}
DETAIL_FIELDS  = {"requestorEmpNo", "managerUPN", "paymentMode", "managerComment",
                  "apComment", "fulfillmentComment", "rejectionReason",
                  "orderedUtc", "receivedUtc", "closedUtc"}
LINE_FIELDS    = {"id", "lineNumber", "itemDescription", "quantity",
                  "unitPriceEstimate", "vendorAtPurchase", "actualUnitPrice"}
HISTORY_FIELDS = {"fromStatus", "toStatus", "transitionUtc", "comment"}
ENVELOPE       = {"requisition", "lineItems", "allowedActions", "items",
                  "itemCode", "itemName", "sortOrder", "success", "error"}

BACKEND_ACTIONS = {"Submit", "Approve", "Reject", "Cancel", "MarkOrdered",
                   "MarkReceived", "ConfirmReceipt", "RejectAtPurchase"}
UI_ONLY_ACTIONS = {"CopyToNew"}      # handled in the page, never sent as-is

# Arguments each action must arrive with, taken from the workflow functions
# that raise WorkflowError when they are missing. Without this the page can
# offer a button that always fails, and nothing static would notice.
REQUIRED_ARGS = {
    "Submit":           {"currency", "reason", "lineItems"},
    "Reject":           {"comment"},
    "MarkOrdered":      {"paymentMode", "comment"},
    "RejectAtPurchase": {"comment"},
}

BACKEND_STATUSES = {"Draft", "PendingManager", "PendingAP",
                    "ApprovedPendingPurchase", "Ordered", "ReceivedByPurchaser",
                    "ConfirmedByRequestor", "Closed", "RejectedByManager",
                    "RejectedByAP", "RejectedAtPurchase", "CancelledByRequestor"}

# Methods on browser objects that share a variable name with backend data.
BROWSER_MEMBERS = {"json", "ok", "status", "text", "headers", "body"}


def read_page(path="index.html"):
    html = Path(path).read_text()
    marker = '<script>\n"use strict"'
    if marker not in html:
        raise SystemExit(f"{path} does not contain the expected script block")
    return html, html[html.index(marker):]


def check_handlers(html, script, errors):
    """Every onclick and oninput must name a function the page defines."""
    defined = set(re.findall(r"\bfunction\s+(\w+)\s*\(", script))
    defined |= set(re.findall(r"async\s+function\s+(\w+)\s*\(", script))

    handlers = set(re.findall(r'on(?:click|input)="(\w+)\(', html))
    for name in sorted(handlers):
        if name not in defined:
            errors.append(f'Handler "{name}()" has no matching function')
    return defined, handlers


def check_element_ids(html, script, errors):
    """Every static getElementById target must exist in the markup."""
    ids = set(re.findall(r'\bid="([\w-]+)"', html))
    for ref in sorted(set(re.findall(r'getElementById\("([\w-]+)"\)', script))):
        if ref not in ids:
            errors.append(f'getElementById("{ref}") — no such id in the markup')

    for screen in ("signin", "loading", "portal", "detail"):
        if f"screen-{screen}" not in ids:
            errors.append(f"Screen container screen-{screen} is missing")
    return ids


def check_backend_fields(script, errors):
    """Every field read off backend data must be one the API returns."""
    known = (PORTAL_FIELDS | ACTOR_FIELDS | SUMMARY_FIELDS | DETAIL_FIELDS
             | LINE_FIELDS | HISTORY_FIELDS | ENVELOPE | BROWSER_MEMBERS)

    pattern = r"\b(?:r|me|l|h|c|detail|result|portalData)\.(\w+)\b"
    for field in sorted(set(re.findall(pattern, script))):
        if field not in known:
            errors.append(f"Reads .{field} from backend data — not in the contract")


def check_actions(script, errors):
    """Actions offered in the UI must be ones the backend handles."""
    ui = set(re.findall(r"^\s+(\w+):\s+\{ label:", script, re.M))
    unknown = ui - BACKEND_ACTIONS - UI_ONLY_ACTIONS
    if unknown:
        errors.append(f"UI offers actions the backend cannot handle: {sorted(unknown)}")
    return ui


def check_required_args(script, errors):
    """Each action must send every argument the backend insists on.

    A missing argument produces a button that always fails, which no amount
    of field-name checking would reveal.
    """
    sent = set(re.findall(r"\b(\w+)\s*[:=]\s*(?:answers\.|payload|val\()", script))
    sent |= set(re.findall(r"extra\.(\w+)\s*=", script))
    sent |= set(re.findall(r"apiPost\(\{\s*action:[^}]*?(\w+)[,:}]", script))
    # Anything named in an apiPost body literal counts as sent.
    for body in re.findall(r"apiPost\(\{([^}]*)\}", script, re.S):
        sent |= set(re.findall(r"(\w+)\s*[,:]", body))

    for action, needed in REQUIRED_ARGS.items():
        if action not in script:
            continue
        missing = needed - sent
        if missing:
            errors.append(
                f"{action} needs {sorted(missing)} but the page never sends "
                f"{'it' if len(missing) == 1 else 'them'}"
            )


def check_statuses(script, errors):
    """Every status the backend can send needs display wording."""
    ui = set(re.findall(r"^\s+(\w+):\s+\{ text:", script, re.M))

    missing = BACKEND_STATUSES - ui
    if missing:
        errors.append(f"Statuses with no display wording: {sorted(missing)}")

    extra = ui - BACKEND_STATUSES
    if extra:
        errors.append(f"Wording for statuses the backend never sends: {sorted(extra)}")
    return ui


def main():
    html, script = read_page()
    errors = []

    defined, handlers = check_handlers(html, script, errors)
    ids               = check_element_ids(html, script, errors)
    check_backend_fields(script, errors)
    check_required_args(script, errors)
    actions           = check_actions(script, errors)
    statuses          = check_statuses(script, errors)

    print("=" * 64)
    print("PORTAL REVIEW")
    print("=" * 64)
    print(f"  {len(defined):3} functions defined")
    print(f"  {len(handlers):3} event handlers")
    print(f"  {len(ids):3} element ids")
    print(f"  {len(actions):3} actions, {len(statuses)} statuses mapped")
    print("-" * 64)

    if errors:
        print(f"\n{len(set(errors))} issue(s):\n")
        for e in sorted(set(errors)):
            print(f"  x  {e}")
        return 1

    print("\n  ok  All handlers resolve")
    print("  ok  All element ids exist")
    print("  ok  Backend field reads match the contract")
    print("  ok  Actions and statuses align with the backend")
    print("  ok  Every action sends the arguments the backend requires")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
