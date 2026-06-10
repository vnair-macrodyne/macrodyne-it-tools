"""
Macrodyne Requisition App — Flask Web App
==========================================
Single HTTP-triggered Flask app handling all requisition routing logic.

Endpoints:
  POST /api/requisition          — main entry point from HTML form
  GET  /api/approve?token=<jwt>  — approval link handler (email clicks)
  GET  /health                   — health check for App Service

Environment variables (set in Azure App Service Application Settings):
  APPROVAL_TOKEN_SECRET   — 32-char random string for JWT signing
  FUNCTION_BASE_URL       — e.g. https://macrodyne-requisition.azurewebsites.net
  TENANT_ID               — 4c9a50a1-c27f-4044-8025-b59b5b804d16
  CLIENT_ID               — 6bfa3adb-1f7d-44e4-8cb9-38be4a107549
  MSAL_CACHE_PATH         — path to token cache file, e.g. /home/.msalcache_req

All SharePoint writes use the cached delegated token (vnair@).
Run the app once interactively to prime the cache.
# Deployment: 2026-06-09-v6.5
"""

import os
import json
import time
import logging
import datetime
import urllib.parse
from flask import Flask, request, Response, jsonify
import requests
import jwt
import msal

# ── Configuration ──────────────────────────────────────────────────────────────

TENANT_ID         = os.environ["TENANT_ID"]
CLIENT_ID         = os.environ["CLIENT_ID"]
TOKEN_SECRET      = os.environ["APPROVAL_TOKEN_SECRET"]
FUNCTION_BASE_URL = os.environ["FUNCTION_BASE_URL"].rstrip("/")
MSAL_CACHE_PATH   = os.environ.get("MSAL_CACHE_PATH", "/home/.msalcache_req")

GRAPH_SCOPES = [
    "https://graph.microsoft.com/Sites.Read.All",
    "https://graph.microsoft.com/Mail.Send",
]
SP_WRITE_SCOPES = [
    "https://macrodyne.sharepoint.com/AllSites.Write",
    "https://macrodyne.sharepoint.com/AllSites.Read",
]

# SharePoint sites
REQ_SITE  = "https://macrodyne.sharepoint.com/sites/Requisitions"
CRD_SITE  = "https://macrodyne.sharepoint.com/sites/CorporateReferenceData"

# SharePoint list names
LIST_CONFIG     = "Requisition Config"
LIST_ROLES      = "Requisition Roles"
LIST_REQ        = "Requisitions"
LIST_LINES      = "Requisition Lines"
LIST_HISTORY    = "Requisition Status History"
LIST_CATALOGUE  = "Requisition Items Catalogue"
LIST_EMPLOYEE   = "Employee Master"

# Token expiry for approval links (hours)
APPROVAL_TOKEN_EXPIRY_HOURS = 72

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Flask app ──────────────────────────────────────────────────────────────────

app = Flask(__name__)

@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response

@app.route("/api/requisition", methods=["OPTIONS"])
def requisition_options():
    return Response("", status=200)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/api/requisition", methods=["POST", "OPTIONS"])
def requisition_handler():
    if request.method == "OPTIONS":
        return Response("", status=200)

    try:
        payload = request.get_json(force=True)
    except Exception:
        return Response(
            json.dumps({"success": False, "error": "Invalid JSON payload"}),
            status=400, mimetype="application/json"
        )

    action = payload.get("action", "")
    logger.info(f"Requisition action: {action}")

    try:
        if action == "Submit":
            result = handle_submit(payload)
        elif action == "Cancel":
            result = handle_cancel(payload)
        elif action == "MarkOrdered":
            result = handle_mark_ordered(payload)
        elif action == "MarkReceived":
            result = handle_mark_received(payload)
        elif action == "Confirm":
            result = handle_confirm(payload)
        elif action == "RejectAtPurchase":
            result = handle_reject_at_purchase(payload)
        elif action == "Close":
            result = handle_close(payload)
        else:
            result = {"success": False, "error": f"Unknown action: {action}"}

    except Exception as e:
        logger.exception(f"Error handling action {action}")
        result = {"success": False, "error": str(e)}

    return Response(
        json.dumps(result),
        status=200, mimetype="application/json"
    )


