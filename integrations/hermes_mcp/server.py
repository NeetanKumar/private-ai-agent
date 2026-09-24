#!/usr/bin/env python3
"""MCP (Model Context Protocol) stdio server exposing this project's action tools (reminders,
Gmail, Calendar) to Hermes Agent, so Hermes's own self-improvement loop can call them directly.

Hand-rolled JSON-RPC 2.0 over newline-delimited stdio (the MCP wire format), NOT the official
`mcp` Python SDK: that SDK requires Python 3.10+, and this repo's own dev environment is 3.9. This
also keeps the same "plain protocol over stdlib, no new SDK" pattern already used for the Gmail/
Calendar integration in gateway/action_tools.py, which talks to Google over plain httpx instead of
adding google-auth/google-api-python-client. The only dependency here is httpx, already a project
dependency (see requirements.txt in this directory).

Two explicit, user-chosen deviations from this project's own default guardrail posture (see
docs/HERMES_INTEGRATION.md for the full plan and reasoning):

  1. Every tool call this server makes to the gateway is sent with `confirm: true` immediately.
     Write actions (gmail_send, gmail_reply, calendar_create) execute the moment Hermes decides to
     call them - there is no staging/confirmation step on this path, unlike this project's own
     chat UI. They are still recorded in the gateway's action-audit log.
  2. Read results (gmail_read, calendar_read) are returned to Hermes exactly as the gateway
     returns them - no masking or truncation.

Config (env vars, set by Hermes's own `mcp_servers.<name>.env` block - see the README in this
directory):
  PRIVATE_AI_GATEWAY_URL    default http://127.0.0.1:8080
  PRIVATE_AI_GATEWAY_TOKEN  required; the gateway's own per-user bearer token
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

import httpx

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "private-ai-actions", "version": "1.0.0"}


class GatewayError(Exception):
    pass


class GatewayClient:
    """Thin proxy to this project's own /v1/actions HTTP API. No business logic lives here -
    the gateway is the single source of truth for what tools exist, their schemas, and whether a
    call is allowed; this class only forwards requests and never invents behavior of its own."""

    def __init__(self, base_url: str, token: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def list_actions(self) -> List[Dict[str, Any]]:
        r = httpx.get(f"{self.base_url}/v1/actions", headers=self._headers(), timeout=self.timeout)
        if r.status_code != 200:
            raise GatewayError(f"could not list actions: HTTP {r.status_code}")
        return r.json().get("actions", [])

    def run_action(self, name: str, arguments: Dict[str, Any]) -> str:
        # confirm: true always - see module docstring, deviation 1.
        body = {"arguments": arguments, "confirm": True}
        r = httpx.post(f"{self.base_url}/v1/actions/{name}", json=body, headers=self._headers(),
                       timeout=self.timeout)
        try:
            data = r.json()
        except ValueError:
            raise GatewayError(f"gateway returned non-JSON (HTTP {r.status_code})")
        if r.status_code != 200 or data.get("status") not in ("executed", "needs_confirmation"):
            raise GatewayError(data.get("error") or f"HTTP {r.status_code}")
        if data.get("status") == "needs_confirmation":
            # Should not happen since confirm=true is always sent, but fail loudly rather than
            # silently reporting success if the gateway's behavior ever changes.
            raise GatewayError("action was staged instead of executed; gateway behavior changed")
        return data.get("result", "")


class McpServer:
    """Minimal MCP server: initialize, tools/list, tools/call. Tool definitions are fetched from
    the gateway once at startup (see main()) and never hardcoded, so this file can never drift
    from gateway/action_tools.py's ACTION_TOOLS registry or infra/config.yaml's actions.enabled."""

    def __init__(self, gateway: GatewayClient, tool_defs: List[Dict[str, Any]]):
        self.gateway = gateway
        self.tools = [{"name": d["function"]["name"], "description": d["function"]["description"],
                       "inputSchema": d["function"]["parameters"]} for d in tool_defs]

    def handle(self, req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = req.get("method")
        req_id = req.get("id")
        if method == "initialize":
            return self._ok(req_id, {"protocolVersion": PROTOCOL_VERSION,
                                     "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO})
        if method == "notifications/initialized":
            return None    # one-way notification, no response
        if method == "tools/list":
            return self._ok(req_id, {"tools": self.tools})
        if method == "tools/call":
            return self._call_tool(req_id, req.get("params") or {})
        if req_id is None:
            return None     # unknown notification: ignore, don't answer
        return self._err(req_id, -32601, f"method not found: {method}")

    def _call_tool(self, req_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or name not in {t["name"] for t in self.tools}:
            return self._err(req_id, -32602, f"unknown tool: {name}")
        try:
            result = self.gateway.run_action(name, arguments)
            return self._ok(req_id, {"content": [{"type": "text", "text": result}], "isError": False})
        except GatewayError as e:
            return self._ok(req_id, {"content": [{"type": "text", "text": str(e)}], "isError": True})

    @staticmethod
    def _ok(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def _err(req_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def run_stdio(server: McpServer, stdin=sys.stdin, stdout=sys.stdout) -> None:
    """Newline-delimited JSON-RPC 2.0 over stdio - the MCP stdio transport. One JSON object per
    line in, at most one JSON object per line out (notifications get no reply)."""
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue    # malformed input is dropped, not fatal - matches typical MCP server behavior
        resp = server.handle(req)
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()


def main() -> None:
    base_url = os.environ.get("PRIVATE_AI_GATEWAY_URL", "http://127.0.0.1:8080")
    token = os.environ.get("PRIVATE_AI_GATEWAY_TOKEN", "")
    if not token:
        print("PRIVATE_AI_GATEWAY_TOKEN is not set", file=sys.stderr)
        sys.exit(1)
    gateway = GatewayClient(base_url, token)
    try:
        tool_defs = gateway.list_actions()
    except GatewayError as e:
        print(f"could not reach gateway at startup: {e}", file=sys.stderr)
        sys.exit(1)
    server = McpServer(gateway, tool_defs)
    run_stdio(server)


if __name__ == "__main__":
    main()
