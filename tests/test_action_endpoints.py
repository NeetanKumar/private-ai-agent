"""/v1/actions and /v1/actions/{name}: allowlist enforcement, confirmation staging, execution,
audit trail, auth, and isolation. Mirrors tests/test_ui.py's tool-endpoint tests but for actions."""
import json

from conftest import auth


def test_actions_disabled_by_default(make_rig):
    rig = make_rig()  # no extra_actions passed
    assert rig.client.get("/v1/actions", headers=auth()).json()["actions"] == []
    assert rig.action("reminder_create", {"text": "x"}).status_code == 403


def test_definitions_endpoint_lists_only_enabled_actions(make_rig):
    rig = make_rig(extra_actions=["reminder_create", "reminder_list"])
    names = {a["function"]["name"] for a in rig.client.get("/v1/actions", headers=auth()).json()["actions"]}
    assert names == {"reminder_create", "reminder_list"}


def test_unknown_or_disabled_action_is_refused_and_logged(make_rig):
    rig = make_rig(extra_actions=["reminder_list"])
    r = rig.action("gmail_send", {"to": "a@b.com", "subject": "x", "body": "y"})
    assert r.status_code == 403 and r.json()["error"] == "action_not_permitted"
    assert any(e["tool"] == "gmail_send" and e["event"] == "action_call_blocked" for e in rig.sec_events())
    r2 = rig.action("delete_all_files", {})
    assert r2.status_code == 403


def test_action_endpoints_require_auth(make_rig):
    rig = make_rig(extra_actions=["reminder_list"])
    assert rig.client.get("/v1/actions").status_code == 401
    assert rig.client.post("/v1/actions/reminder_list", json={"arguments": {}}).status_code == 401


# ---- confirmation staging --------------------------------------------------------------------------

def test_fixed_confirmation_required_stages_without_executing(make_rig):
    rig = make_rig(extra_actions=["reminder_create", "reminder_list"])
    r = rig.action("reminder_create", {"text": "buy milk"}, confirm=False)
    assert r.status_code == 200 and r.json()["status"] == "needs_confirmation"
    listing = rig.action("reminder_list", {}, confirm=False)          # read-only tool, no confirmation
    assert listing.json()["status"] == "executed" and "buy milk" not in listing.json()["result"]


def test_confirm_true_executes_the_staged_action(make_rig):
    rig = make_rig(extra_actions=["reminder_create", "reminder_list"])
    rig.action("reminder_create", {"text": "buy milk"}, confirm=False)   # staged, not executed
    r = rig.action("reminder_create", {"text": "buy milk"}, confirm=True)
    assert r.status_code == 200 and r.json()["status"] == "executed"
    listing = rig.action("reminder_list", {})
    assert "buy milk" in listing.json()["result"]


def test_readonly_style_action_never_needs_confirmation(make_rig):
    rig = make_rig(extra_actions=["reminder_list"])
    r = rig.action("reminder_list", {}, confirm=False)
    assert r.json()["status"] == "executed"


def test_judge_tool_skips_confirmation_only_on_explicit_consent_hint(make_rig):
    rig = make_rig(extra_actions=["gmail_send"])   # google not configured, but staging happens first
    r = rig.action("gmail_send", {"to": "a@b.com", "subject": "hi", "body": "hello"},
                   confirm=False, context_hint="draft me an email to bob")
    assert r.json()["status"] == "needs_confirmation"
    r2 = rig.action("gmail_send", {"to": "a@b.com", "subject": "hi", "body": "hello"},
                    confirm=False, context_hint="yes send it")
    # explicit consent means needs_confirmation is False internally, so it tries to execute -
    # and fails cleanly because google isn't configured in this test, proving it actually attempted
    assert r2.json()["status"] == "error" and r2.json()["error"] == "google_not_configured"


def test_missing_context_hint_on_a_judge_tool_defaults_to_confirmation_required(make_rig):
    rig = make_rig(extra_actions=["gmail_send"])
    r = rig.action("gmail_send", {"to": "a@b.com", "subject": "hi", "body": "hello"}, confirm=False)
    assert r.json()["status"] == "needs_confirmation"


def test_invalid_arguments_are_rejected_even_when_confirmed(make_rig):
    rig = make_rig(extra_actions=["reminder_create"])
    r = rig.action("reminder_create", {"text": "x", "extra": 1}, confirm=True)
    assert r.status_code == 400
    r2 = rig.action("reminder_create", {}, confirm=True)
    assert r2.status_code == 400


# ---- audit trail -----------------------------------------------------------------------------------

def test_staged_and_executed_actions_are_both_audited_without_raw_arguments(make_rig):
    rig = make_rig(extra_actions=["reminder_create"])
    rig.action("reminder_create", {"text": "a secret plan"}, confirm=False)
    rig.action("reminder_create", {"text": "a secret plan"}, confirm=True)
    records = rig.client.get("/v1/actions-audit", headers=auth()).json()["records"]
    assert [r["status"] for r in records] == ["executed", "staged"]        # newest first
    blob = json.dumps(records)
    assert "a secret plan" not in blob                                    # no raw argument text
    assert all(set(r) == {"ts", "user", "tool", "args_sha256", "status"} for r in records)


def test_action_audit_endpoint_requires_auth_and_is_isolated_per_user(make_rig):
    rig = make_rig(extra_actions=["reminder_create", "reminder_list"])
    assert rig.client.get("/v1/actions-audit").status_code == 401
    rig.action("reminder_create", {"text": "owner's"}, confirm=True, user="owner")
    rig.action("reminder_create", {"text": "guest's"}, confirm=True, user="guest")
    owner_records = rig.client.get("/v1/actions-audit", headers=auth("owner")).json()["records"]
    guest_records = rig.client.get("/v1/actions-audit", headers=auth("guest")).json()["records"]
    assert len(owner_records) == 1 and len(guest_records) == 1


def test_reminders_are_isolated_per_user_through_the_endpoint(make_rig):
    rig = make_rig(extra_actions=["reminder_create", "reminder_list"])
    rig.action("reminder_create", {"text": "owner reminder"}, confirm=True, user="owner")
    rig.action("reminder_create", {"text": "guest reminder"}, confirm=True, user="guest")
    owner_list = rig.action("reminder_list", {}, user="owner").json()["result"]
    guest_list = rig.action("reminder_list", {}, user="guest").json()["result"]
    assert "owner reminder" in owner_list and "guest reminder" not in owner_list
    assert "guest reminder" in guest_list and "owner reminder" not in guest_list
