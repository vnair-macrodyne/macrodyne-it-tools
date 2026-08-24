"""
workflow.py
===========
Business rules for the requisition lifecycle: who someone is, what they are
allowed to do, and what happens when they do it.

Contract:
  - Takes and returns plain dicts describing requisitions and people.
  - Calls dao for persistence and notifications for email.
  - Knows nothing about HTTP. No Flask imports, no request objects.
  - Raises WorkflowError for rule violations the caller should show the user.
    Lets DaoError propagate — a storage failure is not a business rule failure
    and the two deserve different handling upstream.

Imports config, dao, notifications.
"""

import logging

import config
import dao
import notifications

logger = logging.getLogger(__name__)


class WorkflowError(Exception):
    """Raised when an operation is refused by a business rule.

    The message is written to be shown to the end user.
    """


# ── Actor identity and role ───────────────────────────────────────────────────

def identify_actor(upn):
    """Build the actor record used by every permission decision.

    Returns a dict with the person's identity plus the role flags derived from
    the Requisition Roles list. Raises WorkflowError when the UPN is not in
    Employee Master — that is a data problem the user needs told about, not a
    silent fallback to fewer permissions.
    """
    if not upn:
        raise WorkflowError("No sign-in identity was supplied.")

    employee = dao.get_employee_by_upn(upn)
    if not employee:
        raise WorkflowError(
            f"{upn} was not found in the employee directory. "
            "Please contact IT to be set up for requisitions."
        )

    roles   = dao.load_roles()
    emp_no  = str(employee.get(config.COL_EMP_NUMBER, ""))

    return {
        "upn":            upn,
        "emp_no":         emp_no,
        "full_name":      employee.get(config.COL_EMP_NAME, "") or upn,
        "department":     employee.get(config.COL_EMP_DEPT, ""),
        "manager_emp_no": str(employee.get(config.COL_EMP_MGR, "") or ""),
        "is_finance_low":  _holds_role(emp_no, roles, config.ROLE_FINANCE_LOW),
        "is_finance_high": _holds_role(emp_no, roles, config.ROLE_FINANCE_HIGH),
        "is_fulfillment":  _holds_role(emp_no, roles, config.ROLE_FULFILLMENT),
    }


def _holds_role(emp_no, roles, role_code):
    """True when this employee is the named holder of a role.

    Only the primary counts. Cover during an absence comes from an explicit
    delegation, not from a standing backup, so that there is exactly one
    answer to who holds a role at any moment.
    """
    if not emp_no:
        return False
    holder = str(roles.get(role_code, {}).get("PrimaryEmpNo", "") or "")
    return bool(holder) and str(emp_no) == holder


# ── Routing: who receives the next notification ───────────────────────────────

def finance_authority_emp_no(total, currency, roles, settings):
    """Whose sign-off releases this amount of spend.

    Below the threshold the Director of Finance decides; at or above it the CFO
    does. There is no backup — cover during an absence is arranged through an
    explicit delegation rather than a standing rule.
    """
    role_code = _finance_role_for(total, settings)
    holder    = str(roles.get(role_code, {}).get("PrimaryEmpNo", "") or "")

    if not holder:
        raise WorkflowError(
            f"No {role_code} is configured. Please contact IT."
        )
    return holder


def _finance_role_for(total, settings):
    """Which finance role owns this amount."""
    threshold = _as_float(
        settings.get(config.CFG_FINANCE_THRESHOLD),
        config.DEFAULT_FINANCE_THRESHOLD,
    )
    return (
        config.ROLE_FINANCE_HIGH if total >= threshold
        else config.ROLE_FINANCE_LOW
    )


def is_terminal_authority(emp_no, roles):
    """True when this person is the CFO.

    The CFO is the last word on spend, so their own requisitions have nobody
    left to approve them and release directly to purchasing.
    """
    cfo = str(roles.get(config.ROLE_FINANCE_HIGH, {}).get("PrimaryEmpNo", "") or "")
    return bool(cfo) and str(emp_no) == cfo


def finance_notify_emp_no(total, currency, roles, settings):
    """The Director of Finance, when they are not the one approving.

    Above the threshold the CFO decides, but Finance still needs sight of the
    spend. Returns an empty string when the Director is already the approver.
    """
    threshold = _as_float(
        settings.get(config.CFG_FINANCE_THRESHOLD),
        config.DEFAULT_FINANCE_THRESHOLD,
    )
    if total < threshold:
        return ""      # the Director is the approver, so no separate copy

    return str(roles.get(config.ROLE_FINANCE_LOW, {}).get("PrimaryEmpNo", "") or "")


