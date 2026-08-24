"""
notifications.py
================
Email composition and sending.

Contract:
  - Each public function takes explicit values and sends one email.
  - No SharePoint reads. If an address or a role is needed, the caller
    resolves it and passes it in. That keeps this module free of the
    routing rules that live in workflow.py.
  - Failures propagate as DaoError from dao.send_mail. The caller decides
    whether a failed notification should fail the whole operation.

Imports config and dao only.
"""

import logging

import config
import dao

logger = logging.getLogger(__name__)

# ── Shared markup ─────────────────────────────────────────────────────────────

BRAND_BLUE = "#0078D4"
GREY_TEXT  = "#757575"
RED        = "#A4262C"


def _escape(text):
    """Minimal HTML escaping for values that came from user input."""
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _money(amount, currency=""):
    """Format a number as currency for display in an email."""
    try:
        formatted = f"{float(amount):,.2f}"
    except (TypeError, ValueError):
        formatted = "0.00"
    return f"{currency} ${formatted}".strip()


def _field(label, value):
    """One label/value row in the summary table at the top of each email."""
    return (
        f"<tr>"
        f"<td style='padding:4px 16px 4px 0;color:{GREY_TEXT}'>{label}</td>"
        f"<td>{value}</td>"
        f"</tr>"
    )


def _summary_table(rows_html):
    return f"<table style='font-size:13px'>{rows_html}</table>"


def _line_items_table(line_items):
    """Render line items.

    Accepts either the camelCase dicts the submission form sends or the
    PascalCase rows read back from SharePoint, because both shapes reach
    this function depending on which workflow step is sending the email.
    """
    rows = []
    for item in line_items:
        description = item.get("itemDescription") or item.get("ItemDescription") or ""
        quantity    = item.get("quantity")         or item.get("Quantity")         or 1
        unit_price  = item.get("unitPriceEstimate") or item.get("UnitPriceEstimate") or 0

        try:
            qty   = float(quantity)
            price = float(unit_price)
        except (TypeError, ValueError):
            qty, price = 1.0, 0.0

        cell = "padding:6px 12px;border:1px solid #e0e0e0"
        rows.append(
            f"<tr>"
            f"<td style='{cell}'>{_escape(description)}</td>"
            f"<td style='{cell};text-align:center'>{qty:g}</td>"
            f"<td style='{cell};text-align:right'>${price:,.2f}</td>"
            f"<td style='{cell};text-align:right'>${qty * price:,.2f}</td>"
            f"</tr>"
        )

    header_cell = "padding:8px 12px"
    return f"""
    <table style='border-collapse:collapse;width:100%;font-size:13px;margin:12px 0'>
      <thead>
        <tr style='background:{BRAND_BLUE};color:white'>
          <th style='{header_cell};text-align:left'>Description</th>
          <th style='{header_cell};text-align:center'>Qty</th>
          <th style='{header_cell};text-align:right'>Unit Est.</th>
          <th style='{header_cell};text-align:right'>Total Est.</th>
        </tr>
      </thead>
      <tbody>{''.join(rows)}</tbody>
    </table>"""


def _portal_link(requisition_id):
    """A plain visible link to the portal.

    Outlook Safe Links rewrites href targets, which breaks button-style links
    with long query strings. Showing the URL as its own text means the
    recipient can copy it if the click is mangled.
    """
    url = f"{config.PORTAL_BASE_URL}?req={requisition_id}"
    return (
        "<div style='margin:20px 0;padding:16px;background:#EBF3FB;"
        f"border-left:4px solid {BRAND_BLUE};border-radius:4px'>"
        "<p style='margin:0 0 8px 0;font-size:14px'>"
        "Open this requisition in the portal:</p>"
        f"<a href='{url}' style='font-size:14px;font-weight:700;color:#0063B1'>{url}</a>"
        "</div>"
    )


def _wrapper(content_html):
    """Macrodyne-branded shell around every email body."""
    return f"""
    <html><body style='font-family:Segoe UI,Arial,sans-serif;color:#212121;
                       max-width:680px;margin:0 auto'>
      <div style='background:{BRAND_BLUE};padding:16px 24px'>
        <span style='color:white;font-size:18px;font-weight:600'>Macrodyne Technologies</span>
        <span style='color:#c7e0f4;font-size:13px;margin-left:12px'>IT Requisition System</span>
      </div>
      <div style='padding:24px'>
        {content_html}
        <hr style='border:none;border-top:1px solid #e0e0e0;margin:24px 0'>
        <p style='font-size:11px;color:{GREY_TEXT}'>
          Automated notification from the Macrodyne IT Requisition System.
          All items are delivered to the reception desk. Do not reply to this email.
        </p>
      </div>
    </body></html>"""


# ── Public senders ────────────────────────────────────────────────────────────

def send_approval_request(requisition_id, requestor_name, department, reason,
                          total, currency, line_items, approver_upn,
                          is_ap_stage, manager_was_skipped=False):
    """Ask an approver to action a requisition.

    is_ap_stage picks the wording; the mechanics are identical for both
    manager and AP approval, so one function serves both.
    """
    heading = "AP Approval Required" if is_ap_stage else "Approval Required"
    skip_note = (
        f"<p style='color:{GREY_TEXT};font-size:12px'>"
        "Manager approval was not required for this amount.</p>"
        if manager_was_skipped else ""
    )
    requester_line = _escape(requestor_name)
    if department:
        requester_line += f" — {_escape(department)}"

    body = _wrapper(f"""
        <h2 style='color:{BRAND_BLUE};margin-top:0'>{heading}</h2>
        {skip_note}
        {_summary_table(
            _field("Requisition", f"<strong>{requisition_id}</strong>")
            + _field("Requested by", requester_line)
            + _field("Reason", _escape(reason))
            + _field("Total estimate", f"<strong>{_money(total, currency)}</strong>")
        )}
        {_line_items_table(line_items)}
        {_portal_link(requisition_id)}""")

    subject = (
        f"AP approval required — {requisition_id}" if is_ap_stage
        else f"Approval required — {requisition_id}"
    )
    dao.send_mail(subject, body, [approver_upn])