@app.route("/api/approve", methods=["GET"])
def approve_handler():
    token_str = request.args.get("token", "")
    action    = request.args.get("action", "")
    comment   = request.args.get("comment", "")

    if not token_str or action not in ("approve", "reject"):
        return Response(
            _html_page("Invalid Link",
                "This approval link is invalid or malformed. "
                "Please contact IT if you believe this is an error."),
            status=400, mimetype="text/html"
        )

    try:
        claims = jwt.decode(token_str, TOKEN_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return Response(
            _html_page("Link Expired",
                "This approval link has expired (links are valid for 72 hours). "
                "Please check if the requisition has already been actioned, "
                "or contact the requestor to re-submit."),
            status=400, mimetype="text/html"
        )
    except jwt.InvalidTokenError:
        return Response(
            _html_page("Invalid Link",
                "This approval link is invalid. "
                "Please contact IT if you believe this is an error."),
            status=400, mimetype="text/html"
        )

    requisition_id  = claims["requisitionID"]
    expected_status = claims["expectedStatus"]
    actor_emp_no    = claims["actorEmpNo"]
    actor_name      = claims.get("actorName", "")

    try:
        if action == "approve":
            result = handle_approval(
                requisition_id, expected_status,
                actor_emp_no, actor_name, comment
            )
        else:
            result = handle_rejection(
                requisition_id, expected_status,
                actor_emp_no, actor_name, comment
            )

        if result["success"]:
            return Response(
                _html_page("Thank You",
                    f"Your response has been recorded for requisition "
                    f"<strong>{requisition_id}</strong>. "
                    f"The requestor has been notified."),
                mimetype="text/html"
            )
        else:
            return Response(
                _html_page("Already Actioned",
                    f"Requisition <strong>{requisition_id}</strong> has already "
                    f"been actioned (current status: {result.get('currentStatus','')})."
                    f" No further action is needed."),
                mimetype="text/html"
            )

    except Exception as e:
        logger.exception("Error processing approval")
        return Response(
            _html_page("Error",
                f"An error occurred processing your response: {str(e)}. "
                f"Please contact IT."),
            status=500, mimetype="text/html"
        )


# ── Branch A — Submit ──────────────────────────────────────────────────────────

def handle_submit(payload: dict) -> dict:
    config = load_config()
    roles  = load_roles()

    requestor_emp_no = payload["requestorEmpNo"]
    requestor_upn    = payload["requestorUPN"]
    requestor_name   = payload["requestorFullName"]
    manager_emp_no   = payload.get("requestorManagerEmpNo", "")
    dept             = payload.get("requestorDept", "")
    currency         = payload["currency"]
    reason           = payload["reason"]
    line_items       = payload["lineItems"]

    max_cap = float(config.get("MaxCAD" if currency == "CAD" else "MaxUSD", 5000))
    notification_sender = config.get("NotificationSender", "vnair@macrodynepress.com")

    total = sum(
        float(item["quantity"]) * float(item["unitPriceEstimate"])
        for item in line_items
    )
    if total > max_cap:
        return {
            "success": False,
            "capExceeded": True,
            "error": (
                f"This requisition exceeds the {currency} cap of "
                f"${max_cap:,.2f}. Please use the formal PO process "
                f"for purchases over this amount."
            )
        }

    # Skip manager approval if total is below the configured threshold.
    # If the config key is missing, default to 0 (all requisitions require manager approval).
    threshold_key = "ManagerThresholdCAD" if currency == "CAD" else "ManagerThresholdUSD"
    threshold = float(config.get(threshold_key, 0))
    initial_status = "PendingAP" if (not manager_emp_no or total < threshold) else "PendingManager"

    manager_upn = ""
    if manager_emp_no:
        manager_upn = resolve_upn(manager_emp_no)

    now = datetime.datetime.utcnow().isoformat() + "Z"

    # Create Requisition row — SP assigns atomic integer ID
    req_row = {
        "Title":                  "PENDING",
        "Status":                 initial_status,
        "RequestorEmpNo":         requestor_emp_no,
        "RequestorUPN":           requestor_upn,
        "RequestorName":          requestor_name,
        "ManagerEmpNoSnapshot":   manager_emp_no,
        "ManagerUPN":             manager_upn,
        "Reason":                 reason,
        "Currency":               currency,
        "TotalAmount":            total,
    }
    created = sp_create_item(REQ_SITE, LIST_REQ, req_row)
    sp_item_id = created["ID"]
    requisition_id = generate_requisition_id(sp_item_id)

    # Update Title with real RequisitionID
    sp_update_item(REQ_SITE, LIST_REQ, sp_item_id, {
        "Title": requisition_id,
    })

    # Create line items — RequisitionID and ItemCode are Lookup fields
    for i, item in enumerate(line_items, start=1):
        item_code = item.get("itemCode", "OTHER")
        line_row = {
            "Title":             f"{requisition_id}-{i:02d}",
            "RequisitionIDId":   sp_item_id,            # Lookup → integer SP ID
            "LineNumber":        i,
            "ItemDescription":   item.get("itemDescription", ""),
            "Quantity":          item.get("quantity", 1),
            "UnitPriceEstimate": item.get("unitPriceEstimate", 0),
        }
        # ItemCode: Lookup — omit entirely for free-text OTHER items
        if item_code and item_code != "OTHER":
            cat_id = resolve_catalogue_id(item_code)
            if cat_id:
                line_row["ItemCodeId"] = cat_id         # Lookup → integer SP ID
        # ItemURL: URL field — requires object format, omit if empty
        item_url = item.get("itemURL", "").strip()
        if item_url:
            line_row["ItemURL"] = {"Url": item_url, "Description": item_url}

        sp_create_item(REQ_SITE, LIST_LINES, line_row)

    write_history(
        requisition_id=requisition_id,
        sp_req_id=sp_item_id,
        from_status="Draft",
        to_status=initial_status,
        actor_emp_no=requestor_emp_no,
        comment="Submitted by requestor"
    )

    if initial_status == "PendingManager":
        _send_manager_approval_email(
            requisition_id=requisition_id,
            requestor_name=requestor_name,
            dept=dept,
            reason=reason,
            total=total,
            currency=currency,
            line_items=line_items,
            submitted_utc=now,
            manager_emp_no=manager_emp_no,
            manager_upn=manager_upn,
            notification_sender=notification_sender,
            roles=roles
        )
    else:
        ap_upn = _resolve_ap_upn(requestor_emp_no, roles)
        _send_ap_approval_email(
            requisition_id=requisition_id,
            requestor_name=requestor_name,
            dept=dept,
            reason=reason,
            total=total,
            currency=currency,
            line_items=line_items,
            submitted_utc=now,
            ap_upn=ap_upn,
            notification_sender=notification_sender,
            manager_skipped=True
        )

    return {"success": True, "requisitionID": requisition_id}


# ── Approval handling ──────────────────────────────────────────────────────────

def handle_approval(
    requisition_id, expected_status, actor_emp_no, actor_name, comment
) -> dict:
    config = load_config()
    roles  = load_roles()
    notification_sender = config.get("NotificationSender", "vnair@macrodynepress.com")

    req = get_requisition(requisition_id)
    if not req:
        raise ValueError(f"Requisition {requisition_id} not found")

    current_status = req.get("Status", "")
    if current_status != expected_status:
        return {"success": False, "currentStatus": current_status}

    sp_req_id = req["ID"]
    now = datetime.datetime.utcnow().isoformat() + "Z"

    if current_status == "PendingManager":
        sp_update_item(REQ_SITE, LIST_REQ, sp_req_id, {
            "Status":               "PendingAP",
            "ManagerApprovedUtc":   now,
            "ManagerApproverEmpNo": actor_emp_no,
            "ManagerApproverEmpNo0": actor_name,        # internal name of ManagerApproverEmpName
            "ManagerComment":       comment,
        })
        write_history(requisition_id, sp_req_id, "PendingManager", "PendingAP",
                      actor_emp_no, comment or "Approved by manager")

        ap_upn = _resolve_ap_upn(req["RequestorEmpNo"], roles)
        _send_ap_approval_email(
            requisition_id=requisition_id,
            requestor_name=req["RequestorName"],
            dept="",
            reason=req["Reason"],
            total=float(req.get("TotalAmount", 0)),
            currency=req.get("Currency", "CAD"),
            line_items=get_line_items(requisition_id),
            submitted_utc=req.get("SubmittedUtc", ""),
            ap_upn=ap_upn,
            notification_sender=notification_sender,
            manager_skipped=False
        )

    elif current_status == "PendingAP":
        sp_update_item(REQ_SITE, LIST_REQ, sp_req_id, {
            "Status":          "ApprovedPendingPurchase",
            "APApprovedUtc":   now,
            "APApproverEmpNo": actor_emp_no,
            "APComment":       comment,
        })
        write_history(requisition_id, sp_req_id, "PendingAP", "ApprovedPendingPurchase",
                      actor_emp_no, comment or "Approved by AP")

        fulfill_upn = _resolve_fulfill_upn(req["RequestorEmpNo"], roles)
        _send_fulfillment_email(
            requisition_id=requisition_id,
            requestor_name=req["RequestorName"],
            requestor_upn=req.get("RequestorUPN", ""),
            reason=req["Reason"],
            total=float(req.get("TotalAmount", 0)),
            currency=req.get("Currency", "CAD"),
            line_items=get_line_items(requisition_id),
            approved_utc=now,
            fulfill_upn=fulfill_upn,
            manager_upn=req.get("ManagerUPN", ""),
            notification_sender=notification_sender
        )

    return {"success": True}


def handle_rejection(
    requisition_id, expected_status, actor_emp_no, actor_name, comment
) -> dict:
    config = load_config()
    notification_sender = config.get("NotificationSender", "vnair@macrodynepress.com")

    req = get_requisition(requisition_id)
    if not req:
        raise ValueError(f"Requisition {requisition_id} not found")

    current_status = req.get("Status", "")
    if current_status != expected_status:
        return {"success": False, "currentStatus": current_status}

    sp_req_id = req["ID"]
    now = datetime.datetime.utcnow().isoformat() + "Z"
    terminal_status = (
        "RejectedByManager" if current_status == "PendingManager"
        else "RejectedByAP"
    )

    sp_update_item(REQ_SITE, LIST_REQ, sp_req_id, {
        "Status":          terminal_status,
        "RejectionReason": comment,
    })
    write_history(requisition_id, sp_req_id, current_status, terminal_status,
                  actor_emp_no, comment or "Rejected")

    cc = req.get("ManagerUPN", "") if current_status == "PendingAP" else ""
    send_email(
        sender=notification_sender,
        to=req.get("RequestorUPN", ""),
        cc=cc,
        subject=f"Requisition not approved — {requisition_id}",
        body=_rejection_email_body(requisition_id, req["RequestorName"], comment)
    )
    return {"success": True}


# ── Branch D — Cancel ─────────────────────────────────────────────────────────

def handle_cancel(payload: dict) -> dict:
    requisition_id = payload["requisitionID"]
    actor_emp_no   = payload["actorEmpNo"]
    comment        = payload.get("comment", "")

    config = load_config()
    notification_sender = config.get("NotificationSender", "vnair@macrodynepress.com")

    req = get_requisition(requisition_id)
    if not req:
        return {"success": False, "error": "Requisition not found"}

    cancellable = {"Draft", "PendingManager", "PendingAP", "ApprovedPendingPurchase"}
    current_status = req.get("Status", "")
    if current_status not in cancellable:
        return {"success": False, "error": "This requisition can no longer be cancelled."}

    sp_req_id = req["ID"]
    sp_update_item(REQ_SITE, LIST_REQ, sp_req_id, {
        "Status":             "CancelledByRequestor",
        "CancellationReason": comment,
    })
    write_history(requisition_id, sp_req_id, current_status, "CancelledByRequestor",
                  actor_emp_no, comment or "Cancelled by requestor")

    if current_status == "PendingManager":
        send_email(
            sender=notification_sender,
            to=req.get("ManagerUPN", ""),
            cc="",
            subject=f"Requisition cancelled — {requisition_id}",
            body=f"<p>Requisition <strong>{requisition_id}</strong> has been cancelled by the requestor. No further action is required.</p>"
        )
    elif current_status in {"PendingAP", "ApprovedPendingPurchase"}:
        roles = load_roles()
        ap_upn      = _resolve_ap_upn(req["RequestorEmpNo"], roles)
        fulfill_upn = _resolve_fulfill_upn(req["RequestorEmpNo"], roles)
        send_email(
            sender=notification_sender,
            to=ap_upn,
            cc=fulfill_upn,
            subject=f"Requisition cancelled — {requisition_id}",
            body=f"<p>Requisition <strong>{requisition_id}</strong> has been cancelled by the requestor. No further action is required.</p>"
        )

    return {"success": True}


# ── Branch E — Mark Ordered ───────────────────────────────────────────────────

def handle_mark_ordered(payload: dict) -> dict:
    requisition_id = payload["requisitionID"]
    actor_emp_no   = payload["actorEmpNo"]
    payment_mode   = payload["paymentMode"]
    line_updates   = payload.get("lineUpdates", [])

    config = load_config()
    notification_sender = config.get("NotificationSender", "vnair@macrodynepress.com")

    req = get_requisition(requisition_id)
    if not req:
        return {"success": False, "error": "Requisition not found"}

    sp_req_id = req["ID"]
    now = datetime.datetime.utcnow().isoformat() + "Z"
    sp_update_item(REQ_SITE, LIST_REQ, sp_req_id, {
        "Status":         "Ordered",
        "OrderedUtc":     now,
        "OrderedByEmpNo": actor_emp_no,
        "PaymentMode":    payment_mode,
    })
    write_history(requisition_id, sp_req_id, "ApprovedPendingPurchase", "Ordered",
                  actor_emp_no, f"Ordered via {payment_mode}")

    for update in line_updates:
        sp_update_item(REQ_SITE, LIST_LINES, update["id"], {
            "VendorAtPurchase": update.get("vendorAtPurchase", ""),
            "ActualUnitPrice":  update.get("actualUnitPrice", 0),
        })

    send_email(
        sender=notification_sender,
        to=req.get("RequestorUPN", ""),
        cc=req.get("ManagerUPN", ""),
        subject=f"Your requisition has been ordered — {requisition_id}",
        body=_ordered_email_body(requisition_id, req["RequestorName"], payment_mode, now)
    )
    return {"success": True}


# ── Branch F — Mark Received ──────────────────────────────────────────────────

def handle_mark_received(payload: dict) -> dict:
    requisition_id = payload["requisitionID"]
    actor_emp_no   = payload["actorEmpNo"]

    config = load_config()
    notification_sender = config.get("NotificationSender", "vnair@macrodynepress.com")

    req = get_requisition(requisition_id)
    if not req:
        return {"success": False, "error": "Requisition not found"}

    sp_req_id = req["ID"]
    now = datetime.datetime.utcnow().isoformat() + "Z"
    sp_update_item(REQ_SITE, LIST_REQ, sp_req_id, {
        "Status":          "ReceivedByPurchaser",
        "ReceivedUtc":     now,
        "ReceivedByEmpNo": actor_emp_no,
    })
    write_history(requisition_id, sp_req_id, "Ordered", "ReceivedByPurchaser",
                  actor_emp_no, "Item received at reception")

    receipt_days = config.get("ReceiptConfirmDays", "5")
    send_email(
        sender=notification_sender,
        to=req.get("RequestorUPN", ""),
        cc=req.get("ManagerUPN", ""),
        subject=f"Your order has arrived — {requisition_id}",
        body=_received_email_body(requisition_id, req["RequestorName"], now, receipt_days)
    )
    return {"success": True}


# ── Branch G — Confirm Receipt ────────────────────────────────────────────────

def handle_confirm(payload: dict) -> dict:
    requisition_id = payload["requisitionID"]
    actor_emp_no   = payload["actorEmpNo"]

    config = load_config()
    roles  = load_roles()
    notification_sender = config.get("NotificationSender", "vnair@macrodynepress.com")

    req = get_requisition(requisition_id)
    if not req:
        return {"success": False, "error": "Requisition not found"}

    sp_req_id = req["ID"]
    now = datetime.datetime.utcnow().isoformat() + "Z"
    sp_update_item(REQ_SITE, LIST_REQ, sp_req_id, {
        "Status":       "Closed",
        "ConfirmedUtc": now,
        "ClosedUtc":    now,
    })
    write_history(requisition_id, sp_req_id, "ReceivedByPurchaser", "ConfirmedByRequestor",
                  actor_emp_no, "Requestor confirmed receipt")
    write_history(requisition_id, sp_req_id, "ConfirmedByRequestor", "Closed",
                  actor_emp_no, "Auto-closed on receipt confirmation")

    fulfill_upn = _resolve_fulfill_upn(req["RequestorEmpNo"], roles)
    send_email(
        sender=notification_sender,
        to=fulfill_upn,
        cc=req.get("ManagerUPN", ""),
        subject=f"Receipt confirmed — {requisition_id}",
        body=f"<p>{req['RequestorName']} has confirmed receipt of their order for requisition <strong>{requisition_id}</strong>. This requisition is now closed.</p>"
    )
    return {"success": True}


# ── Branch H — Reject at Purchase ────────────────────────────────────────────

def handle_reject_at_purchase(payload: dict) -> dict:
    requisition_id = payload["requisitionID"]
    actor_emp_no   = payload["actorEmpNo"]
    comment        = payload.get("comment", "")

    config = load_config()
    roles  = load_roles()
    notification_sender = config.get("NotificationSender", "vnair@macrodynepress.com")

    req = get_requisition(requisition_id)
    if not req:
        return {"success": False, "error": "Requisition not found"}

    sp_req_id = req["ID"]
    now = datetime.datetime.utcnow().isoformat() + "Z"
    sp_update_item(REQ_SITE, LIST_REQ, sp_req_id, {
        "Status":                    "RejectedAtPurchase",
        "RejectedAtPurchaseUtc":     now,
        "RejectedAtPurchaseByEmpNo": actor_emp_no,
        "RejectionReason":           comment,
    })
    write_history(requisition_id, sp_req_id, "ApprovedPendingPurchase", "RejectedAtPurchase",
                  actor_emp_no, comment or "Rejected at purchase")

    ap_upn   = _resolve_ap_upn(req["RequestorEmpNo"], roles)
    cc_parts = [req.get("ManagerUPN", ""), ap_upn]
    cc       = ",".join(p for p in cc_parts if p)

    send_email(
        sender=notification_sender,
        to=req.get("RequestorUPN", ""),
        cc=cc,
        subject=f"Requisition could not be purchased — {requisition_id}",
        body=_rejected_at_purchase_email_body(requisition_id, req["RequestorName"], comment)
    )
    return {"success": True}


# ── Branch I — Manual Close ───────────────────────────────────────────────────

def handle_close(payload: dict) -> dict:
    requisition_id = payload["requisitionID"]
    actor_emp_no   = payload["actorEmpNo"]
    comment        = payload.get("comment", "")

    req = get_requisition(requisition_id)
    if not req:
        return {"success": False, "error": "Requisition not found"}

    sp_req_id      = req["ID"]
    current_status = req.get("Status", "")
    now = datetime.datetime.utcnow().isoformat() + "Z"

    sp_update_item(REQ_SITE, LIST_REQ, sp_req_id, {
        "Status":    "Closed",
        "ClosedUtc": now,
    })
    write_history(requisition_id, sp_req_id, current_status, "Closed",
                  actor_emp_no, comment or "Manually closed by AP")
    return {"success": True}


# ── SharePoint helpers ─────────────────────────────────────────────────────────

def _get_sp_token() -> str:
    cache = msal.SerializableTokenCache()
    if os.path.exists(MSAL_CACHE_PATH):
        with open(MSAL_CACHE_PATH, "r") as f:
            cache.deserialize(f.read())

    app_msal = msal.PublicClientApplication(
        client_id=CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        token_cache=cache
    )

    accounts = app_msal.get_accounts()
    if accounts:
        result = app_msal.acquire_token_silent(scopes=SP_WRITE_SCOPES, account=accounts[0])
        if result and "access_token" in result:
            _save_cache(cache)
            return result["access_token"]

    result = app_msal.acquire_token_interactive(scopes=SP_WRITE_SCOPES)
    if "access_token" not in result:
        raise RuntimeError(f"SP auth failed: {result.get('error_description')}")
    _save_cache(cache)
    return result["access_token"]


def _get_graph_token() -> str:
    cache = msal.SerializableTokenCache()
    if os.path.exists(MSAL_CACHE_PATH):
        with open(MSAL_CACHE_PATH, "r") as f:
            cache.deserialize(f.read())

    app_msal = msal.PublicClientApplication(
        client_id=CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        token_cache=cache
    )

    accounts = app_msal.get_accounts()
    if accounts:
        result = app_msal.acquire_token_silent(scopes=GRAPH_SCOPES, account=accounts[0])
        if result and "access_token" in result:
            _save_cache(cache)
            return result["access_token"]

    result = app_msal.acquire_token_interactive(scopes=GRAPH_SCOPES)
    if "access_token" not in result:
        raise RuntimeError(f"Graph auth failed: {result.get('error_description')}")
    _save_cache(cache)
    return result["access_token"]


def _save_cache(cache: msal.SerializableTokenCache):
    if cache.has_state_changed:
        with open(MSAL_CACHE_PATH, "w") as f:
            f.write(cache.serialize())


def sp_get_items(site: str, list_name: str, filter_query: str = "") -> list:
    token = _get_sp_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json;odata=verbose",
    }
    url = (
        f"{site}/_api/web/lists/getbytitle"
        f"('{urllib.parse.quote(list_name)}')/items"
    )
    if filter_query:
        url += f"?$filter={urllib.parse.quote(filter_query)}"

    items = []
    while url:
        r = requests.get(url, headers=headers)
        if not r.ok:
            logger.error(f"SP get_items failed {r.status_code} on {list_name}: {r.text[:500]}")
        r.raise_for_status()
        data = r.json()
        results = data.get("d", {}).get("results", data.get("value", []))
        items.extend(results)
        url = data.get("d", {}).get("__next", data.get("@odata.nextLink"))
    return items


