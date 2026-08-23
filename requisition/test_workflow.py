"""
test_workflow.py
================
Behavioural verification of the requisition rules.

SharePoint is mocked, so this exercises decision logic only — no network,
no credentials, safe to run anywhere. Run it before every deploy.

  python3 test_workflow.py
"""

import os
import sys

os.environ.setdefault("TENANT_ID", "test-tenant")
os.environ.setdefault("CLIENT_ID", "test-client")
os.environ.setdefault("MSAL_CACHE_PATH", "/tmp/no-such-cache")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import dao
import workflow


# ── Fixtures: a small but representative slice of the org ────────────────────

ROLES = {
    "FinanceAuthority-Low":  {"PrimaryEmpNo": "258"},    # Gurnoor, Director of Finance
    "FinanceAuthority-High": {"PrimaryEmpNo": "273"},    # Hari, CFO
    "Fulfillment":           {"PrimaryEmpNo": "5331"},   # Jennie
}

SETTINGS = {
    "MaxCAD": "5000",
    "MaxUSD": "5000",
    "FinanceAuthorityThreshold": "1000",
}

# upn -> (emp_no, name, manager_emp_no)
PEOPLE = {
    "vnair@macrodynepress.com":       ("5329", "Vijay Nair",      "273"),
    "hraghavan@macrodynepress.com":   ("273",  "Hari Raghavan",   ""),
    "gbajaj@macrodynepress.com":      ("258",  "Gurnoor Bajaj",   "273"),
    "jrego@macrodynepress.com":       ("5331", "Jennie Rego",     "68"),
    "bmacfarlane@macrodynepress.com": ("68",   "Brett MacFarlane","274"),
    "kfernandes@macrodynepress.com":  ("274",  "Kevin Fernandes", ""),
}

EMPLOYEES = {
    upn: {
        config.COL_EMP_NUMBER: emp_no,
        config.COL_EMP_NAME:   name,
        config.COL_EMP_DEPT:   "Test Dept",
        config.COL_EMP_MGR:    manager,
        config.COL_EMP_UPN:    upn,
    }
    for upn, (emp_no, name, manager) in PEOPLE.items()
}
BY_EMP_NO = {e[config.COL_EMP_NUMBER]: e for e in EMPLOYEES.values()}


def install_mocks():
    """Swap the DAO's SharePoint calls for in-memory fixtures."""
    dao.load_roles             = lambda: ROLES
    dao.load_config_values     = lambda: SETTINGS
    dao.get_employee_by_upn    = lambda upn: EMPLOYEES.get(upn)
    dao.get_employee_by_emp_no = lambda no: BY_EMP_NO.get(str(no))
    dao.get_upn_for_emp_no     = (
        lambda no: BY_EMP_NO.get(str(no), {}).get(config.COL_EMP_UPN, "")
    )


# ── Test harness ─────────────────────────────────────────────────────────────

RESULTS = []


def check(label, actual, expected):
    passed = actual == expected
    RESULTS.append(passed)
    print(f"  {'PASS' if passed else 'FAIL'}  {label}")
    if not passed:
        print(f"          expected: {expected!r}")
        print(f"          actual:   {actual!r}")


def section(title):
    print(f"\n{title}")


def requisition(total, requestor_upn="vnair@macrodynepress.com",
                requestor_emp_no="5329",
                manager_upn="hraghavan@macrodynepress.com",
                status=config.STATUS_PENDING_AP):
    """Build a requisition row shaped the way SharePoint returns one."""
    return {
        "ID":              1,
        "Status":          status,
        "TotalAmount":     total,
        "Currency":        "CAD",
        "RequestorUPN":    requestor_upn,
        "RequestorEmpNo":  requestor_emp_no,
        "ManagerUPN":      manager_upn,
    }


# ── Tests ────────────────────────────────────────────────────────────────────

def test_role_detection(people):
    section("Role detection")
    check("Gurnoor holds the low finance role",
          people["gurnoor"]["is_finance_low"], True)
    check("Hari holds the high finance role",
          people["hari"]["is_finance_high"], True)
    check("Hari does not hold the low role (no backups)",
          people["hari"]["is_finance_low"], False)
    check("Kevin holds no finance role",
          (people["kevin"]["is_finance_low"], people["kevin"]["is_finance_high"]),
          (False, False))
    check("Jennie holds fulfillment",
          people["jennie"]["is_fulfillment"], True)
    check("Vijay holds no role",
          (people["vijay"]["is_finance_low"],
           people["vijay"]["is_finance_high"],
           people["vijay"]["is_fulfillment"]),
          (False, False, False))


