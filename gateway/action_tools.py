"""Action tools: read/write agent actions with real-world side effects (reminders, Gmail, Calendar).

This is a SEPARATE registry from gateway/tools.py, which stays read-only forever. The same
hard rule applies here: the allowlist lives in code (ACTION_TOOLS below). Config
(actions.enabled) may only narrow it; naming anything not in this dict is a startup error
(enforced in gateway/config.py, mirroring tools.enabled).

Two deliberate deviations from this project's usual all-deterministic design, made at the
operator's explicit request and flagged everywhere they appear:

  1. `requires_confirmation="judge"` tools use `judge_confirmation_needed()`, a heuristic over
     the user's own latest wording, to decide whether to skip the confirmation step. It is NOT
     a model call (kept deterministic and unit-testable) and its fail-safe default is to REQUIRE
     confirmation whenever the wording is not unambiguous explicit consent. This still means a
     cleverly-worded instruction (or an injected one, if a hostile document's text is ever
     mistaken for the user's own words) could trigger a real send with no human step. Do not
     feed untrusted text into the confirmation decision.
  2. Content taint for these tools follows the SAME deterministic registry every other tool
     uses (`gateway/taint.py: SourceRegistry`) — no new taint mechanism exists. The operator may
     choose to register `tool:gmail_read` etc. as CLEAN in `infra/config.yaml`'s `sources:` map,
     which is a materially riskier choice than the project's PRIVATE-by-default rule. That choice
     is NOT made here or by default; `infra/config.yaml` ships with no such registration, so
     Gmail/Calendar content defaults to PRIVATE like any unregistered source.

Invocation model: action tools are invoked the same way read-only tools already are outside a
chat turn — through their own HTTP endpoints (`GET /v1/actions`, `POST /v1/actions/{name}`),
mirroring `/v1/tools/{name}` in gateway/main.py. The gateway does not run a tool-calling loop
for these any more than it does for the read-only ones; an agent or the caller decides when to
invoke one, and the gateway only vets, stages, and executes.

Known gap (see docs/KNOWN_GAPS.md): a confirmed action is executed on that HTTP call; repeating
the same POST with confirm=true executes it again. No idempotency key exists yet.
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple, Union

import httpx

from .tools import ToolError, validate_args  # reuse the same small JSON-schema subset, no new validator

_USER_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR_API = "https://www.googleapis.com/calendar/v3/calendars/primary"


class ActionError(Exception):
    """Bad arguments, a failed call, or a not-configured provider. Message is safe to show."""


# ---- confirmation requirement -----------------------------------------------------------------
# True = always stage for confirmation. False = never needs it (read-only-ish, e.g. listing).
# "judge" = judge_confirmation_needed() decides, with a fail-safe default of True.
Confirmation = Union[bool, str]

# Wording that counts as unambiguous explicit consent to skip confirmation for a "judge" tool.
# Deliberately narrow: anything not matching one of these falls back to requiring confirmation.
_EXPLICIT_CONSENT = re.compile(
    r"\b(yes,?\s*(please\s*)?send|go ahead and send|send it now|please send it|"
    r"send that email now|confirm(ed)? *[:\-]? *send|yes create (the|that) event|"
    r"go ahead and create|create it now)\b", re.IGNORECASE)


def judge_confirmation_needed(latest_user_text: str) -> bool:
    """True (needs confirmation) unless `latest_user_text` is unambiguous explicit consent.

    This is a plain regex heuristic, not a model call, so it is deterministic and testable in
    isolation. Callers MUST only pass the user's own typed words here, never text drawn from a
    document, email body, or other untrusted source — this function has no way to tell the
    difference, and doing so would let injected text talk its way past confirmation."""
    if not isinstance(latest_user_text, str) or not latest_user_text.strip():
        return True
    return not _EXPLICIT_CONSENT.search(latest_user_text)


# ---- reminders: local, no OAuth, no external egress --------------------------------------------

_reminders_lock = threading.Lock()


def _reminders_path(ctx: "ActionContext") -> Path:
    if not _USER_RE.fullmatch(ctx.user_id):
        raise ActionError("invalid user")
    return Path(ctx.reminders_dir) / f"{ctx.user_id}.json"


def _reminders_load(ctx: "ActionContext") -> List[Dict[str, Any]]:
    p = _reminders_path(ctx)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except (ValueError, OSError):
        return []


def _reminders_save(ctx: "ActionContext", items: List[Dict[str, Any]]) -> None:
    p = _reminders_path(ctx)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items))


async def _reminder_create(ctx: "ActionContext", args: Dict[str, Any]) -> str:
    with _reminders_lock:
        items = _reminders_load(ctx)
        rid = uuid.uuid4().hex[:8]
        items.append({"id": rid, "text": args["text"], "when": args.get("when", ""), "done": False})
        _reminders_save(ctx, items)
    return f"reminder {rid} created: {args['text']}" + (f" (when: {args['when']})" if args.get("when") else "")


async def _reminder_list(ctx: "ActionContext", args: Dict[str, Any]) -> str:
    with _reminders_lock:
        items = _reminders_load(ctx)
    if not items:
        return "(no reminders)"
    return "\n".join(f"{i['id']}: {i['text']}" + (f" (when: {i['when']})" if i.get("when") else "")
                     for i in items)


async def _reminder_delete(ctx: "ActionContext", args: Dict[str, Any]) -> str:
    with _reminders_lock:
        items = _reminders_load(ctx)
        kept = [i for i in items if i["id"] != args["id"]]
        if len(kept) == len(items):
            raise ActionError("no such reminder")
        _reminders_save(ctx, kept)
    return f"reminder {args['id']} deleted"


# ---- Google: plain REST over httpx, no google-* SDK dependency ---------------------------------

class GoogleClient:
    """Minimal OAuth2 + REST client for Gmail and Calendar. Deliberately dependency-free (uses
    httpx, already a project dependency) rather than adding google-auth/google-api-python-client,
    which would need approval as network-calling dependencies."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, timeout: float = 30):
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self.timeout = timeout
        self._token: Optional[str] = None
        self._expiry: float = 0

    async def _access_token(self) -> str:
        if self._token and time.time() < self._expiry - 30:
            return self._token
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(GOOGLE_TOKEN_URL, data={
                "client_id": self.client_id, "client_secret": self.client_secret,
                "refresh_token": self.refresh_token, "grant_type": "refresh_token"})
        if r.status_code >= 400:
            raise ActionError("google_auth_failed")
        data = r.json()
        self._token, self._expiry = data["access_token"], time.time() + data.get("expires_in", 3600)
        return self._token

    async def _request(self, method: str, url: str, **kw) -> Dict[str, Any]:
        token = await self._access_token()
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kw)
        if r.status_code >= 400:
            raise ActionError(f"google_api_error_{r.status_code}")
        return r.json() if r.content else {}

    async def get(self, url: str, params: Optional[dict] = None) -> Dict[str, Any]:
        return await self._request("GET", url, params=params or {})

    async def post(self, url: str, json_body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request("POST", url, json=json_body)


def _require_google(ctx: "ActionContext") -> GoogleClient:
    if ctx.google is None:
        raise ActionError("google_not_configured")
    return ctx.google


async def _gmail_read(ctx: "ActionContext", args: Dict[str, Any]) -> str:
    g = _require_google(ctx)
    params = {"maxResults": args.get("max_results", 10)}
    if args.get("query"):
        params["q"] = args["query"]
    listing = await g.get(f"{GMAIL_API}/messages", params=params)
    msgs = listing.get("messages", [])
    if not msgs:
        return "(no messages)"
    lines = []
    for m in msgs[: args.get("max_results", 10)]:
        detail = await g.get(f"{GMAIL_API}/messages/{m['id']}", params={"format": "metadata",
                             "metadataHeaders": ["Subject", "From", "Date"]})
        headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
        lines.append(f"{m['id']} | {headers.get('From', '?')} | {headers.get('Subject', '(no subject)')} "
                     f"| {headers.get('Date', '?')} | {detail.get('snippet', '')[:120]}")
    return "\n".join(lines)


def _build_mime(to: str, subject: str, body: str, in_reply_to: Optional[str] = None,
               references: Optional[str] = None) -> str:
    msg = MIMEText(body)
    msg["to"] = to
    msg["subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


async def _gmail_send(ctx: "ActionContext", args: Dict[str, Any]) -> str:
    g = _require_google(ctx)
    raw = _build_mime(args["to"], args["subject"], args["body"])
    result = await g.post(f"{GMAIL_API}/messages/send", {"raw": raw})
    return f"sent, message id {result.get('id', '?')}"


async def _gmail_reply(ctx: "ActionContext", args: Dict[str, Any]) -> str:
    g = _require_google(ctx)
    original = await g.get(f"{GMAIL_API}/messages/{args['message_id']}",
                           params={"format": "metadata", "metadataHeaders": ["Subject", "From", "Message-ID"]})
    headers = {h["name"]: h["value"] for h in original.get("payload", {}).get("headers", [])}
    subject = headers.get("Subject", "")
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    msg_id = headers.get("Message-ID", "")
    raw = _build_mime(headers.get("From", ""), subject, args["body"], in_reply_to=msg_id, references=msg_id)
    result = await g.post(f"{GMAIL_API}/messages/send",
                          {"raw": raw, "threadId": original.get("threadId")})
    return f"replied, message id {result.get('id', '?')}"


async def _calendar_read(ctx: "ActionContext", args: Dict[str, Any]) -> str:
    g = _require_google(ctx)
    params: Dict[str, Any] = {"maxResults": args.get("max_results", 10), "singleEvents": True,
                             "orderBy": "startTime"}
    if args.get("time_min"):
        params["timeMin"] = args["time_min"]
    if args.get("time_max"):
        params["timeMax"] = args["time_max"]
    listing = await g.get(f"{CALENDAR_API}/events", params=params)
    items = listing.get("items", [])
    if not items:
        return "(no events)"
    lines = []
    for e in items:
        start = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", "?")
        lines.append(f"{e.get('id', '?')} | {start} | {e.get('summary', '(no title)')}")
    return "\n".join(lines)


async def _calendar_create(ctx: "ActionContext", args: Dict[str, Any]) -> str:
    g = _require_google(ctx)
    body = {"summary": args["summary"],
            "start": {"dateTime": args["start"]}, "end": {"dateTime": args["end"]}}
    if args.get("description"):
        body["description"] = args["description"]
    result = await g.post(f"{CALENDAR_API}/events", body)
    return f"created, event id {result.get('id', '?')}"


# ---- registry ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ActionContext:
    user_id: str
    reminders_dir: str
    google: Optional[GoogleClient] = None


@dataclass(frozen=True)
class ActionSpec:
    name: str
    description: str
    schema: Dict[str, Any]
    run: Callable[[ActionContext, Dict[str, Any]], Awaitable[str]]
    requires_confirmation: Confirmation

    def definition(self) -> Dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.schema}}