def sp_create_item(site: str, list_name: str, fields: dict) -> dict:
    token = _get_sp_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json;odata=verbose",
        "Content-Type": "application/json;odata=verbose",
    }
    meta_url = (
        f"{site}/_api/web/lists/getbytitle"
        f"('{urllib.parse.quote(list_name)}')?$select=ListItemEntityTypeFullName"
    )
    meta_r = requests.get(meta_url, headers=headers)
    meta_r.raise_for_status()
    entity_type = meta_r.json()["d"].get("ListItemEntityTypeFullName", "SP.Data.ListItem")

    body = {"__metadata": {"type": entity_type}, **fields}
    url = (
        f"{site}/_api/web/lists/getbytitle"
        f"('{urllib.parse.quote(list_name)}')/items"
    )
    headers["X-RequestDigest"] = _get_request_digest(site, token)
    r = requests.post(url, headers=headers, json=body)
    if not r.ok:
        logger.error(f"SP create_item failed {r.status_code} on {list_name}: {r.text[:500]}")
    r.raise_for_status()
    return r.json().get("d", r.json())


def sp_update_item(site: str, list_name: str, item_id: int, fields: dict):
    token = _get_sp_token()
    odata_headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json;odata=verbose",
    }
    meta_url = (
        f"{site}/_api/web/lists/getbytitle"
        f"('{urllib.parse.quote(list_name)}')?$select=ListItemEntityTypeFullName"
    )
    meta_r = requests.get(meta_url, headers=odata_headers)
    meta_r.raise_for_status()
    entity_type = meta_r.json()["d"].get("ListItemEntityTypeFullName", "SP.Data.ListItem")

    body = {"__metadata": {"type": entity_type}, **fields}
    url = (
        f"{site}/_api/web/lists/getbytitle"
        f"('{urllib.parse.quote(list_name)}')/items({item_id})"
    )
    headers = {
        "Authorization":    f"Bearer {token}",
        "Accept":           "application/json;odata=verbose",
        "Content-Type":     "application/json;odata=verbose",
        "IF-MATCH":         "*",
        "X-HTTP-Method":    "MERGE",
        "X-RequestDigest":  _get_request_digest(site, token),
    }
    r = requests.post(url, headers=headers, json=body)
    if not r.ok:
        logger.error(f"SP update_item failed {r.status_code} on {list_name}({item_id}): {r.text[:500]}")
    r.raise_for_status()


