"""
config.py
=========
All environment-dependent settings and constants for the Requisition app.

This module is imported by every other module. It imports nothing from them —
it sits at the bottom of the dependency graph and must stay that way.

Environment variables (Azure App Service → Configuration → Application settings):
  TENANT_ID        Entra tenant GUID
  CLIENT_ID        App registration client ID
  MSAL_CACHE_PATH  Path to the delegated-token cache primed by prime_cache.py
"""

import os

# ── Identity ──────────────────────────────────────────────────────────────────

TENANT_ID       = os.environ["TENANT_ID"]
CLIENT_ID       = os.environ["CLIENT_ID"]
MSAL_CACHE_PATH = os.environ.get("MSAL_CACHE_PATH", "/home/.msalcache_req")

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"

# Two token audiences: Graph for mail, SharePoint REST for list access.
# They cannot be combined in a single token request.
GRAPH_SCOPES = [
    "https://graph.microsoft.com/Sites.Read.All",
    "https://graph.microsoft.com/Mail.Send",
]
SHAREPOINT_SCOPES = [
    "https://macrodyne.sharepoint.com/AllSites.Write",
    "https://macrodyne.sharepoint.com/AllSites.Read",
]

# ── SharePoint sites ──────────────────────────────────────────────────────────

REQ_SITE = "https://macrodyne.sharepoint.com/sites/Requisitions"
CRD_SITE = "https://macrodyne.sharepoint.com/sites/CorporateReferenceData"

# ── SharePoint list names (display names, as used in getbytitle) ───────────────

LIST_CONFIG    = "Requisition Config"
LIST_ROLES     = "Requisition Roles"
LIST_REQ       = "Requisitions"
LIST_LINES     = "Requisition Lines"
LIST_HISTORY   = "Requisition Status History"
LIST_CATALOGUE = "Requisition Items Catalogue"
LIST_EMPLOYEE  = "Employee Master"

# ── SharePoint internal column names ──────────────────────────────────────────
# Several columns were renamed after creation. SharePoint keeps the original
# internal name, so the display name in the UI does not match what the REST API
# expects. These constants exist so that quirk lives in exactly one place.

# Requisitions list: the "RequisitionID" column is the renamed Title column.
COL_REQ_ID = "Title"

# Requisitions list: "ManagerApproverEmpName" collided with an existing name,
# so SharePoint appended a 0.
COL_MGR_APPROVER_NAME = "ManagerApproverEmpNo0"

# Employee Master: columns created with spaces get _x0020_ encoding.
COL_EMP_NUMBER = "Employee_x0020_Number"
COL_EMP_NAME   = "Full_x0020_Name"
COL_EMP_UPN    = "M365_x0020_UPN"
COL_EMP_DEPT   = "Department_x0020_Name"
COL_EMP_MGR    = "ManagerEmpNo"

# Config and Roles lists: Title column renamed to ConfigKey / RoleCode.
COL_CONFIG_KEY = "Title"
COL_ROLE_CODE  = "Title"

# Catalogue list: Title column renamed to ItemCode.
COL_CATALOGUE_CODE = "Title"

# ── Role codes (values in the Requisition Roles list) ──────────────────────────
# Financial authority is split by amount. The Director of Finance signs off
# smaller spend; the CFO signs off anything at or above the threshold.

ROLE_FINANCE_LOW  = "FinanceAuthority-Low"    # Director of Finance
ROLE_FINANCE_HIGH = "FinanceAuthority-High"   # CFO
ROLE_FULFILLMENT  = "Fulfillment"

# ── Requisition statuses ──────────────────────────────────────────────────────
# These strings must match the Choice values defined on the Status column.

STATUS_DRAFT               = "Draft"
STATUS_PENDING_MANAGER     = "PendingManager"
STATUS_PENDING_AP          = "PendingAP"   # the finance-authority gate
STATUS_APPROVED_PENDING    = "ApprovedPendingPurchase"
STATUS_ORDERED             = "Ordered"
STATUS_RECEIVED            = "ReceivedByPurchaser"
STATUS_CONFIRMED           = "ConfirmedByRequestor"
STATUS_CLOSED              = "Closed"
STATUS_REJECTED_MANAGER    = "RejectedByManager"
STATUS_REJECTED_AP         = "RejectedByAP"
STATUS_REJECTED_PURCHASE   = "RejectedAtPurchase"
STATUS_CANCELLED           = "CancelledByRequestor"

# Statuses where the requisition is still moving through the workflow.
ACTIVE_STATUSES = {
    STATUS_DRAFT,
    STATUS_PENDING_MANAGER,
    STATUS_PENDING_AP,
    STATUS_APPROVED_PENDING,
    STATUS_ORDERED,
    STATUS_RECEIVED,
}

# Statuses where the requisition is finished, for better or worse.
TERMINAL_STATUSES = {
    STATUS_CLOSED,
    STATUS_REJECTED_MANAGER,
    STATUS_REJECTED_AP,
    STATUS_REJECTED_PURCHASE,
    STATUS_CANCELLED,
}

# A requestor can withdraw a requisition only before it has been ordered.
CANCELLABLE_STATUSES = {
    STATUS_DRAFT,
    STATUS_PENDING_MANAGER,
    STATUS_PENDING_AP,
    STATUS_APPROVED_PENDING,
}

# ── Portal ────────────────────────────────────────────────────────────────────

PORTAL_BASE_URL = "https://orange-bay-0e0de3210.7.azurestaticapps.net/requisition/"

# ── Defaults used when a Config row is missing ────────────────────────────────

DEFAULT_CAP                  = 5000.0
DEFAULT_FINANCE_THRESHOLD    = 1000.0   # at or above this, the CFO signs off
DEFAULT_RECEIPT_CONFIRM_DAYS = "5"
DEFAULT_SENDER               = "vnair@macrodynepress.com"

# Config list keys, named here so a typo shows up in one place rather than
# silently falling back to a default somewhere in the workflow.
CFG_MAX_CAD           = "MaxCAD"
CFG_MAX_USD           = "MaxUSD"
CFG_FINANCE_THRESHOLD = "FinanceAuthorityThreshold"
CFG_RECEIPT_DAYS      = "ReceiptConfirmDays"