def _fulfiller_upn(requestor_emp_no, roles):
    """Who buys the items for a requisition.

    Purchasing is a task, not an approval, so the fulfillment holder can act on
    their own requisition without a conflict of interest. That keeps the path
    simple and means a fulfillment absence is covered by delegation rather than
    by a special case here.
    """
    holder = str(roles.get(config.ROLE_FULFILLMENT, {}).get("PrimaryEmpNo", "") or "")
    if not holder:
        raise WorkflowError(
            "No fulfillment actor is configured. Please contact IT."
        )
    return dao.get_upn_for_emp_no(holder)


# ── Permissions ───────────────────────────────────────────────────────────────

def allowed_actions(requisition, actor):
    """Return the list of actions this actor may take on this requisition now.

    One place decides permissions. The portal uses it to render buttons and the
    action handlers use it to authorise, so the UI can never offer something the
    backend would refuse.
    """
    status         = requisition.get("Status", "")
    requestor_upn  = (requisition.get("RequestorUPN") or "").lower()
    manager_upn    = (requisition.get("ManagerUPN") or "").lower()
    actor_upn      = actor["upn"].lower()

    is_requestor = actor_upn == requestor_upn and requestor_upn != ""
    is_manager   = actor_upn == manager_upn and manager_upn != ""

    actions = []

    if status == config.STATUS_PENDING_MANAGER and is_manager:
        actions += ["Approve", "Reject"]

    elif status == config.STATUS_PENDING_AP:
        # The finance gate. Which role may act depends on the amount, so the
        # requisition itself tells us who to expect.
        if _is_finance_approver_for(requisition, actor):
            actions += ["Approve", "Reject"]

    elif status == config.STATUS_APPROVED_PENDING and actor["is_fulfillment"]:
        actions += ["MarkOrdered", "RejectAtPurchase"]

    elif status == config.STATUS_ORDERED and actor["is_fulfillment"]:
        actions += ["MarkReceived"]

    elif status == config.STATUS_RECEIVED and is_requestor:
        actions += ["ConfirmReceipt"]

    # A requestor can withdraw their own request until it has been ordered.
    if is_requestor and status in config.CANCELLABLE_STATUSES:
        actions.append("Cancel")

    # Anyone who submitted a finished requisition can start a new one from it.
    if is_requestor and status in config.TERMINAL_STATUSES:
        actions.append("CopyToNew")

    return actions


def _is_finance_approver_for(requisition, actor):
    """True when this actor may sign off the finance gate on this requisition.

    Reads the amount off the requisition rather than trusting the caller, so a
    stale page cannot claim a lower threshold than the one that applies.
    Nobody releases their own spend, whatever role they hold.
    """
    if str(actor["emp_no"]) == str(requisition.get("RequestorEmpNo", "")):
        return False

    roles    = dao.load_roles()
    settings = dao.load_config_values()

    expected = finance_authority_emp_no(
        _as_float(requisition.get("TotalAmount")),
        requisition.get("Currency", "CAD"),
        roles, settings,
    )
    return str(actor["emp_no"]) == str(expected)


def can_view(requisition, actor):
    """True when this actor is entitled to see this requisition at all.

    Requestors see their own. AP and fulfillment see everything, because both
    roles need visibility across the queue to do their jobs.
    """
    if actor["is_finance_low"] or actor["is_finance_high"] or actor["is_fulfillment"]:
        return True

    actor_upn = actor["upn"].lower()
    return actor_upn in (
        (requisition.get("RequestorUPN") or "").lower(),
        (requisition.get("ManagerUPN") or "").lower(),
    )


def _require_action(requisition, actor, action):
    """Guard used at the top of every state-changing operation.

    Re-checks permission at execution time rather than trusting what the UI
    sent, and gives a clear message when a requisition has moved on since the
    page was loaded.
    """
    if action not in allowed_actions(requisition, actor):
        raise WorkflowError(
            f"You cannot {action} this requisition in its current state "
            f"({requisition.get('Status', 'unknown')}). "
            "It may have already been actioned."
        )


# ── Reference data for the submission form ────────────────────────────────────

def list_catalogue():
    """Catalogue items for the submission form's picker.

    Reshapes the SharePoint rows into the field names the form expects so the
    browser never has to know about SharePoint's internal column names.
    """
    rows = dao.get_catalogue()
    items = []
    for row in rows:
        items.append({
            "itemCode": row.get(config.COL_CATALOGUE_CODE, ""),
            "itemName": row.get("ItemName", ""),
            "sortOrder": row.get("SortOrder", 999),
        })
    items.sort(key=_catalogue_sort_key)
    return items