def _get_request_digest(site: str, token: str) -> str:
    r = requests.post(
        f"{site}/_api/contextinfo",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json;odata=verbose",
        }
    )
    r.raise_for_status()
    return r.json()["d"]["GetContextWebInformation"]["FormDigestValue"]


def load_config() -> dict:
    items = sp_get_items(REQ_SITE, LIST_CONFIG)
    # Title column displays as "ConfigKey" — internal name is Title
    return {item.get("Title", ""): item.get("ConfigValue", "") for item in items}


def load_roles() -> dict:
    items = sp_get_items(REQ_SITE, LIST_ROLES)
    # Title column displays as "RoleCode" — keyed by RoleCode value
    return {item.get("Title", ""): item for item in items}


def get_requisition(requisition_id: str) -> dict | None:
    # RequisitionID is a real Text column on Requisitions list — plain filter works
    items = sp_get_items(REQ_SITE, LIST_REQ, f"RequisitionID eq '{requisition_id}'")
    return items[0] if items else None


def get_line_items(requisition_id: str) -> list:
    # RequisitionID on Lines is a Lookup — must use /Title syntax
    return sp_get_items(
        REQ_SITE, LIST_LINES,
        f"RequisitionID/Title eq '{requisition_id}'"
    )


def write_history(
    requisition_id: str,
    sp_req_id: int,
    from_status: str,
    to_status: str,
    actor_emp_no: str,
    comment: str = ""
):
    """Write one status-transition row to Requisition Status History.
    sp_req_id is the SP integer ID of the parent Requisition row (required for Lookup field).
    """
    sp_create_item(REQ_SITE, LIST_HISTORY, {
        "Title":            f"{requisition_id}:{from_status}→{to_status}",
        "RequisitionIDId":  sp_req_id,              # Lookup → integer SP ID
        "FromStatus":       from_status,
        "ToStatus":         to_status,
        "TransitionUtc":    datetime.datetime.utcnow().isoformat() + "Z",
        "Comment":          comment,
    })


