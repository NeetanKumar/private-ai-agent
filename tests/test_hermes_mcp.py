"""integrations/hermes_mcp/server.py against a REAL running gateway (real socket, not the
FastAPI TestClient) since the MCP adapter makes plain httpx calls to a URL, not to an ASGI app in
process. Covers: tool list matches /v1/actions, a call round-trips through confirm=true
unconditionally (the explicit, documented deviation - see docs/HERMES_INTEGRATION.md), and the
JSON-RPC framing (initialize / tools/list / tools/call) is correct."""
import copy
import os
import pathlib
import socket
import sys
import threading
import time

import pytest
import uvicorn
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "integrations" / "hermes_mcp")]

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from conftest import TOKENS           # noqa: E402 - conftest.py already sets GATEWAY_TOKEN_OWNER
from gateway.config import Settings   # noqa: E402
from gateway.main import create_app   # noqa: E402
import server as mcp_server           # noqa: E402

TOKEN = TOKENS["owner"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def gateway_url(tmp_path):
    port = free_port()
    raw = copy.deepcopy(yaml.safe_load((ROOT / "infra" / "config.yaml").read_text()))
    raw["model_server"]["base_url"] = "http://127.0.0.1:1/v1"          # unused by action tools
    raw["model_server"]["health_url"] = "http://127.0.0.1:1/api/tags"
    raw["rag"]["db_path"] = str(tmp_path / "rag.db")
    raw["audit"]["path"] = str(tmp_path / "audit.jsonl")
    raw["security"]["path"] = str(tmp_path / "sec.jsonl")
    raw["tools"]["files_root"] = str(tmp_path / "files")
    (tmp_path / "files" / "owner").mkdir(parents=True)
    raw["actions"]["enabled"] = ["reminder_create", "reminder_list"]
    raw["actions"]["reminders_dir"] = str(tmp_path / "reminders")
    raw["actions"]["audit_path"] = str(tmp_path / "actions-audit.jsonl")
    app = create_app(Settings(**raw))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            import httpx
            if httpx.get(url + "/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield url
    server.should_exit = True
    thread.join(timeout=5)


def test_list_actions_matches_enabled_config(gateway_url):
    client = mcp_server.GatewayClient(gateway_url, TOKEN)
    names = {d["function"]["name"] for d in client.list_actions()}
    assert names == {"reminder_create", "reminder_list"}


def test_run_action_executes_and_returns_the_result(gateway_url):
    client = mcp_server.GatewayClient(gateway_url, TOKEN)
    out = client.run_action("reminder_create", {"text": "buy milk"})
    assert "buy milk" in out
    listing = client.run_action("reminder_list", {})
    assert "buy milk" in listing


def test_run_action_always_sends_confirm_true(gateway_url):
    """The documented deviation: even reminder_create (requires_confirmation=True, i.e. this
    project's own UI would stage it) executes immediately through this adapter, no staging."""
    client = mcp_server.GatewayClient(gateway_url, TOKEN)
    out = client.run_action("reminder_create", {"text": "no staging here"})
    assert out.startswith("reminder ") or "created" in out


def test_run_action_raises_on_disabled_or_unknown_tool(gateway_url):
    client = mcp_server.GatewayClient(gateway_url, TOKEN)
    with pytest.raises(mcp_server.GatewayError):
        client.run_action("gmail_send", {"to": "a@example.com", "subject": "x", "body": "y"})
    with pytest.raises(mcp_server.GatewayError):
        client.run_action("delete_everything", {})


def test_mcp_json_rpc_initialize_tools_list_and_call(gateway_url):
    client = mcp_server.GatewayClient(gateway_url, TOKEN)
    srv = mcp_server.McpServer(client, client.list_actions())

    init = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["serverInfo"]["name"] == "private-ai-actions"

    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    listed = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in listed["result"]["tools"]}
    assert names == {"reminder_create", "reminder_list"}

    called = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "reminder_create", "arguments": {"text": "via mcp"}}})
    assert called["result"]["isError"] is False
    assert "via mcp" in called["result"]["content"][0]["text"]


def test_mcp_call_on_unknown_tool_is_reported_as_an_error_not_a_crash(gateway_url):
    client = mcp_server.GatewayClient(gateway_url, TOKEN)
    srv = mcp_server.McpServer(client, client.list_actions())
    r = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "nope", "arguments": {}}})
    assert r["error"]["code"] == -32602


def test_run_stdio_reads_one_line_writes_one_line(gateway_url):
    import io
    client = mcp_server.GatewayClient(gateway_url, TOKEN)
    srv = mcp_server.McpServer(client, client.list_actions())
    req = '{"jsonrpc": "2.0", "id": 9, "method": "tools/list"}\n'
    out = io.StringIO()
    mcp_server.run_stdio(srv, stdin=io.StringIO(req), stdout=out)
    import json
    resp = json.loads(out.getvalue().strip())
    assert resp["id"] == 9 and len(resp["result"]["tools"]) == 2
