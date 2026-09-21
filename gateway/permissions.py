"""Permission layer. Enforced here, in code, on every model response; never by prompting.

What it does
  * `filter_client_tools`   client-supplied tool definitions are reduced to names; only names in the
                            code allowlist AND enabled by config survive, and the model is shown the
                            gateway's own canonical definitions, never the client's.
  * `enforce_tool_calls`    every tool call in a model response is checked: known, offered, valid
                            arguments, within the per-turn cap. Anything else is removed before the
                            response leaves the gateway, so no client can be handed a write/exec call.
  * `SecurityLog`           blocked attempts are recorded (tool name and reason only, no arguments or
                            content) in a file separate from the egress audit log.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .tools import READ_ONLY_TOOLS, ToolError, get_spec, validate_args

BLOCKED_TEXT = "That action is not permitted: this agent is read-only."


class SecurityLog:
    def __init__(self, path: str):
        self.path = Path(path)
        self._lock = threading.Lock()

    def write(self, user: str, event: str, tool: str = "", reason: str = "") -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
               "user": user, "event": event, "tool": tool[:64], "reason": reason[:64]}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
                f.flush()
                os.fsync(f.fileno())

    def events(self, user: Optional[str] = None, limit: Optional[int] = None) -> List[dict]:
        """Events (newest first if a limit is given), optionally only one user's."""
        if not self.path.exists():
            return []
        rows = [json.loads(l) for l in self.path.read_text().splitlines() if l]
        if user is not None:
            rows = [r for r in rows if r.get("user") == user]
        return rows[::-1][:limit] if limit else rows


def filter_client_tools(client_names: List[str], enabled: List[str], user: str,
                        sec: Optional[SecurityLog]) -> Tuple[List[Dict[str, Any]], Set[str]]:
    """-> (canonical definitions to show the model, set of offered names)."""
    defs, offered = [], set()
    for name in client_names:
        spec = get_spec(name, enabled)
        if spec is None:
            if sec:
                sec.write(user, "tool_def_stripped", name, "not_in_readonly_allowlist")
            continue
        if name not in offered:
            defs.append(spec.definition())
            offered.add(name)
    return defs, offered


def enforce_tool_calls(message: Dict[str, Any], offered: Set[str], enabled: List[str], user: str,
                       sec: Optional[SecurityLog], max_calls: int) -> Tuple[List[Dict[str, Any]], int]:
    """Validate message['tool_calls'] in place. Returns (kept calls, number blocked).

    Kept calls are rewritten to a canonical shape with a gateway-issued id if the model gave none."""
    raw = message.get("tool_calls") or []
    if not isinstance(raw, list):
        raw = []
    kept: List[Dict[str, Any]] = []
    blocked = 0
    seen_ids: Set[str] = set()
    for call in raw:
        fn = call.get("function") if isinstance(call, dict) else None
        name = fn.get("name") if isinstance(fn, dict) else None
        if not isinstance(name, str):
            blocked += 1
            if sec:
                sec.write(user, "tool_call_blocked", "", "malformed")
            continue
        if get_spec(name, enabled) is None:
            blocked += 1
            if sec:
                sec.write(user, "tool_call_blocked", name, "not_in_readonly_allowlist")
            continue
        if name not in offered:
            blocked += 1
            if sec:
                sec.write(user, "tool_call_blocked", name, "not_offered_this_turn")
            continue
        if len(kept) >= max_calls:
            blocked += 1
            if sec:
                sec.write(user, "tool_call_blocked", name, "too_many_calls")
            continue
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except ValueError:
                blocked += 1
                if sec:
                    sec.write(user, "tool_call_blocked", name, "arguments_not_json")
                continue
        try:
            validate_args(READ_ONLY_TOOLS[name].schema, args)
        except ToolError:
            blocked += 1
            if sec:
                sec.write(user, "tool_call_blocked", name, "invalid_arguments")
            continue
        cid = call.get("id") if isinstance(call.get("id"), str) and call.get("id") else ""
        if not cid or cid in seen_ids:
            cid = "call_" + uuid.uuid4().hex[:16]
        seen_ids.add(cid)
        kept.append({"id": cid, "type": "function",
                     "function": {"name": name, "arguments": json.dumps(args)}})
    if kept:
        message["tool_calls"] = kept
    else:
        message.pop("tool_calls", None)
    return kept, blocked