def generate_requisition_id(sp_item_id: int) -> str:
    year = datetime.datetime.utcnow().year
    return f"REQ-{year}-{sp_item_id:04d}"


def resolve_upn(emp_no: str) -> str:
    if not emp_no:
        return ""
    items = sp_get_items(
        CRD_SITE, LIST_EMPLOYEE,
        f"Employee_x0020_Number eq '{emp_no}'"
    )
    return items[0].get("M365_x0020_UPN", "") if items else ""


def resolve_catalogue_id(item_code: str) -> int | None:
    """Return the SP integer ID of a catalogue row by ItemCode (stored in Title column)."""
    if not item_code or item_code == "OTHER":
        return None
    items = sp_get_items(REQ_SITE, LIST_CATALOGUE, f"Title eq '{item_code}'")
    return items[0]["ID"] if items else None


# ── Routing helpers ────────────────────────────────────────────────────────────

def _resolve_ap_upn(requestor_emp_no: str, roles: dict) -> str:
    ap_role = roles.get("AP-Approver", {})
    primary = str(ap_role.get("PrimaryEmpNo", ""))
    backup  = str(ap_role.get("BackupEmpNo", ""))
    if str(requestor_emp_no) == primary:
        return resolve_upn(backup)
    return resolve_upn(primary)