def _catalogue_sort_key(item):
    """Sort by the configured order, then alphabetically as a tiebreak."""
    return (item["sortOrder"], item["itemName"])


# ── Portal assembly ───────────────────────────────────────────────────────────

def build_portal_view(actor):
    """Everything the portal needs for one signed-in user, in one call.

    Splits the user's own requisitions into active and finished, and adds a
    queue for AP and fulfillment holders.
    """
    own = dao.get_requisitions_by_requestor(actor["upn"])

    active  = [r for r in own if r.get("Status") in config.ACTIVE_STATUSES]
    history = [r for r in own if r.get("Status") in config.TERMINAL_STATUSES]

    queue = []
    queue_statuses = _queue_statuses_for(actor)
    if queue_statuses:
        queue = [
            r for r in dao.get_requisitions_by_statuses(queue_statuses)
            # A person's own requisitions already appear under Active; showing
            # them again in the queue would be noise.
            if (r.get("RequestorUPN") or "").lower() != actor["upn"].lower()
        ]

    return {
        "actor":   _public_actor(actor),
        "active":  [summarise(r) for r in active],
        "history": [summarise(r) for r in history],
        "queue":   [summarise(r) for r in queue],
    }


def _queue_statuses_for(actor):
    """Which statuses belong in this actor's work queue.

    AP sees everything from their approval step onward so they retain oversight
    after approving. Fulfillment sees only what they can act on.
    """
    if actor["is_finance_low"] or actor["is_finance_high"]:
        return [
            config.STATUS_PENDING_AP,
            config.STATUS_APPROVED_PENDING,
            config.STATUS_ORDERED,
            config.STATUS_RECEIVED,
        ]
    if actor["is_fulfillment"]:
        return [
            config.STATUS_APPROVED_PENDING,
            config.STATUS_ORDERED,
            config.STATUS_RECEIVED,
        ]
    return []


def _public_actor(actor):
    """The subset of the actor record the browser is allowed to see."""
    return {
        "upn":           actor["upn"],
        "empNo":         actor["emp_no"],
        "fullName":      actor["full_name"],
        "department":    actor["department"],
        "managerEmpNo":  actor["manager_emp_no"],
        "isFinanceLow":  actor["is_finance_low"],
        "isFinanceHigh": actor["is_finance_high"],
        "isFulfillment": actor["is_fulfillment"],
    }


def summarise(requisition):
    """Compact requisition shape for list views."""
    return {
        "requisitionID": requisition.get(config.COL_REQ_ID, ""),
        "status":        requisition.get("Status", ""),
        "requestorName": requisition.get("RequestorName", ""),
        "requestorUPN":  requisition.get("RequestorUPN", ""),
        "reason":        requisition.get("Reason", ""),
        "currency":      requisition.get("Currency", "CAD"),
        "totalAmount":   _as_float(requisition.get("TotalAmount")),
        "submittedUtc":  requisition.get("SubmittedUtc", ""),
    }


def build_detail_view(requisition_id, actor):
    """Full requisition detail plus the actions this actor may take.

    One shape serves every context — the portal, an approver arriving from an
    email, and fulfillment. The caller decides how to render it; the action list
    is what varies by person.
    """
    requisition = dao.get_requisition(requisition_id)
    if not requisition:
        raise WorkflowError(f"Requisition {requisition_id} was not found.")

    if not can_view(requisition, actor):
        raise WorkflowError(
            "You do not have permission to view this requisition."
        )

    lines   = dao.get_line_items(requisition_id)
    history = dao.get_history(requisition_id)

    return {
        "requisition":    _detail(requisition),
        "lineItems":      [_line(l) for l in lines],
        "history":        [_history_entry(h) for h in history],
        "allowedActions": allowed_actions(requisition, actor),
    }


def _detail(requisition):
    """Full requisition shape for the detail view."""
    summary = summarise(requisition)
    summary.update({
        "requestorEmpNo":   requisition.get("RequestorEmpNo", ""),
        "managerUPN":       requisition.get("ManagerUPN", ""),
        "paymentMode":      requisition.get("PaymentMode", ""),
        "managerComment":     requisition.get("ManagerComment", ""),
        "apComment":          requisition.get("APComment", ""),
        "fulfillmentComment": requisition.get("FulfillmentComment", ""),
        "rejectionReason":    requisition.get("RejectionReason", ""),
        "orderedUtc":       requisition.get("OrderedUtc", ""),
        "receivedUtc":      requisition.get("ReceivedUtc", ""),
        "closedUtc":        requisition.get("ClosedUtc", ""),
    })
    return summary


