#!/usr/bin/env python3
"""Stdio MCP bridge: exposes the gateway's read-only tools to an MCP-capable agent (OpenClaw).

The bridge holds no tool logic and no permissions of its own. It lists what the gateway lists and
forwards each call to the gateway, which validates the tool name and arguments, sandboxes paths to
the authenticated user's folder, and refuses anything outside the read-only allowlist. Standard
library only, so it adds no dependency.

  GATEWAY_URL     e.g. http://gateway:8080
  GATEWAY_TOKEN   the user's bearer token
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

PROTOCOL = "2024-11-05"
VERSION = "0.1.0"


class Gateway:
    def __init__(self, url: str, token: str, timeout: float = 60):
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout

    def _req(self, method: str, path: str, body: Optional[dict] = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {"error": "gateway_error"}
        except (urllib.error.URLError, OSError):
            return 0, {"error": "gateway_unreachable"}

    def list_tools(self):
        return self._req("GET", "/v1/tools")

    def call_tool(self, name: str, arguments: Dict[str, Any]):
        return self._req("POST", f"/v1/tools/{name}", {"arguments": arguments})


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle(msg: Any, gw: Gateway) -> Optional[dict]:
    """One JSON-RPC message in, at most one response out (notifications get none)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return _err(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
    method, mid, params = msg["method"], msg.get("id"), msg.get("params") or {}
    if "id" not in msg:
        return None                                   # notification, e.g. notifications/initialized
    if method == "initialize":
        return _ok(mid, {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                         "serverInfo": {"name": "private-ai-readonly-tools", "version": VERSION}})
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        status, body = gw.list_tools()
        if status != 200:
            return _err(mid, -32000, body.get("error", "gateway error"))
        return _ok(mid, {"tools": [
            {"name": t["function"]["name"], "description": t["function"]["description"],
             "inputSchema": t["function"]["parameters"]} for t in body.get("tools", [])]})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return _err(mid, -32602, "invalid params")
        status, body = gw.call_tool(name, args)
        if status == 200:
            return _ok(mid, {"content": [{"type": "text", "text": body.get("content", "")}], "isError": False})
        # Refusals and tool errors are results the model can read, not protocol failures.
        return _ok(mid, {"content": [{"type": "text", "text": f"error: {body.get('error', 'failed')}"}],
                         "isError": True})
    return _err(mid, -32601, f"method not found: {method}")


def main() -> int:
    url, token = os.environ.get("GATEWAY_URL", ""), os.environ.get("GATEWAY_TOKEN", "")
    if not url or not token:
        print("GATEWAY_URL and GATEWAY_TOKEN are required", file=sys.stderr)
        return 2
    gw = Gateway(url, token)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            resp = handle(json.loads(line), gw)
        except ValueError:
            resp = _err(None, -32700, "parse error")
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