def _resolve_fulfill_upn(requestor_emp_no: str, roles: dict) -> str:
    fulfill_role = roles.get("Fulfillment", {})
    primary = str(fulfill_role.get("PrimaryEmpNo", ""))
    backup  = str(fulfill_role.get("BackupEmpNo", ""))
    if str(requestor_emp_no) == primary:
        if backup:
            return resolve_upn(backup)
        ap_role = roles.get("AP-Approver", {})
        return resolve_upn(str(ap_role.get("PrimaryEmpNo", "")))
    return resolve_upn(primary)


# ── Email helpers ──────────────────────────────────────────────────────────────

def send_email(sender: str, to: str, cc: str, subject: str, body: str):
    if not to:
        logger.warning(f"send_email called with empty To. Subject: {subject}")
        return

    token = _get_graph_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }
    to_recipients = [{"emailAddress": {"address": a.strip()}}
                     for a in to.split(",") if a.strip()]
    cc_recipients = [{"emailAddress": {"address": a.strip()}}
                     for a in cc.split(",") if a.strip()]

    message = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body},
            "toRecipients": to_recipients,
            "ccRecipients": cc_recipients,
        }
    }
    r = requests.post(
        "https://graph.microsoft.com/v1.0/me/sendMail",
        headers=headers, json=message
    )
    r.raise_for_status()


def _make_approval_token(
    requisition_id, expected_status, actor_emp_no, actor_name
) -> str:
    payload = {
        "requisitionID":  requisition_id,
        "expectedStatus": expected_status,
        "actorEmpNo":     actor_emp_no,
        "actorName":      actor_name,
        "exp":            int(time.time()) + (APPROVAL_TOKEN_EXPIRY_HOURS * 3600),
    }
    return jwt.encode(payload, TOKEN_SECRET, algorithm="HS256")


def _approval_links(
    requisition_id, expected_status, actor_emp_no, actor_name
) -> tuple:
    token = _make_approval_token(
        requisition_id, expected_status, actor_emp_no, actor_name
    )
    base = f"{FUNCTION_BASE_URL}/api/approve?token={urllib.parse.quote(token)}"
    return f"{base}&action=approve", f"{base}&action=reject"