def test_authority_by_amount():
    section("Financial authority by amount")

    def authority(total):
        return workflow.finance_authority_emp_no(total, "CAD", ROLES, SETTINGS)

    check("$700 is the Director's call",       authority(700),    "258")
    check("$999.99 is the Director's call",    authority(999.99), "258")
    check("$1000 exactly is the CFO's call",   authority(1000),   "273")
    check("$1200 is the CFO's call",           authority(1200),   "273")
    check("Authority does not vary by who asks",
          (authority(700), authority(700)), ("258", "258"))


def test_terminal_authority():
    section("Terminal authority")
    check("The CFO is terminal",     workflow.is_terminal_authority("273", ROLES), True)
    check("The Director is not",     workflow.is_terminal_authority("258", ROLES), False)
    check("The CEO is not",          workflow.is_terminal_authority("274", ROLES), False)


def test_initial_status(people):
    section("Where a new requisition starts")

    def start(actor, total):
        return workflow._initial_status(actor, total, "CAD", ROLES, SETTINGS)

    check("Vijay starts at his manager",
          start(people["vijay"], 700), config.STATUS_PENDING_MANAGER)
    check("Vijay starts at his manager for large amounts too",
          start(people["vijay"], 1200), config.STATUS_PENDING_MANAGER)
    check("Kevin has no manager, so starts at the finance gate",
          start(people["kevin"], 700), config.STATUS_PENDING_AP)
    check("Kevin's large request also starts at the finance gate",
          start(people["kevin"], 1200), config.STATUS_PENDING_AP)
    check("The CFO's request is released on submission",
          start(people["hari"], 700), config.STATUS_APPROVED_PENDING)
    check("The CFO's large request is released on submission",
          start(people["hari"], 5000), config.STATUS_APPROVED_PENDING)
    check("Gurnoor starts at his manager like anyone else",
          start(people["gurnoor"], 700), config.STATUS_PENDING_MANAGER)


def test_finance_gate_collapse():
    section("When the finance gate needs no separate approval")

    def satisfied(approver, requestor, total):
        return workflow._finance_gate_already_satisfied(
            approver, requestor, total, "CAD", ROLES, SETTINGS
        )

    check("Hari approving Vijay's $1200 satisfies it — he is the CFO",
          satisfied("273", "5329", 1200), True)
    check("Hari approving Vijay's $700 does not — that is the Director's call",
          satisfied("273", "5329", 700), False)
    check("Hari approving Gurnoor's $700 satisfies it — Gurnoor cannot self-approve",
          satisfied("273", "258", 700), True)
    check("Brett approving Jennie's $1500 does not satisfy it",
          satisfied("68", "5331", 1500), False)
    check("Brett approving Jennie's $300 does not satisfy it",
          satisfied("68", "5331", 300), False)


def test_finance_gate_permissions(people):
    section("Who may act at the finance gate")

    check("Gurnoor approves a $700 request",
          sorted(workflow.allowed_actions(requisition(700), people["gurnoor"])),
          ["Approve", "Reject"])
    check("Hari does not — $700 is the Director's call",
          workflow.allowed_actions(requisition(700), people["hari"]), [])
    check("Hari approves a $1200 request",
          sorted(workflow.allowed_actions(requisition(1200), people["hari"])),
          ["Approve", "Reject"])
    check("Gurnoor does not — $1200 is the CFO's call",
          workflow.allowed_actions(requisition(1200), people["gurnoor"]), [])

    own = requisition(700, "gbajaj@macrodynepress.com", "258")
    actions = workflow.allowed_actions(own, people["gurnoor"])
    check("Gurnoor cannot approve his own request",
          [a for a in actions if a in ("Approve", "Reject")], [])
    check("Gurnoor can still withdraw his own request",
          "Cancel" in actions, True)


def test_manager_gate_permissions(people):
    section("Who may act at the manager gate")

    pending = requisition(700, status=config.STATUS_PENDING_MANAGER)
    check("Hari approves as Vijay's manager",
          sorted(workflow.allowed_actions(pending, people["hari"])),
          ["Approve", "Reject"])
    check("Vijay may only withdraw his own",
          workflow.allowed_actions(pending, people["vijay"]), ["Cancel"])
    check("Brett has no say in Vijay's request",
          workflow.allowed_actions(pending, people["brett"]), [])