def _obj(props: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


ACTION_TOOLS: Dict[str, ActionSpec] = {t.name: t for t in (
    ActionSpec("reminder_create", "Create a personal reminder.",
              _obj({"text": {"type": "string", "minLength": 1, "maxLength": 500},
                    "when": {"type": "string", "minLength": 0, "maxLength": 100}}, ["text"]),
              _reminder_create, True),
    ActionSpec("reminder_list", "List personal reminders.", _obj({}, []), _reminder_list, False),
    ActionSpec("reminder_delete", "Delete a personal reminder by id.",
              _obj({"id": {"type": "string", "minLength": 1, "maxLength": 32}}, ["id"]),
              _reminder_delete, True),
    ActionSpec("gmail_read", "List recent Gmail messages, optionally filtered by a search query.",
              _obj({"query": {"type": "string", "minLength": 0, "maxLength": 200},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 25}}, []),
              _gmail_read, False),
    ActionSpec("gmail_send", "Send a new email.",
              _obj({"to": {"type": "string", "minLength": 3, "maxLength": 320},
                    "subject": {"type": "string", "minLength": 0, "maxLength": 500},
                    "body": {"type": "string", "minLength": 1, "maxLength": 50000}},
                   ["to", "subject", "body"]),
              _gmail_send, "judge"),
    ActionSpec("gmail_reply", "Reply to an existing email by message id.",
              _obj({"message_id": {"type": "string", "minLength": 1, "maxLength": 200},
                    "body": {"type": "string", "minLength": 1, "maxLength": 50000}},
                   ["message_id", "body"]),
              _gmail_reply, "judge"),
    ActionSpec("calendar_read", "List upcoming Calendar events.",
              _obj({"time_min": {"type": "string", "minLength": 0, "maxLength": 40},
                    "time_max": {"type": "string", "minLength": 0, "maxLength": 40},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 25}}, []),
              _calendar_read, False),
    ActionSpec("calendar_create", "Create a Calendar event.",
              _obj({"summary": {"type": "string", "minLength": 1, "maxLength": 500},
                    "start": {"type": "string", "minLength": 1, "maxLength": 40},
                    "end": {"type": "string", "minLength": 1, "maxLength": 40},
                    "description": {"type": "string", "minLength": 0, "maxLength": 5000}},
                   ["summary", "start", "end"]),
              _calendar_create, "judge"),
)}


