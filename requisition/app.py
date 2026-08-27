"""
app.py
======
HTTP layer for the Requisition portal.

Contract:
  - Parses requests, calls workflow, shapes JSON responses.
  - Contains no business rules and no SharePoint knowledge.
  - Translates the two exception types into the right status codes:
      WorkflowError -> 400, message shown to the user
      DaoError      -> 502, generic message to the user, detail to the log

Imports workflow and dao (for DaoError only). Never imports config directly —
anything the HTTP layer needs to know is exposed through workflow.

Deployment: 2026-08-21-v7.0
"""

import json
import logging

from flask import Flask, request, Response, jsonify

from dao import DaoError
from workflow import WorkflowError
import workflow

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)


# ── Cross-origin support ──────────────────────────────────────────────────────
# The portal is served from Azure Static Web Apps on a different origin to this
# API, so every response needs CORS headers — including the preflight OPTIONS
# that never reaches a route handler.

@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


@app.route("/api/<path:_any>", methods=["OPTIONS"])
def handle_preflight(_any):
    """Answer any preflight request. The after_request hook adds the headers."""
    return Response("", status=200)


# ── Response helpers ──────────────────────────────────────────────────────────

def _ok(payload):
    body = {"success": True}
    body.update(payload or {})
    return Response(json.dumps(body), status=200, mimetype="application/json")


def _fail(message, status=400):
    return Response(
        json.dumps({"success": False, "error": message}),
        status=status, mimetype="application/json",
    )


def _run(operation_name, fn):
    """Run one operation with uniform error handling.

    Business rule violations come back as 400 with the rule's own wording.
    Storage failures come back as 502 with a generic message, because the
    detail belongs in the log rather than on a user's screen.
    """
    try:
        return _ok(fn())
    except WorkflowError as e:
        logger.info(f"{operation_name} refused: {e}")
        return _fail(str(e), 400)
    except DaoError as e:
        logger.error(f"{operation_name} failed at data layer: {e}")
        return _fail(
            "The requisition service could not reach its data store. "
            "Please try again shortly or contact IT if this persists.",
            502,
        )
    except Exception as e:
        logger.exception(f"{operation_name} raised an unexpected error")
        return _fail(
            "An unexpected error occurred. IT has been notified.", 500
        )


def _actor_from(upn):
    """Resolve the signed-in user into an actor record.

    The portal proves identity with MSAL in the browser and passes the UPN.
    Everything downstream is authorised against Employee Master and the
    Requisition Roles list, so a forged UPN gains only that person's own
    permissions — not elevated ones.
    """
    return workflow.identify_actor(upn)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    """Liveness probe for App Service. Deliberately touches nothing."""
    return jsonify({"status": "ok"})


@app.route("/api/portal", methods=["GET"])
def get_portal():
    """Everything the portal needs for one signed-in user in a single call."""
    upn = request.args.get("upn", "").strip()
    if not upn:
        return _fail("Sign-in identity is missing.")

    def operation():
        return workflow.build_portal_view(_actor_from(upn))

    return _run("Portal load", operation)


@app.route("/api/requisition/<requisition_id>", methods=["GET"])
def get_requisition_detail(requisition_id):
    """Full detail for one requisition, with the actions this user may take."""
    upn = request.args.get("upn", "").strip()
    if not upn:
        return _fail("Sign-in identity is missing.")

    def operation():
        return workflow.build_detail_view(requisition_id, _actor_from(upn))

    return _run(f"Detail load for {requisition_id}", operation)


@app.route("/api/catalogue", methods=["GET"])
def get_catalogue():
    """Item catalogue for the submission form's picker."""
    def operation():
        return {"items": workflow.list_catalogue()}

    return _run("Catalogue load", operation)


@app.route("/api/requisition", methods=["POST"])
def post_action():
    """Single entry point for every state-changing operation.

    The action name in the payload selects the handler. Each handler
    re-authorises against the current state of the requisition, so a stale
    page cannot force an invalid transition.
    """
    try:
        payload = request.get_json(force=True) or {}
    except Exception:
        return _fail("Request body was not valid JSON.")

    action = payload.get("action", "")
    upn    = (payload.get("upn") or "").strip()

    if not action:
        return _fail("No action was specified.")
    if not upn:
        return _fail("Sign-in identity is missing.")

    handler = ACTION_HANDLERS.get(action)
    if not handler:
        return _fail(f"Unknown action: {action}")

    def operation():
        return handler(_actor_from(upn), payload)

    return _run(f"Action {action}", operation)


# ── Action handlers ───────────────────────────────────────────────────────────
# Each one pulls what it needs from the payload and delegates to workflow.
# They exist to keep payload parsing out of the workflow layer.

def _handle_submit(actor, payload):
    return workflow.submit(
        actor,
        currency   = payload.get("currency", "CAD"),
        reason     = payload.get("reason", ""),
        line_items = payload.get("lineItems", []),
    )


def _handle_approve(actor, payload):
    return workflow.approve(
        payload.get("requisitionID", ""), actor,
        comment=payload.get("comment", ""),
    )


def _handle_reject(actor, payload):
    return workflow.reject(
        payload.get("requisitionID", ""), actor,
        comment=payload.get("comment", ""),
    )


def _handle_cancel(actor, payload):
    return workflow.cancel(
        payload.get("requisitionID", ""), actor,
        comment=payload.get("comment", ""),
    )


def _handle_mark_ordered(actor, payload):
    return workflow.mark_ordered(
        payload.get("requisitionID", ""), actor,
        payment_mode = payload.get("paymentMode", ""),
        comment      = payload.get("comment", ""),
        line_updates = payload.get("lineUpdates", []),
    )


def _handle_mark_received(actor, payload):
    return workflow.mark_received(payload.get("requisitionID", ""), actor)


def _handle_confirm_handover(actor, payload):
    return workflow.confirm_handover(
        payload.get("requisitionID", ""), actor,
        comment=payload.get("comment", ""),
    )


def _handle_reject_at_purchase(actor, payload):
    return workflow.reject_at_purchase(
        payload.get("requisitionID", ""), actor,
        comment=payload.get("comment", ""),
    )


# Action names match the strings returned by workflow.allowed_actions, so the
# buttons the portal renders map one-to-one onto the handlers here.
ACTION_HANDLERS = {
    "Submit":           _handle_submit,
    "Approve":          _handle_approve,
    "Reject":           _handle_reject,
    "Cancel":           _handle_cancel,
    "MarkOrdered":      _handle_mark_ordered,
    "MarkReceived":     _handle_mark_received,
    "ConfirmHandover":  _handle_confirm_handover,
    "RejectAtPurchase": _handle_reject_at_purchase,
}


# ── Local development entry point ─────────────────────────────────────────────
# In Azure this module is served by gunicorn, which imports `app` directly and
# never runs this block.

if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