def test_fulfillment_permissions(people):
    section("Fulfillment")

    approved = requisition(700, status=config.STATUS_APPROVED_PENDING)
    check("Jennie may order once approved",
          sorted(workflow.allowed_actions(approved, people["jennie"])),
          ["MarkOrdered", "RejectAtPurchase"])

    ordered = requisition(700, status=config.STATUS_ORDERED)
    check("Jennie may mark received once ordered",
          workflow.allowed_actions(ordered, people["jennie"]), ["MarkReceived"])
    check("The requestor cannot cancel once ordered",
          workflow.allowed_actions(ordered, people["vijay"]), [])

    received = requisition(700, status=config.STATUS_RECEIVED)
    check("The requestor confirms receipt",
          workflow.allowed_actions(received, people["vijay"]), ["ConfirmReceipt"])

    closed = requisition(700, status=config.STATUS_CLOSED)
    check("A closed request can be copied to a new one",
          workflow.allowed_actions(closed, people["vijay"]), ["CopyToNew"])


def test_finance_notification():
    section("Director of Finance visibility")

    def notify(total):
        return workflow.finance_notify_emp_no(total, "CAD", ROLES, SETTINGS)

    check("The Director is copied on CFO-approved spend", notify(1200), "258")
    check("No separate copy when the Director approved it", notify(700), "")


def test_visibility(people):
    section("Who can see what")

    other = requisition(700, "stranger@macrodynepress.com", "9999",
                        "othermgr@macrodynepress.com")
    check("The Director sees everything",  workflow.can_view(other, people["gurnoor"]), True)
    check("The CFO sees everything",       workflow.can_view(other, people["hari"]), True)
    check("Fulfillment sees everything",   workflow.can_view(other, people["jennie"]), True)
    check("Others see only their own",     workflow.can_view(other, people["vijay"]), False)
    check("A requestor sees their own",
          workflow.can_view(requisition(700), people["vijay"]), True)


def test_cap_enforcement():
    section("Spend cap")

    try:
        workflow._enforce_cap(6000.0, "CAD", SETTINGS)
        check("Over the cap is refused", "allowed", "refused")
    except workflow.WorkflowError:
        check("Over the cap is refused", "refused", "refused")

    try:
        workflow._enforce_cap(4999.0, "CAD", SETTINGS)
        check("Under the cap is allowed", "allowed", "allowed")
    except workflow.WorkflowError:
        check("Under the cap is allowed", "refused", "allowed")


def test_unknown_user():
    section("Unknown sign-in")

    try:
        workflow.identify_actor("stranger@macrodynepress.com")
        check("An unknown UPN is refused", "allowed", "refused")
    except workflow.WorkflowError:
        check("An unknown UPN is refused", "refused", "refused")


def test_audit_notes(people):
    section("Audit trail wording")

    note = workflow._submission_audit_note(
        people["hari"], config.STATUS_APPROVED_PENDING, ROLES
    )
    check("A CFO submission says why it skipped approval",
          "terminal authority" in note, True)

    note = workflow._submission_audit_note(
        people["kevin"], config.STATUS_PENDING_AP, ROLES
    )
    check("A no-manager submission says why it skipped the manager",
          "no manager on record" in note, True)

    note = workflow._collapse_audit_note(
        people["hari"], "5329", 1200, "CAD", ROLES, SETTINGS
    )
    check("A collapsed gate names the approver's authority",
          "holds financial authority" in note, True)

    note = workflow._collapse_audit_note(
        people["hari"], "258", 700, "CAD", ROLES, SETTINGS
    )
    check("A self-approval collapse says the requestor could not approve",
          "cannot" in note, True)


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    install_mocks()

    people = {
        "vijay":   workflow.identify_actor("vnair@macrodynepress.com"),
        "hari":    workflow.identify_actor("hraghavan@macrodynepress.com"),
        "gurnoor": workflow.identify_actor("gbajaj@macrodynepress.com"),
        "jennie":  workflow.identify_actor("jrego@macrodynepress.com"),
        "brett":   workflow.identify_actor("bmacfarlane@macrodynepress.com"),
        "kevin":   workflow.identify_actor("kfernandes@macrodynepress.com"),
    }

    print("=" * 70)
    print("REQUISITION WORKFLOW — BEHAVIOURAL VERIFICATION")
    print("=" * 70)

    test_role_detection(people)
    test_authority_by_amount()
    test_terminal_authority()
    test_initial_status(people)
    test_finance_gate_collapse()
    test_finance_gate_permissions(people)
    test_manager_gate_permissions(people)
    test_fulfillment_permissions(people)
    test_finance_notification()
    test_visibility(people)
    test_cap_enforcement()
    test_unknown_user()
    test_audit_notes(people)

    passed = sum(RESULTS)
    total  = len(RESULTS)
    print("\n" + "=" * 70)
    print(f"  {passed}/{total} checks passed")
    print("=" * 70)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