def get_action_spec(name: str, enabled: List[str]) -> Optional[ActionSpec]:
    """The spec if `name` is both in the code allowlist and enabled by config, else None."""
    return ACTION_TOOLS.get(name) if name in enabled else None


async def execute_action(name: str, args: Any, ctx: ActionContext, enabled: List[str]) -> str:
    spec = get_action_spec(name, enabled)
    if spec is None:
        raise KeyError(name)
    try:
        validated = validate_args(spec.schema, args)
    except ToolError as e:
        raise ActionError(str(e)) from None
    return await spec.run(ctx, validated)


# ---- chat-turn plumbing: letting a model call these by name in a normal conversation -----------
# Mirrors gateway/permissions.py's filter_client_tools / enforce_tool_calls exactly, but against
# ACTION_TOOLS instead of READ_ONLY_TOOLS, so the two registries can never misclassify each
# other's calls. The gateway itself never executes an action mid-turn - the model only proposes a
# call here; the client (UI or agent) executes it via POST /v1/actions/{name}, where confirmation
# staging actually happens, then sends the result back as a normal tool_results message, reusing
# the same round-trip the read-only tools already use.

def filter_client_action_tools(client_names: List[str], enabled: List[str], user: str,
                               sec) -> Tuple[List[Dict[str, Any]], Set[str]]:
    """-> (canonical definitions to show the model, set of offered names). Silently skips a name
    that isn't an action tool at all (it may be a read-only tool name meant for the other
    filter) - only gateway.permissions logs unknown-tool attempts, to avoid double-logging the
    same name from both registries."""
    defs, offered = [], set()
    for name in client_names:
        spec = get_action_spec(name, enabled)
        if spec is None:
            continue
        if name not in offered:
            defs.append(spec.definition())
            offered.add(name)
    return defs, offered


