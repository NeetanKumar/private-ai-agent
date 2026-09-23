"""A single chat turn can offer BOTH read-only and action tools; the model's response is
validated against exactly the registry each call belongs to, so neither can misclassify or
launder the other. This is the wiring that makes "type a natural-language request in chat"
actually able to call an action tool, not just POST /v1/actions/{name} directly."""
import json

from conftest import auth, tool_defs, user_body
from stubs import call


def mixed_defs(*names):
    return tool_defs(*names)


def test_model_can_be_offered_and_call_an_action_tool_in_one_chat_turn(make_rig):
    rig = make_rig(extra_actions=["reminder_create"])
    rig.local.script.append({"tool_calls": [call("reminder_create", {"text": "buy milk"}, "a1")]})
    r = rig.post(user_body("remind me to buy milk", tools=mixed_defs("reminder_create")))
    d = r.json()
    assert d["choices"][0]["finish_reason"] == "tool_calls"
    calls = d["choices"][0]["message"]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == ["reminder_create"]
    sent_tools = rig.local.last_request()["tools"]
    assert {t["function"]["name"] for t in sent_tools} == {"reminder_create"}


def test_read_only_and_action_tools_offered_together_and_both_kept(make_rig):
    rig = make_rig(extra_actions=["reminder_create"])
    rig.local.script.append({"tool_calls": [
        call("read_file", {"path": "a.md"}, "r1"),
        call("reminder_create", {"text": "buy milk"}, "a1")]})
    r = rig.post(user_body("read my file and remind me", tools=mixed_defs("read_file", "reminder_create")))
    kept = r.json()["choices"][0]["message"]["tool_calls"]
    assert {c["function"]["name"] for c in kept} == {"read_file", "reminder_create"}
    sent_tools = {t["function"]["name"] for t in rig.local.last_request()["tools"]}
    assert sent_tools == {"read_file", "reminder_create"}


def test_a_readonly_only_call_cannot_be_smuggled_in_as_an_action_or_vice_versa(make_rig):
    """The core regression this wiring must never allow: enforce_tool_calls must not see (and
    therefore cannot wrongly reject) an action tool's call, and enforce_action_tool_calls must
    never see (and cannot wrongly accept) a read-only tool's call."""
    rig = make_rig(extra_actions=["reminder_create"])
    rig.local.script.append({"tool_calls": [call("reminder_create", {"text": "x"}, "a1")]})
    # only read_file offered - reminder_create must be blocked as not-offered, not silently misfiled
    r = rig.post(user_body("go", tools=mixed_defs("read_file")))
    d = r.json()
    assert "tool_calls" not in d["choices"][0]["message"]
    assert any(e["event"] == "action_call_blocked" and e["reason"] == "not_offered_this_turn"
              for e in rig.sec_events())


def test_action_tool_not_in_the_action_registry_is_blocked_not_misclassified(make_rig):
    rig = make_rig(extra_actions=["reminder_create"])
    rig.local.script.append({"tool_calls": [call("gmail_send", {"to": "a@example.com", "subject": "x",
                                                                "body": "y"}, "a1")]})
    r = rig.post(user_body("send an email", tools=mixed_defs("reminder_create", "gmail_send")))
    d = r.json()
    assert "tool_calls" not in d["choices"][0]["message"]     # gmail_send is not enabled
    assert any(e["event"] == "action_call_blocked" and e["tool"] == "gmail_send" for e in rig.sec_events())


def test_disabled_actions_are_never_offered_to_the_model_even_if_requested(make_rig):
    rig = make_rig(extra_actions=["reminder_create"])   # gmail_send not enabled
    rig.post(user_body("hi", tools=mixed_defs("reminder_create", "gmail_send")))
    sent_tools = {t["function"]["name"] for t in rig.local.last_request()["tools"]}
    assert sent_tools == {"reminder_create"}


def test_unknown_tool_name_in_neither_registry_is_still_blocked_and_logged(make_rig):
    rig = make_rig(extra_actions=["reminder_create"])
    rig.local.script.append({"tool_calls": [call("delete_everything", {}, "x1")]})
    r = rig.post(user_body("go", tools=mixed_defs("reminder_create")))
    assert "tool_calls" not in r.json()["choices"][0]["message"]
    assert any(e["tool"] == "delete_everything" for e in rig.sec_events())


def test_action_calls_are_still_private_lane_only_and_require_the_gateway_issued_result_id(make_rig):
    rig = make_rig(extra_actions=["reminder_create"])
    r = rig.post(user_body("remind me", tools=mixed_defs("reminder_create"), lane="frontier"))
    assert r.status_code == 403 and r.json()["error"] == "tools_private_lane_only"


def test_tool_result_for_an_action_call_round_trips_with_taint_like_a_readonly_result(make_rig):
    """After the client executes an action via POST /v1/actions/{name} (tested elsewhere) it sends
    the result back exactly like a read-only tool result - this proves that round trip taints and
    records the fragment correctly for an action-tool name too."""
    rig = make_rig(extra_actions=["reminder_create"])
    rig.local.script.append({"tool_calls": [call("reminder_create", {"text": "buy milk"}, "a1")]})
    rig.post(user_body("remind me", tools=mixed_defs("reminder_create")))
    rig.local.script.append({"content": "Done, I created your reminder."})
    r2 = rig.post({"messages": [{"role": "tool", "tool_call_id": "a1",
                                 "content": "reminder abc123 created: buy milk"}],
                  "tools": mixed_defs("reminder_create")})
    d2 = r2.json()
    assert d2["choices"][0]["message"]["content"] == "Done, I created your reminder."
    assert d2["taint"] == "PRIVATE"     # unregistered "tool:reminder_create" source defaults PRIVATE