def _approval_buttons(approve_url: str, reject_url: str) -> str:
    """
    Approval action links. Rendered as plain hyperlinks — Outlook Safe Links
    rewrites long JWT URLs in a way that breaks styled button rendering.
    Plain <a> tags are reliable across all Outlook versions and Safe Links.
    """
    return (
        "<table cellpadding='0' cellspacing='0' border='0' style='margin:20px 0;border-left:4px solid #0078D4;padding-left:12px'>"
        "<tr><td style='padding:6px 0;font-family:Segoe UI,Arial,sans-serif;font-size:14px'>"
        f"<a href='{approve_url}' style='color:#107C10;font-weight:700;text-decoration:none;'>&#10003; Approve this requisition</a>"
        "</td></tr>"
        "<tr><td style='padding:6px 0;font-family:Segoe UI,Arial,sans-serif;font-size:14px'>"
        f"<a href='{reject_url}' style='color:#A4262C;font-weight:700;text-decoration:none;'>&#10007; Reject this requisition</a>"
        "</td></tr>"
        "</table>"
        "<p style='font-size:11px;color:#757575;margin-top:4px'>"
        "If the links above do not work, copy and paste the URL from your browser address bar after clicking.<br>"
        "Approve: <span style='word-break:break-all'>" + approve_url[:60] + "…</span>"
        "</p>"
    )


def _line_items_table(line_items: list) -> str:
    def _v(item, *keys, default=0):
        for k in keys:
            if k in item and item[k] not in (None, ""):
                return item[k]
        return default

    rows = "".join(
        f"<tr>"
        f"<td style='padding:6px 12px;border:1px solid #e0e0e0'>{_v(item, 'itemDescription', 'ItemDescription', default='')}</td>"
        f"<td style='padding:6px 12px;border:1px solid #e0e0e0;text-align:center'>{_v(item, 'quantity', 'Quantity', default=1)}</td>"
        f"<td style='padding:6px 12px;border:1px solid #e0e0e0;text-align:right'>${float(_v(item, 'unitPriceEstimate', 'UnitPriceEstimate')):,.2f}</td>"
        f"<td style='padding:6px 12px;border:1px solid #e0e0e0;text-align:right'>${float(_v(item, 'quantity', 'Quantity', default=1))*float(_v(item, 'unitPriceEstimate', 'UnitPriceEstimate')):,.2f}</td>"
        f"</tr>"
        for item in line_items
    )
    return f"""
    <table style='border-collapse:collapse;width:100%;font-size:13px;margin:12px 0'>
      <thead>
        <tr style='background:#0078D4;color:white'>
          <th style='padding:8px 12px;text-align:left'>Description</th>
          <th style='padding:8px 12px;text-align:center'>Qty</th>
          <th style='padding:8px 12px;text-align:right'>Unit Est.</th>
          <th style='padding:8px 12px;text-align:right'>Total Est.</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>"""


def _email_wrapper(content: str) -> str:
    return f"""
    <html><body style='font-family:Segoe UI,Arial,sans-serif;color:#212121;max-width:680px;margin:0 auto'>
      <div style='background:#0078D4;padding:16px 24px'>
        <span style='color:white;font-size:18px;font-weight:600'>Macrodyne Technologies</span>
        <span style='color:#c7e0f4;font-size:13px;margin-left:12px'>IT Requisition System</span>
      </div>
      <div style='padding:24px'>
        {content}
        <hr style='border:none;border-top:1px solid #e0e0e0;margin:24px 0'>
        <p style='font-size:11px;color:#757575'>
          This is an automated notification from the Macrodyne IT Requisition System.
          All items are delivered to the reception desk.
          Do not reply to this email.
        </p>
      </div>
    </body></html>"""


def _send_manager_approval_email(
    requisition_id, requestor_name, dept, reason,
    total, currency, line_items, submitted_utc,
    manager_emp_no, manager_upn, notification_sender, roles
):
    approve_url, reject_url = _approval_links(
        requisition_id, "PendingManager", manager_emp_no, ""
    )
    body = _email_wrapper(f"""
        <h2 style='color:#0078D4;margin-top:0'>Approval Required</h2>
        <p>A purchase requisition requires your approval.</p>
        <table style='font-size:13px'>
          <tr><td style='padding:4px 16px 4px 0;color:#757575'>Requisition</td>
              <td><strong>{requisition_id}</strong></td></tr>
          <tr><td style='padding:4px 16px 4px 0;color:#757575'>Requested by</td>
              <td>{requestor_name}{f' — {dept}' if dept else ''}</td></tr>
          <tr><td style='padding:4px 16px 4px 0;color:#757575'>Reason</td>
              <td>{reason}</td></tr>
          <tr><td style='padding:4px 16px 4px 0;color:#757575'>Total estimate</td>
              <td><strong>{currency} ${total:,.2f}</strong></td></tr>
        </table>
        {_line_items_table(line_items)}
        {_approval_buttons(approve_url, reject_url)}
        <p style='font-size:12px;color:#757575'>
          Approval links expire in {APPROVAL_TOKEN_EXPIRY_HOURS} hours.
        </p>""")

    send_email(sender=notification_sender, to=manager_upn, cc="",
               subject=f"Approval required — {requisition_id}", body=body)


def _send_ap_approval_email(
    requisition_id, requestor_name, dept, reason,
    total, currency, line_items, submitted_utc,
    ap_upn, notification_sender, manager_skipped=False
):
    roles = load_roles()
    ap_role   = roles.get("AP-Approver", {})
    ap_emp_no = str(ap_role.get("PrimaryEmpNo", ""))

    approve_url, reject_url = _approval_links(
        requisition_id, "PendingAP", ap_emp_no, ""
    )
    skip_note = (
        "<p style='color:#757575;font-size:12px'>"
        "Note: Manager approval step was skipped.</p>"
        if manager_skipped else ""
    )
    body = _email_wrapper(f"""
        <h2 style='color:#0078D4;margin-top:0'>AP Approval Required</h2>
        {skip_note}
        <table style='font-size:13px'>
          <tr><td style='padding:4px 16px 4px 0;color:#757575'>Requisition</td>
              <td><strong>{requisition_id}</strong></td></tr>
          <tr><td style='padding:4px 16px 4px 0;color:#757575'>Requested by</td>
              <td>{requestor_name}{f' — {dept}' if dept else ''}</td></tr>
          <tr><td style='padding:4px 16px 4px 0;color:#757575'>Reason</td>
              <td>{reason}</td></tr>
          <tr><td style='padding:4px 16px 4px 0;color:#757575'>Total estimate</td>
              <td><strong>{currency} ${total:,.2f}</strong></td></tr>
        </table>
        {_line_items_table(line_items)}
        {_approval_buttons(approve_url, reject_url)}""")

    send_email(sender=notification_sender, to=ap_upn, cc="",
               subject=f"AP approval required — {requisition_id}", body=body)