def enforce_action_tool_calls(calls: List[Dict[str, Any]], offered: Set[str], enabled: List[str],
                              user: str, sec, max_calls: int) -> Tuple[List[Dict[str, Any]], int]:
    """Validate a list of tool_calls already known to be ACTION_TOOLS names (the caller splits by
    registry before calling this, so this never sees a read-only tool's call). Returns
    (kept calls, number blocked), in the same canonical shape gateway.permissions uses."""
    kept: List[Dict[str, Any]] = []
    blocked = 0
    seen_ids: Set[str] = set()
    for call in calls:
        fn = call.get("function") if isinstance(call, dict) else None
        name = fn.get("name") if isinstance(fn, dict) else None
        if not isinstance(name, str) or get_action_spec(name, enabled) is None:
            blocked += 1
            if sec:
                sec.write(user, "action_call_blocked", name or "", "not_in_action_allowlist")
            continue
        if name not in offered:
            blocked += 1
            if sec:
                sec.write(user, "action_call_blocked", name, "not_offered_this_turn")
            continue
        if len(kept) >= max_calls:
            blocked += 1
            if sec:
                sec.write(user, "action_call_blocked", name, "too_many_calls")
            continue
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except ValueError:
                blocked += 1
                if sec:
                    sec.write(user, "action_call_blocked", name, "arguments_not_json")
                continue
        try:
            validate_args(ACTION_TOOLS[name].schema, args)
        except ToolError:
            blocked += 1
            if sec:
                sec.write(user, "action_call_blocked", name, "invalid_arguments")
            continue
        cid = call.get("id") if isinstance(call.get("id"), str) and call.get("id") else ""
        if not cid or cid in seen_ids:
            cid = "call_" + uuid.uuid4().hex[:16]
        seen_ids.add(cid)
        kept.append({"id": cid, "type": "function",
                     "function": {"name": name, "arguments": json.dumps(args)}})
    return kept, blocked
