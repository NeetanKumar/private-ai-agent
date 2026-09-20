import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from gateway.config import get_settings
from gateway.main import app

client = TestClient(app)
AUTH = {"Authorization": "Bearer test-owner-token"}
MSG = [{"role": "user", "content": "hello"}]
BASE = "http://ollama:11434"


def _cfg():
    get_settings.cache_clear()
    return get_settings()


@respx.mock
def test_health_ok():
    respx.get(f"{BASE}/api/tags").respond(200, json={"models": []})
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["model_server"] == "up"


@respx.mock
def test_health_degraded_when_model_down():
    respx.get(f"{BASE}/api/tags").mock(side_effect=httpx.ConnectError("down"))
    assert client.get("/health").status_code == 503


@respx.mock
def test_completion_proxied_and_lane_reported():
    route = respx.post(f"{BASE}/v1/chat/completions").respond(
        200, json={"choices": [{"message": {"role": "assistant", "content": "hi"}}]}
    )
    r = client.post("/v1/chat/completions", json={"messages": MSG}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["lane"] == "private" and r.headers["X-Lane"] == "private"
    assert route.calls.last.request.read()  # forwarded
    sent = route.calls.last.request.content.decode()
    assert _cfg().models.aliases["daily"].id in sent


@respx.mock
def test_on_demand_alias_selects_second_model():
    route = respx.post(f"{BASE}/v1/chat/completions").respond(200, json={"choices": []})
    client.post("/v1/chat/completions", json={"model": "on-demand", "messages": MSG}, headers=AUTH)
    assert _cfg().models.aliases["on-demand"].id in route.calls.last.request.content.decode()


def test_unknown_model_rejected():
    r = client.post("/v1/chat/completions", json={"model": "gpt-4o", "messages": MSG}, headers=AUTH)
    assert r.status_code == 400


@respx.mock
def test_fails_closed_when_model_down():
    respx.post(f"{BASE}/v1/chat/completions").mock(side_effect=httpx.ConnectError("down"))
    r = client.post("/v1/chat/completions", json={"messages": MSG}, headers=AUTH)
    assert r.status_code == 503
    assert r.json()["lane"] == "private" and r.json()["error"] == "local_model_unavailable"


@respx.mock
def test_no_request_ever_leaves_for_other_hosts():
    respx.post(f"{BASE}/v1/chat/completions").mock(side_effect=httpx.ConnectError("down"))
    client.post("/v1/chat/completions", json={"messages": MSG}, headers=AUTH)
    hosts = {c.request.url.host for c in respx.calls}
    assert hosts <= {"ollama"}


def test_models_endpoint_lists_aliases():
    names = {m["id"] for m in client.get("/v1/models", headers=AUTH).json()["data"]}
    assert {"daily", "on-demand"} <= names


def test_requests_without_token_are_rejected():
    assert client.post("/v1/chat/completions", json={"messages": MSG}).status_code == 401
    assert client.get("/v1/models").status_code == 401