def _send_fulfillment_email(
    requisition_id, requestor_name, requestor_upn, reason,
    total, currency, line_items, approved_utc,
    fulfill_upn, manager_upn, notification_sender
):
    body = _email_wrapper(f"""
        <h2 style='color:#0078D4;margin-top:0'>Ready to Purchase</h2>
        <table style='font-size:13px'>
          <tr><td style='padding:4px 16px 4px 0;color:#757575'>Requisition</td>
              <td><strong>{requisition_id}</strong></td></tr>
          <tr><td style='padding:4px 16px 4px 0;color:#757575'>Requested by</td>
              <td>{requestor_name}</td></tr>
          <tr><td style='padding:4px 16px 4px 0;color:#757575'>Reason</td>
              <td>{reason}</td></tr>
          <tr><td style='padding:4px 16px 4px 0;color:#757575'>Total estimate</td>
              <td><strong>{currency} ${total:,.2f}</strong></td></tr>
        </table>
        {_line_items_table(line_items)}
        <p>Please process this order and mark it as Ordered in the Requisitions app once purchased.</p>""")

    cc_parts = [requestor_upn, manager_upn]
    cc = ",".join(p for p in cc_parts if p)
    send_email(sender=notification_sender, to=fulfill_upn, cc=cc,
               subject=f"Ready to purchase — {requisition_id}", body=body)


def _ordered_email_body(requisition_id, requestor_name, payment_mode, ordered_utc):
    return _email_wrapper(f"""
        <h2 style='color:#0078D4;margin-top:0'>Your Requisition Has Been Ordered</h2>
        <table style='font-size:13px'>
          <tr><td style='padding:4px 16px 4px 0;color:#757575'>Requisition</td>
              <td><strong>{requisition_id}</strong></td></tr>
          <tr><td style='padding:4px 16px 4px 0;color:#757575'>Ordered</td>
              <td>{ordered_utc[:10]}</td></tr>
          <tr><td style='padding:4px 16px 4px 0;color:#757575'>Payment</td>
              <td>{payment_mode}</td></tr>
        </table>
        <p>You will receive a notification when your order arrives at the reception desk.</p>""")


def _received_email_body(requisition_id, requestor_name, received_utc, receipt_days):
    return _email_wrapper(f"""
        <h2 style='color:#0078D4;margin-top:0'>Your Order Has Arrived</h2>
        <p>Your order is ready for pickup at the reception desk.</p>
        <table style='font-size:13px'>
          <tr><td style='padding:4px 16px 4px 0;color:#757575'>Requisition</td>
              <td><strong>{requisition_id}</strong></td></tr>
          <tr><td style='padding:4px 16px 4px 0;color:#757575'>Arrived</td>
              <td>{received_utc[:10]}</td></tr>
        </table>
        <p>Please confirm receipt in the Requisitions app within
           <strong>{receipt_days} business days</strong>.</p>""")


def _rejection_email_body(requisition_id, requestor_name, reason):
    return _email_wrapper(f"""
        <h2 style='color:#A4262C;margin-top:0'>Requisition Not Approved</h2>
        <table style='font-size:13px'>
          <tr><td style='padding:4px 16px 4px 0;color:#757575'>Requisition</td>
              <td><strong>{requisition_id}</strong></td></tr>
          <tr><td style='padding:4px 16px 4px 0;color:#757575'>Reason</td>
              <td>{reason or 'No reason provided.'}</td></tr>
        </table>
        <p>You may re-submit with adjustments, or contact Finance if this purchase is business-critical.</p>""")


def _rejected_at_purchase_email_body(requisition_id, requestor_name, reason):
    return _email_wrapper(f"""
        <h2 style='color:#A4262C;margin-top:0'>Requisition Could Not Be Purchased</h2>
        <table style='font-size:13px'>
          <tr><td style='padding:4px 16px 4px 0;color:#757575'>Requisition</td>
              <td><strong>{requisition_id}</strong></td></tr>
          <tr><td style='padding:4px 16px 4px 0;color:#757575'>Reason</td>
              <td>{reason or 'No reason provided.'}</td></tr>
        </table>
        <p>Please re-submit with an updated estimate, or contact Finance to proceed via the formal PO process.</p>""")


def _html_page(title: str, message: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} — Macrodyne Requisitions</title>
  <style>
    body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #f5f5f5;
            display: flex; align-items: center; justify-content: center;
            min-height: 100vh; margin: 0; }}
    .card {{ background: white; border-radius: 8px; padding: 40px;
             max-width: 480px; width: 90%; box-shadow: 0 2px 8px rgba(0,0,0,.1); }}
    .header {{ background: #0078D4; color: white; padding: 12px 20px;
               border-radius: 8px 8px 0 0; margin: -40px -40px 24px; }}
    h1 {{ margin: 0; font-size: 16px; font-weight: 600; }}
    .sub {{ font-size: 12px; opacity: .8; }}
    h2 {{ color: #0078D4; margin-top: 0; }}
    p {{ color: #424242; line-height: 1.5; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <h1>Macrodyne Technologies</h1>
      <div class="sub">IT Requisition System</div>
    </div>
    <h2>{title}</h2>
    <p>{message}</p>
  </div>
</body>
</html>"""


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