def send_ready_to_purchase(requisition_id, requestor_name, reason, total,
                           currency, line_items, fulfiller_upn,
                           copy_to_addresses):
    """Tell the fulfillment role a requisition is approved and ready to buy."""
    body = _wrapper(f"""
        <h2 style='color:{BRAND_BLUE};margin-top:0'>Ready to Purchase</h2>
        {_summary_table(
            _field("Requisition", f"<strong>{requisition_id}</strong>")
            + _field("Requested by", _escape(requestor_name))
            + _field("Reason", _escape(reason))
            + _field("Total estimate", f"<strong>{_money(total, currency)}</strong>")
        )}
        {_line_items_table(line_items)}
        <p>Please place this order and mark it as Ordered once purchased.</p>
        {_portal_link(requisition_id)}""")

    dao.send_mail(
        f"Ready to purchase — {requisition_id}",
        body, [fulfiller_upn], copy_to_addresses,
    )


def send_ordered(requisition_id, payment_mode, ordered_date, note,
                 requestor_upn, copy_to_addresses):
    """Tell the requestor how their request was satisfied.

    The note carries what purchasing actually did, which may be issuing an
    item already in stock rather than buying one, so the wording stays
    neutral about whether a purchase took place.
    """
    body = _wrapper(f"""
        <h2 style='color:{BRAND_BLUE};margin-top:0'>Your Request Is Being Handled</h2>
        {_summary_table(
            _field("Requisition", f"<strong>{requisition_id}</strong>")
            + _field("Date", ordered_date[:10])
            + _field("Payment", _escape(payment_mode))
            + _field("What was done", _escape(note))
        )}
        <p>You will be notified when it is ready to collect from reception.</p>""")

    dao.send_mail(
        f"Your requisition is being handled — {requisition_id}",
        body, [requestor_upn], copy_to_addresses,
    )


def send_received(requisition_id, received_date, confirm_within_days,
                  requestor_upn, copy_to_addresses):
    """Tell the requestor their order has arrived and needs confirming."""
    body = _wrapper(f"""
        <h2 style='color:{BRAND_BLUE};margin-top:0'>Your Order Has Arrived</h2>
        <p>Your order is ready for pickup at the reception desk.</p>
        {_summary_table(
            _field("Requisition", f"<strong>{requisition_id}</strong>")
            + _field("Arrived", received_date[:10])
        )}
        <p>Please confirm receipt within
           <strong>{confirm_within_days} business days</strong>.</p>
        {_portal_link(requisition_id)}""")

    dao.send_mail(
        f"Your order has arrived — {requisition_id}",
        body, [requestor_upn], copy_to_addresses,
    )


def send_receipt_confirmed(requisition_id, requestor_name,
                           fulfiller_upn, copy_to_addresses):
    """Tell fulfillment the requestor confirmed receipt and the loop is closed."""
    body = _wrapper(f"""
        <h2 style='color:{BRAND_BLUE};margin-top:0'>Receipt Confirmed</h2>
        <p>{_escape(requestor_name)} has confirmed receipt of their order for
           requisition <strong>{requisition_id}</strong>.
           This requisition is now closed.</p>""")

    dao.send_mail(
        f"Receipt confirmed — {requisition_id}",
        body, [fulfiller_upn], copy_to_addresses,
    )


def send_rejected(requisition_id, reason, requestor_upn, copy_to_addresses):
    """Tell the requestor an approver declined the requisition."""
    body = _wrapper(f"""
        <h2 style='color:{RED};margin-top:0'>Requisition Not Approved</h2>
        {_summary_table(
            _field("Requisition", f"<strong>{requisition_id}</strong>")
            + _field("Reason", _escape(reason) or "No reason provided.")
        )}
        <p>You may re-submit with adjustments, or contact Finance if this
           purchase is business-critical.</p>""")

    dao.send_mail(
        f"Requisition not approved — {requisition_id}",
        body, [requestor_upn], copy_to_addresses,
    )


def send_rejected_at_purchase(requisition_id, reason,
                              requestor_upn, copy_to_addresses):
    """Tell the requestor the item could not be bought at the approved price."""
    body = _wrapper(f"""
        <h2 style='color:{RED};margin-top:0'>Requisition Could Not Be Purchased</h2>
        {_summary_table(
            _field("Requisition", f"<strong>{requisition_id}</strong>")
            + _field("Reason", _escape(reason) or "No reason provided.")
        )}
        <p>Please re-submit with an updated estimate, or contact Finance to
           proceed via the formal PO process.</p>""")

    dao.send_mail(
        f"Requisition could not be purchased — {requisition_id}",
        body, [requestor_upn], copy_to_addresses,
    )


def send_cancelled(requisition_id, notify_upn, copy_to_addresses):
    """Tell whoever was waiting on this requisition that it has been withdrawn."""
    body = _wrapper(f"""
        <h2 style='color:{GREY_TEXT};margin-top:0'>Requisition Cancelled</h2>
        <p>Requisition <strong>{requisition_id}</strong> has been cancelled by
           the requestor. No further action is required.</p>""")

    dao.send_mail(
        f"Requisition cancelled — {requisition_id}",
        body, [notify_upn], copy_to_addresses,
    )
