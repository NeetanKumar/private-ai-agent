"""The MCP bridge against a real running gateway, plus checks on the agent configuration."""
import json
import pathlib
import socket
import subprocess
import sys
import threading
import time

import pytest
import uvicorn

from conftest import TOKENS, ROOT

sys.path.insert(0, str(ROOT / "agent"))
import mcp_server                                                    # noqa: E402


@pytest.fixture
def live_gateway(make_rig):
    rig = make_rig()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(rig.client.app, host="127.0.0.1", port=port, log_level="error", loop="asyncio"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield rig, f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=5)


def rpc(gw, method, params=None, mid=1):
    return mcp_server.handle({"jsonrpc": "2.0", "id": mid, "method": method, **({"params": params} if params else {})}, gw)


def test_initialize_and_list_only_the_readonly_tools(live_gateway):
    rig, url = live_gateway
    gw = mcp_server.Gateway(url, TOKENS["owner"])
    init = rpc(gw, "initialize", {"protocolVersion": "2024-11-05"})
    assert init["result"]["capabilities"] == {"tools": {}}
    assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, gw) is None
    tools = rpc(gw, "tools/list")["result"]["tools"]
    assert {t["name"] for t in tools} == {"list_files", "read_file", "search_files", "context_query"}
    assert all(t["inputSchema"]["type"] == "object" for t in tools)


def test_calling_a_tool_reads_the_users_own_file(live_gateway):
    rig, url = live_gateway
    rig.put_file("owner", "plan.md", "The launch is on October 21.")
    gw = mcp_server.Gateway(url, TOKENS["owner"])
    r = rpc(gw, "tools/call", {"name": "read_file", "arguments": {"path": "plan.md"}})["result"]
    assert r["isError"] is False and "October 21" in r["content"][0]["text"]


def test_write_and_exec_are_refused_through_the_bridge_and_logged(live_gateway):
    rig, url = live_gateway
    gw = mcp_server.Gateway(url, TOKENS["owner"])
    for name in ("write_file", "exec", "apply_patch", "web_fetch"):
        r = rpc(gw, "tools/call", {"name": name, "arguments": {"path": "x", "content": "y"}})["result"]
        assert r["isError"] is True and "tool_not_permitted" in r["content"][0]["text"]
    assert {e["tool"] for e in rig.sec_events()} >= {"write_file", "exec", "apply_patch", "web_fetch"}


def test_traversal_and_other_users_files_are_refused_through_the_bridge(live_gateway):
    rig, url = live_gateway
    rig.put_file("guest", "secret.md", "GUEST-ONLY")
    gw = mcp_server.Gateway(url, TOKENS["owner"])
    for path in ("../guest/secret.md", "/etc/passwd"):
        r = rpc(gw, "tools/call", {"name": "read_file", "arguments": {"path": path}})["result"]
        assert r["isError"] is True and "GUEST-ONLY" not in json.dumps(r)


def test_bad_token_and_unreachable_gateway_are_reported_not_crashed(live_gateway):
    rig, url = live_gateway
    r = rpc(mcp_server.Gateway(url, "wrong-token"), "tools/list")
    assert "error" in r
    r = rpc(mcp_server.Gateway("http://127.0.0.1:9", TOKENS["owner"], timeout=2), "tools/call",
            {"name": "read_file", "arguments": {"path": "a"}})
    assert r["result"]["isError"] is True


def test_protocol_errors_are_well_formed(live_gateway):
    rig, url = live_gateway
    gw = mcp_server.Gateway(url, TOKENS["owner"])
    assert rpc(gw, "nope")["error"]["code"] == -32601
    assert rpc(gw, "tools/call", {"name": 5, "arguments": {}})["error"]["code"] == -32602
    assert mcp_server.handle("garbage", gw)["error"]["code"] == -32600
    assert mcp_server.handle({"jsonrpc": "2.0", "id": 3}, gw)["error"]["code"] == -32600


def test_stdio_loop_speaks_newline_delimited_json_rpc(live_gateway):
    rig, url = live_gateway
    rig.put_file("owner", "a.md", "hello from a file")
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "read_file", "arguments": {"path": "a.md"}}}]
    p = subprocess.run([sys.executable, str(ROOT / "agent" / "mcp_server.py")], capture_output=True, text=True,
                       input="\n".join(json.dumps(l) for l in lines) + "\nnot json\n", timeout=30,
                       env={"GATEWAY_URL": url, "GATEWAY_TOKEN": TOKENS["owner"], "PATH": ""})
    out = [json.loads(l) for l in p.stdout.splitlines()]
    assert [o.get("id") for o in out] == [1, 2, None]            # no reply to the notification; parse error last
    assert "hello from a file" in out[1]["result"]["content"][0]["text"] and out[2]["error"]["code"] == -32700


def test_bridge_refuses_to_start_without_credentials():
    p = subprocess.run([sys.executable, str(ROOT / "agent" / "mcp_server.py")], capture_output=True, text=True,
                       input="", timeout=30, env={"PATH": ""})
    assert p.returncode == 2


# ---- agent configuration ---------------------------------------------------------------------------------------------

def test_openclaw_config_points_at_the_gateway_and_denies_write_exec_web():
    cfg = json.loads((ROOT / "agent" / "openclaw.json").read_text())
    prov = cfg["models"]["providers"]["private-gateway"]
    assert prov["baseUrl"].startswith("http://gateway:8080") and prov["api"] == "openai-completions"
    assert "api.anthropic.com" not in json.dumps(cfg) and "openai.com" not in json.dumps(cfg)
    deny = set(cfg["agents"]["entries"][0]["tools"]["deny"])
    assert {"group:fs", "group:runtime", "group:web", "exec", "write", "edit", "apply_patch"} <= deny
    assert cfg["agents"]["entries"][0]["tools"]["profile"] == "minimal"


def test_agent_config_never_sets_consent_lane_or_documents():
    text = (ROOT / "agent" / "openclaw.json").read_text().lower()
    for word in ("consent", "\"lane\"", "documents", "extra_body"):
        assert word not in text


def test_daily_driver_model_is_the_gateway_alias_not_a_model_name():
    """Models stay swappable: the agent asks for 'daily', the gateway config maps it to a model."""
    cfg = json.loads((ROOT / "agent" / "openclaw.json").read_text())
    ids = [m["id"] for m in cfg["models"]["providers"]["private-gateway"]["models"]]
    assert ids == ["daily"] and "qwen" not in json.dumps(cfg).lower()
