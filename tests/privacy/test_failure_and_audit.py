"""Fail-closed behaviour, audit integrity, and the 200-query canary run."""
import json
import random

import pytest

from conftest import auth

PRIVATE_DOC = [{"text": "confidential merger terms", "source": "uploaded_doc"}]


def test_killing_the_local_model_returns_an_error_not_a_frontier_response(make_rig):
    rig = make_rig(auto_route_clean=True)
    rig.local.stop()
    # tainted context: must be the private lane, which is dead
    r = rig.chat("hello", context=PRIVATE_DOC)
    assert r.status_code == 503 and r.json()["lane"] == "private"
    assert r.json()["error"] == "local_model_unavailable"
    # CLEAN + explicit private lane: same answer, still no fallback
    r = rig.chat("hello", lane="private")
    assert r.status_code == 503 and r.json()["lane"] == "private"
    # CLEAN + auto_route_clean=false + no consent: goes to the private lane, which is dead
    rig2 = make_rig(auto_route_clean=False)
    rig2.local.stop()
    r = rig2.chat("hello")
    assert r.status_code == 503 and r.json()["error"] == "local_model_unavailable"
    assert rig.frontier.connections == 0 and rig2.frontier.connections == 0
    assert rig.audit_lines() == [] and rig2.audit_lines() == []


def test_streaming_also_fails_closed(make_rig):
    rig = make_rig(auto_route_clean=False)
    rig.local.stop()
    r = rig.client.post("/v1/chat/completions", headers=auth(), json={
        "messages": [{"role": "user", "content": "hi"}], "stream": True})
    assert r.status_code == 503 and rig.frontier.connections == 0


def test_health_reports_degraded_when_model_is_down(make_rig):
    rig = make_rig()
    assert rig.client.get("/health").status_code == 200
    rig.local.stop()
    assert rig.client.get("/health").status_code == 503


def test_frontier_failure_is_an_error_and_is_still_audited(make_rig):
    rig = make_rig(auto_route_clean=True)
    rig.frontier.stop()
    r = rig.chat("hello")
    assert r.status_code == 502 and r.json()["lane"] == "frontier"
    lines = rig.audit_lines()
    assert len(lines) == 1 and json.loads(lines[0])["status"] == "error"
    assert rig.local.connections == 0          # no silent fallback to the local lane either


def test_frontier_not_configured_makes_no_connection(make_rig, monkeypatch):
    rig = make_rig(auto_route_clean=True)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    r = rig.chat("hello")
    assert r.status_code == 503 and r.json()["error"] == "frontier_not_configured"
    assert rig.frontier.connections == 0 and rig.audit_lines() == []


def test_audit_record_count_equals_outbound_connection_count(make_rig):
    rig = make_rig(auto_route_clean=True)
    rig.chat("clean 1")
    rig.chat("clean 2")
    rig.chat("private", context=PRIVATE_DOC)      # blocked: no connection, no record
    rig.chat("still tainted")                     # blocked
    rig.client.post("/session/new", headers=auth())
    rig.chat("clean 3")
    rig.chat("clean 4", lane="private")           # local: no connection
    rig.chat("clean 5", attachments=[{"n": 1}])   # blocked: no connection
    assert rig.frontier.connections == 3
    assert len(rig.audit_lines()) == rig.frontier.connections


def test_audit_log_holds_no_prompt_bodies_or_user_data(make_rig):
    rig = make_rig(auto_route_clean=True)
    secret_question = "why did CANARY-AUDIT-7731 fail?"
    rig.chat(secret_question)
    raw = rig.audit_path.read_text()
    assert "CANARY-AUDIT-7731" not in raw and "why did" not in raw
    rec = json.loads(raw.splitlines()[0])
    import hashlib
    assert rec["prompt_sha256"] == hashlib.sha256(secret_question.encode()).hexdigest()


@pytest.mark.parametrize("auto,consent", [(True, False), (False, True)])
def test_canaries_in_private_sources_never_reach_the_frontier_across_200_queries(make_rig, auto, consent):
    rig = make_rig(auto_route_clean=auto)
    if consent:
        rig.client.post("/session/consent", json={"consent": True}, headers=auth())
    rng = random.Random(1234)
    canaries = []
    for i in range(200):
        kind = rng.choice(["clean", "unregistered", "registered_private", "public", "mixed"])
        ctx, canary = [], f"CANARY-{i:04d}-{rng.randrange(10**8):08d}"
        if kind == "unregistered":
            ctx = [{"text": f"internal memo {canary}", "source": "uploaded_doc"}]
        elif kind == "registered_private":
            ctx = [{"text": f"customer record {canary}", "source": "crm"}]
        elif kind == "public":
            ctx = [{"text": "public price list", "source": "public_docs"}]
            canary = None
        elif kind == "mixed":
            ctx = [{"text": "public price list", "source": "public_docs"},
                   {"text": f"salary table {canary}", "source": "email"}]
        else:
            canary = None
        if canary:
            canaries.append(canary)
        if rng.random() < 0.25:                       # sometimes start fresh, sometimes ride history
            rig.client.post("/session/new", headers=auth())
            if consent:
                rig.client.post("/session/consent", json={"consent": True}, headers=auth())
        r = rig.chat(f"question number {i}", context=ctx)
        assert r.status_code == 200
        if canary:
            assert r.json()["lane"] == "private"
    sent = "\n".join(rig.frontier.bodies)
    assert canaries, "test must actually seed canaries"
    assert not any(c in sent for c in canaries)
    assert 0 < rig.frontier.connections < 200         # not vacuous: some queries did egress
    assert len(rig.audit_lines()) == rig.frontier.connections
    # sanity: the canaries did reach the local model, so they were really in the requests
    assert any(c in "\n".join(rig.local.bodies) for c in canaries)