def _line(line):
    return {
        "id":                line.get("ID"),
        "lineNumber":        line.get("LineNumber", 0),
        "itemDescription":   line.get("ItemDescription", ""),
        "quantity":          _as_float(line.get("Quantity"), default=1),
        "unitPriceEstimate": _as_float(line.get("UnitPriceEstimate")),
        "vendorAtPurchase":  line.get("VendorAtPurchase", ""),
        "actualUnitPrice":   _as_float(line.get("ActualUnitPrice")),
    }


def _history_entry(entry):
    return {
        "fromStatus":    entry.get("FromStatus", ""),
        "toStatus":      entry.get("ToStatus", ""),
        "transitionUtc": entry.get("TransitionUtc", ""),
        "comment":       entry.get("Comment", ""),
    }


def _as_float(value, default=0.0):
    """SharePoint returns numbers as strings often enough to warrant this."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ── Submission ────────────────────────────────────────────────────────────────

def submit(actor, currency, reason, line_items):
    """Create a new requisition and start it down the approval path.

    Returns {"requisitionID": "REQ-YYYY-NNNN"}.
    """
    if not reason or not reason.strip():
        raise WorkflowError("A reason for the purchase is required.")
    if not line_items:
        raise WorkflowError("At least one item is required.")

    settings = dao.load_config_values()
    roles    = dao.load_roles()

    total = _total_of(line_items)
    _enforce_cap(total, currency, settings)

    initial_status = _initial_status(actor, total, currency, roles, settings)
    manager_upn    = (
        dao.get_upn_for_emp_no(actor["manager_emp_no"])
        if actor["manager_emp_no"] else ""
    )

    # SharePoint assigns the integer ID atomically, so we create first with a
    # placeholder and derive the business ID from the ID it hands back. That
    # removes any chance of two simultaneous submissions colliding.
    created = dao.create_requisition({
        config.COL_REQ_ID:      "PENDING",
        "Status":               initial_status,
        "RequestorEmpNo":       actor["emp_no"],
        "RequestorUPN":         actor["upn"],
        "RequestorName":        actor["full_name"],
        "ManagerEmpNoSnapshot": actor["manager_emp_no"],
        "ManagerUPN":           manager_upn,
        "Reason":               reason.strip(),
        "Currency":             currency,
        "TotalAmount":          total,
        "SubmittedUtc":         dao.utc_now_iso(),
    })

    sp_id          = created["ID"]
    requisition_id = _format_requisition_id(sp_id)
    dao.update_requisition(sp_id, {config.COL_REQ_ID: requisition_id})

    _create_lines(sp_id, requisition_id, line_items)

    _record_history(
        sp_id, requisition_id,
        config.STATUS_DRAFT, initial_status,
        _submission_audit_note(actor, initial_status, roles),
    )

    _notify_next_approver(
        requisition_id, actor, reason, total, currency,
        line_items, initial_status, manager_upn, roles, settings,
    )

    return {"requisitionID": requisition_id}


def _submission_audit_note(actor, initial_status, roles):
    """Explain in the audit trail why a requisition started where it did.

    A requisition that skips gates should say so, otherwise the history looks
    like steps went missing.
    """
    if initial_status == config.STATUS_APPROVED_PENDING:
        return (
            f"Submitted by {actor['full_name']} and released directly — "
            "requestor is the CFO and holds terminal authority on spend"
        )
    if initial_status == config.STATUS_PENDING_AP:
        return (
            f"Submitted by {actor['full_name']} — no manager on record, "
            "sent straight to the finance gate"
        )
    return f"Submitted by {actor['full_name']}"


def _total_of(line_items):
    """Sum the estimated value of a submission."""
    total = 0.0
    for item in line_items:
        total += _as_float(item.get("quantity"), 1) * _as_float(item.get("unitPriceEstimate"))
    return total


def _enforce_cap(total, currency, settings):
    """Refuse anything over the self-service ceiling."""
    key = "MaxCAD" if currency == "CAD" else "MaxUSD"
    cap = _as_float(settings.get(key), config.DEFAULT_CAP)
    if total > cap:
        raise WorkflowError(
            f"This requisition totals {currency} ${total:,.2f}, which exceeds "
            f"the ${cap:,.2f} limit. Please use the formal PO process."
        )


def _initial_status(actor, total, currency, roles, settings):
    """Where a new requisition starts.

    Three outcomes:
      - The CFO has nobody above them on spend, so their requisitions are
        already released and go straight to purchasing.
      - Anyone without a manager in the directory skips to the finance gate.
      - Everyone else starts with their manager.
    """
    if is_terminal_authority(actor["emp_no"], roles):
        return config.STATUS_APPROVED_PENDING

    if not actor["manager_emp_no"]:
        return config.STATUS_PENDING_AP

    return config.STATUS_PENDING_MANAGER


def _finance_gate_already_satisfied(approver_emp_no, requestor_emp_no,
                                    total, currency, roles, settings):
    """True when the finance gate needs no separate approval.

    Two cases, both of which would otherwise leave a requisition waiting on
    someone who has already had their say or cannot have one:

      - The manager who just approved is also the financial authority for this
        amount. Asking the same person twice adds delay, not control.
      - The requestor is the financial authority for this amount. Nobody
        approves their own spend, and their manager has already signed off.
    """
    authority = finance_authority_emp_no(total, currency, roles, settings)

    if approver_emp_no and str(approver_emp_no) == str(authority):
        return True
    if requestor_emp_no and str(requestor_emp_no) == str(authority):
        return True
    return False


def _format_requisition_id(sp_id):
    """REQ-YYYY-NNNN, where NNNN is the SharePoint row ID."""
    import datetime
    year = datetime.datetime.utcnow().year
    return f"REQ-{year}-{sp_id:04d}"


def _create_lines(sp_req_id, requisition_id, line_items):
    """Write the line item rows for a new requisition."""
    for index, item in enumerate(line_items, start=1):
        row = {
            "Title":             f"{requisition_id}-{index:02d}",
            "RequisitionIDId":   sp_req_id,   # Lookup fields take the integer ID
            "LineNumber":        index,
            "ItemDescription":   item.get("itemDescription", ""),
            "Quantity":          _as_float(item.get("quantity"), 1),
            "UnitPriceEstimate": _as_float(item.get("unitPriceEstimate")),
        }

        # A catalogue item links to its master row. Free-text items omit the
        # Lookup entirely — SharePoint rejects a Lookup set to empty.
        catalogue_id = dao.get_catalogue_id(item.get("itemCode", ""))
        if catalogue_id:
            row["ItemCodeId"] = catalogue_id

        # URL columns want an object, not a string, and reject empty values.
        url = (item.get("itemURL") or "").strip()
        if url:
            row["ItemURL"] = {"Url": url, "Description": url}

        dao.create_line_item(row)


def _notify_next_approver(requisition_id, actor, reason, total, currency,
                          line_items, initial_status, manager_upn, roles,
                          settings):
    """Send the first notification for a newly submitted requisition.

    Usually that is an approval request. When the CFO submits, there is nothing
    to approve, so purchasing is notified directly instead.
    """
    if initial_status == config.STATUS_APPROVED_PENDING:
        copy_to = _copy_list(actor["upn"])
        finance_notify = finance_notify_emp_no(total, currency, roles, settings)
        if finance_notify:
            notify_upn = dao.get_upn_for_emp_no(finance_notify)
            if notify_upn and notify_upn not in copy_to:
                copy_to.append(notify_upn)

        notifications.send_ready_to_purchase(
            requisition_id, actor["full_name"], reason, total, currency,
            line_items, _fulfiller_upn(actor["emp_no"], roles), copy_to,
        )
        return

    if initial_status == config.STATUS_PENDING_MANAGER:
        notifications.send_approval_request(
            requisition_id, actor["full_name"], actor["department"], reason,
            total, currency, line_items, manager_upn,
            is_ap_stage=False,
        )
        return

    # No manager in the directory, so the finance gate is the first stop.
    authority_emp_no = finance_authority_emp_no(total, currency, roles, settings)
    notifications.send_approval_request(
        requisition_id, actor["full_name"], actor["department"], reason,
        total, currency, line_items,
        dao.get_upn_for_emp_no(authority_emp_no),
        is_ap_stage=True,
        manager_was_skipped=True,
    )


# ── State transitions ─────────────────────────────────────────────────────────

def approve(requisition_id, actor, comment=""):
    """Advance a requisition past its current approval gate.

    Two gates exist: the requestor's manager, then whoever releases the spend.
    When the manager is already that person, one approval clears both.
    """
    requisition = _load_for_action(requisition_id, actor, "Approve")
    status      = requisition.get("Status", "")
    sp_id       = requisition["ID"]
    roles       = dao.load_roles()
    settings    = dao.load_config_values()
    now         = dao.utc_now_iso()

    total    = _as_float(requisition.get("TotalAmount"))
    currency = requisition.get("Currency", "CAD")

    if status == config.STATUS_PENDING_MANAGER:
        _approve_manager_gate(
            requisition, sp_id, requisition_id, actor, comment,
            total, currency, roles, settings, now,
        )
    else:
        _approve_finance_gate(
            requisition, sp_id, requisition_id, actor, comment,
            total, currency, roles, settings, now,
        )

    return {"status": "ok"}


def _approve_manager_gate(requisition, sp_id, requisition_id, actor, comment,
                          total, currency, roles, settings, now):
    """Record a manager approval and route onward.

    If this manager also holds the financial authority for the amount, the
    requisition skips the second gate entirely.
    """
    requestor_emp_no = requisition.get("RequestorEmpNo", "")
    gate_satisfied   = _finance_gate_already_satisfied(
        actor["emp_no"], requestor_emp_no, total, currency, roles, settings,
    )
    next_status = (
        config.STATUS_APPROVED_PENDING if gate_satisfied
        else config.STATUS_PENDING_AP
    )

    dao.update_requisition(sp_id, {
        "Status":                     next_status,
        "ManagerApprovedUtc":         now,
        "ManagerApproverEmpNo":       actor["emp_no"],
        config.COL_MGR_APPROVER_NAME: actor["full_name"],
        "ManagerComment":             comment,
    })

    if gate_satisfied:
        # Record the finance approval explicitly rather than leaving a gap in
        # the trail where a second sign-off would normally sit.
        note = _collapse_audit_note(
            actor, requestor_emp_no, total, currency, roles, settings
        )
        dao.update_requisition(sp_id, {
            "APApprovedUtc":   now,
            "APApproverEmpNo": actor["emp_no"],
            "APComment":       note,
        })
        _record_history(
            sp_id, requisition_id,
            config.STATUS_PENDING_MANAGER, config.STATUS_APPROVED_PENDING,
            f"{comment} — {note}" if comment else note,
        )
        _send_to_fulfillment(
            requisition, requisition_id, total, currency, roles, settings,
        )
        return

    _record_history(
        sp_id, requisition_id,
        config.STATUS_PENDING_MANAGER, config.STATUS_PENDING_AP,
        comment or "Approved by manager",
    )

    authority_emp_no = finance_authority_emp_no(total, currency, roles, settings)
    notifications.send_approval_request(
        requisition_id,
        requisition.get("RequestorName", ""), "",
        requisition.get("Reason", ""),
        total, currency,
        dao.get_line_items(requisition_id),
        dao.get_upn_for_emp_no(authority_emp_no),
        is_ap_stage=True,
    )


def _collapse_audit_note(actor, requestor_emp_no, total, currency,
                         roles, settings):
    """Wording for the audit trail when the finance gate is skipped."""
    authority = finance_authority_emp_no(total, currency, roles, settings)

    if str(actor["emp_no"]) == str(authority):
        return (
            f"Finance gate satisfied at manager approval — "
            f"{actor['full_name']} holds financial authority for this amount"
        )
    return (
        "Finance gate satisfied at manager approval — "
        "requestor holds financial authority for this amount and cannot "
        "approve their own spend"
    )


def _approve_finance_gate(requisition, sp_id, requisition_id, actor, comment,
                          total, currency, roles, settings, now):
    """Record the financial approval and release the requisition to purchasing."""
    dao.update_requisition(sp_id, {
        "Status":          config.STATUS_APPROVED_PENDING,
        "APApprovedUtc":   now,
        "APApproverEmpNo": actor["emp_no"],
        "APComment":       comment,
    })
    _record_history(
        sp_id, requisition_id,
        config.STATUS_PENDING_AP, config.STATUS_APPROVED_PENDING,
        comment or "Approved by financial authority",
    )
    _send_to_fulfillment(
        requisition, requisition_id, total, currency, roles, settings,
    )


def _send_to_fulfillment(requisition, requisition_id, total, currency,
                         roles, settings):
    """Hand an approved requisition to purchasing.

    The Director of Finance is copied on anything the CFO released, so Finance
    keeps sight of spend it did not personally approve.
    """
    requestor_emp_no = requisition.get("RequestorEmpNo", "")

    copy_to = _copy_list(
        requisition.get("RequestorUPN"),
        requisition.get("ManagerUPN"),
    )

    finance_notify = finance_notify_emp_no(total, currency, roles, settings)
    if finance_notify:
        notify_upn = dao.get_upn_for_emp_no(finance_notify)
        if notify_upn and notify_upn not in copy_to:
            copy_to.append(notify_upn)

    notifications.send_ready_to_purchase(
        requisition_id,
        requisition.get("RequestorName", ""),
        requisition.get("Reason", ""),
        total, currency,
        dao.get_line_items(requisition_id),
        _fulfiller_upn(requestor_emp_no, roles),
        copy_to,
    )


def reject(requisition_id, actor, comment):
    """Decline a requisition at either approval gate."""
    if not comment or not comment.strip():
        raise WorkflowError("A reason is required when rejecting a requisition.")

    requisition = _load_for_action(requisition_id, actor, "Reject")
    status      = requisition.get("Status", "")
    sp_id       = requisition["ID"]

    terminal = (
        config.STATUS_REJECTED_MANAGER
        if status == config.STATUS_PENDING_MANAGER
        else config.STATUS_REJECTED_AP
    )

    dao.update_requisition(sp_id, {
        "Status":          terminal,
        "RejectionReason": comment.strip(),
    })
    _record_history(sp_id, requisition_id, status, terminal, comment.strip())

    # Loop the manager in when AP is the one declining, so they know the
    # request they approved did not proceed.
    copy_to = (
        [requisition.get("ManagerUPN")]
        if status == config.STATUS_PENDING_AP else []
    )
    notifications.send_rejected(
        requisition_id, comment.strip(),
        requisition.get("RequestorUPN", ""), copy_to,
    )

    return {"status": "ok"}


def cancel(requisition_id, actor, comment=""):
    """Let a requestor withdraw their own request before it is ordered."""
    requisition = _load_for_action(requisition_id, actor, "Cancel")
    status      = requisition.get("Status", "")
    sp_id       = requisition["ID"]
    roles       = dao.load_roles()

    dao.update_requisition(sp_id, {
        "Status":             config.STATUS_CANCELLED,
        "CancellationReason": comment,
    })
    _record_history(sp_id, requisition_id, status, config.STATUS_CANCELLED,
                    comment or "Cancelled by requestor")

    # Tell whoever was waiting on it. Nobody is waiting on a Draft.
    if status == config.STATUS_PENDING_MANAGER:
        notifications.send_cancelled(
            requisition_id, requisition.get("ManagerUPN", ""), [],
        )
    elif status in (config.STATUS_PENDING_AP, config.STATUS_APPROVED_PENDING):
        settings         = dao.load_config_values()
        requestor_emp_no = requisition.get("RequestorEmpNo", "")
        authority_emp_no = finance_authority_emp_no(
            _as_float(requisition.get("TotalAmount")),
            requisition.get("Currency", "CAD"),
            roles, settings,
        )
        notifications.send_cancelled(
            requisition_id,
            dao.get_upn_for_emp_no(authority_emp_no),
            [_fulfiller_upn(requestor_emp_no, roles)],
        )

    return {"status": "ok"}


def mark_ordered(requisition_id, actor, payment_mode, comment="",
                 line_updates=None):
    """Record how fulfillment satisfied a requisition.

    Covers both buying the item and issuing one already held in stock. The
    comment is where that distinction is recorded, along with anything else
    worth knowing — a substituted model, a delayed delivery, a partial fill.
    Actual unit price of zero is what marks a stock issue in the spend report.
    """
    if not payment_mode:
        raise WorkflowError("A payment method is required.")
    if not comment or not comment.strip():
        raise WorkflowError(
            "Add a note saying what was done — ordered, issued from stock, "
            "or substituted."
        )

    requisition = _load_for_action(requisition_id, actor, "MarkOrdered")
    sp_id       = requisition["ID"]
    now         = dao.utc_now_iso()
    note        = comment.strip()

    dao.update_requisition(sp_id, {
        "Status":              config.STATUS_ORDERED,
        "OrderedUtc":          now,
        "OrderedByEmpNo":      actor["emp_no"],
        "PaymentMode":         payment_mode,
        "FulfillmentComment":  note,
    })

    # Actual vendor and price are captured per line at purchase time.
    for update in (line_updates or []):
        dao.update_line_item(update["id"], {
            "VendorAtPurchase": update.get("vendorAtPurchase", ""),
            "ActualUnitPrice":  _as_float(update.get("actualUnitPrice")),
        })

    _record_history(sp_id, requisition_id, config.STATUS_APPROVED_PENDING,
                    config.STATUS_ORDERED, note)

    notifications.send_ordered(
        requisition_id, payment_mode, now, note,
        requisition.get("RequestorUPN", ""),
        _copy_list(requisition.get("ManagerUPN")),
    )

    return {"status": "ok"}


def mark_received(requisition_id, actor):
    """Record that the goods have arrived at reception."""
    requisition = _load_for_action(requisition_id, actor, "MarkReceived")
    sp_id       = requisition["ID"]
    now         = dao.utc_now_iso()
    settings    = dao.load_config_values()

    dao.update_requisition(sp_id, {
        "Status":          config.STATUS_RECEIVED,
        "ReceivedUtc":     now,
        "ReceivedByEmpNo": actor["emp_no"],
    })
    _record_history(sp_id, requisition_id, config.STATUS_ORDERED,
                    config.STATUS_RECEIVED, "Received at reception")

    notifications.send_received(
        requisition_id, now,
        settings.get("ReceiptConfirmDays", config.DEFAULT_RECEIPT_CONFIRM_DAYS),
        requisition.get("RequestorUPN", ""),
        _copy_list(requisition.get("ManagerUPN")),
    )

    return {"status": "ok"}


def confirm_receipt(requisition_id, actor):
    """Requestor confirms they have the goods; the requisition closes."""
    requisition = _load_for_action(requisition_id, actor, "ConfirmReceipt")
    sp_id       = requisition["ID"]
    now         = dao.utc_now_iso()
    roles       = dao.load_roles()

    dao.update_requisition(sp_id, {
        "Status":       config.STATUS_CLOSED,
        "ConfirmedUtc": now,
        "ClosedUtc":    now,
    })

    # Two history rows: the confirmation itself, then the automatic close.
    # Keeping them separate means the audit trail shows why it closed.
    _record_history(sp_id, requisition_id, config.STATUS_RECEIVED,
                    config.STATUS_CONFIRMED, "Requestor confirmed receipt")
    _record_history(sp_id, requisition_id, config.STATUS_CONFIRMED,
                    config.STATUS_CLOSED, "Closed on receipt confirmation")

    notifications.send_receipt_confirmed(
        requisition_id,
        requisition.get("RequestorName", ""),
        _fulfiller_upn(requisition.get("RequestorEmpNo", ""), roles),
        _copy_list(requisition.get("ManagerUPN")),
    )

    return {"status": "ok"}


def reject_at_purchase(requisition_id, actor, comment):
    """Fulfillment could not buy the item as approved — send it back."""
    if not comment or not comment.strip():
        raise WorkflowError("A reason is required when rejecting at purchase.")

    requisition = _load_for_action(requisition_id, actor, "RejectAtPurchase")
    sp_id       = requisition["ID"]
    roles       = dao.load_roles()

    dao.update_requisition(sp_id, {
        "Status":                    config.STATUS_REJECTED_PURCHASE,
        "RejectedAtPurchaseUtc":     dao.utc_now_iso(),
        "RejectedAtPurchaseByEmpNo": actor["emp_no"],
        "RejectionReason":           comment.strip(),
    })
    _record_history(sp_id, requisition_id, config.STATUS_APPROVED_PENDING,
                    config.STATUS_REJECTED_PURCHASE, comment.strip())

    settings         = dao.load_config_values()
    authority_emp_no = finance_authority_emp_no(
        _as_float(requisition.get("TotalAmount")),
        requisition.get("Currency", "CAD"),
        roles, settings,
    )
    notifications.send_rejected_at_purchase(
        requisition_id, comment.strip(),
        requisition.get("RequestorUPN", ""),
        _copy_list(
            requisition.get("ManagerUPN"),
            dao.get_upn_for_emp_no(authority_emp_no),
        ),
    )

    return {"status": "ok"}


# ── Shared helpers for transitions ────────────────────────────────────────────

def _load_for_action(requisition_id, actor, action):
    """Fetch a requisition and confirm this actor may perform this action.

    Every transition starts here, so authorisation cannot be skipped by
    adding a new handler and forgetting the check.
    """
    requisition = dao.get_requisition(requisition_id)
    if not requisition:
        raise WorkflowError(f"Requisition {requisition_id} was not found.")

    _require_action(requisition, actor, action)
    return requisition


def _record_history(sp_req_id, requisition_id, from_status, to_status, comment):
    """Append one row to the audit trail."""
    dao.create_history_entry({
        "Title":           f"{requisition_id}: {from_status} to {to_status}",
        "RequisitionIDId": sp_req_id,
        "FromStatus":      from_status,
        "ToStatus":        to_status,
        "TransitionUtc":   dao.utc_now_iso(),
        "Comment":         comment,
    })


def _copy_list(*addresses):
    """Build a CC list, dropping blanks so nobody gets an empty recipient."""
    return [a for a in addresses if a]
