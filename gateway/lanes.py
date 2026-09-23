"""Lane routing. Mechanism, not judgment: the decision is a pure function of taint, config,
and explicit user flags. No model output is ever read to choose a lane.

  PRIVATE context            -> private lane. No offer, no egress. Nothing can change this.
  CLEAN, auto_route_clean=T  -> frontier (through egress()).
  CLEAN, auto_route_clean=F  -> private lane, frontier offered. Frontier only after consent.

The private lane fails closed: if the local model is unreachable the caller gets a 503 from the
private lane. There is no code path from a local failure to the frontier.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx
from fastapi.responses import JSONResponse, StreamingResponse

from .audit import AuditLog
from .config import Settings
from .egress import (AttachmentsBlocked, EgressBlocked, EgressRequest, FrontierNotConfigured,
                     FrontierUnavailable, TaintBlocked, egress)
from .fragments import Fragment, Taint
from .session import Session
from . import action_tools as actiontools
from .permissions import BLOCKED_TEXT, SecurityLog, enforce_tool_calls, filter_client_tools
from .sanitize import StreamSanitizer, sanitize_reply, strip_active_content, strip_text_tool_calls
from .taint import RESERVED_SOURCES, SourceRegistry, model_output_taint, tool_output_taint
from rag.retrieve import NOT_IN_DOCUMENTS, Retriever
from rag.types import RagError

MAX_CONTEXT_ITEMS = 50
MAX_CONTEXT_CHARS = 400_000
PASSTHROUGH = ("temperature", "top_p", "max_tokens", "stop", "seed",
               "presence_penalty", "frequency_penalty")


class BadRequest(Exception):
    def __init__(self, code: str, status: int = 400):
        self.code, self.status = code, status


@dataclass
class ChatInput:
    user_text: str
    has_attachments: bool
    context: List[Tuple[str, str]]          # (text, source)
    lane_pref: str                          # auto | private | frontier
    consent: Optional[bool]
    stream: bool
    model: Optional[str]
    documents: bool = False
    client_tools: List[str] = field(default_factory=list)      # names only; defs are ours
    tool_choice: Optional[str] = None
    tool_results: List[Tuple[str, str]] = field(default_factory=list)   # (tool_call_id, content)
    params: Dict[str, Any] = field(default_factory=dict)


def _flatten(content: Any) -> Tuple[str, bool]:
    if isinstance(content, str):
        return content, False
    if isinstance(content, list):
        texts, nontext = [], False
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
            else:
                nontext = True
        return "\n".join(texts), nontext
    return "", True


def parse_chat(body: Any, header_model: Optional[str] = None) -> ChatInput:
    if not isinstance(body, dict):
        raise BadRequest("invalid_body")
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        raise BadRequest("messages_required")
    has_attach = bool(body.get("attachments") or body.get("files") or body.get("images"))
    last_user: Optional[str] = None
    for m in msgs:
        if not isinstance(m, dict):
            raise BadRequest("invalid_message")
        text, nontext = _flatten(m.get("content"))
        has_attach = has_attach or nontext
        if m.get("role") == "user":
            last_user = text
    # The session, not the client, is the source of truth for history. Only the newest user
    # message (or, when the agent is returning tool results, the trailing tool messages) is taken
    # as this turn's input.
    tool_results: List[Tuple[str, str]] = []
    if msgs and msgs[-1].get("role") == "tool":
        i = len(msgs)
        while i > 0 and msgs[i - 1].get("role") == "tool":
            i -= 1
        for m in msgs[i:]:
            tid = m.get("tool_call_id")
            if not isinstance(tid, str) or not tid:
                raise BadRequest("invalid_tool_message")
            tool_results.append((tid, _flatten(m.get("content"))[0]))
        last_user = ""
    elif last_user is None or not last_user.strip():
        raise BadRequest("no_user_message")

    raw_tools = body.get("tools") or []
    if not isinstance(raw_tools, list) or len(raw_tools) > 64:
        raise BadRequest("invalid_tools")
    tool_names: List[str] = []
    for t in raw_tools:
        fn = t.get("function") if isinstance(t, dict) else None
        name = fn.get("name") if isinstance(fn, dict) else None
        if not isinstance(name, str) or not name or len(name) > 64:
            raise BadRequest("invalid_tools")
        tool_names.append(name)
    tc = body.get("tool_choice")

    ctx: List[Tuple[str, str]] = []
    raw_ctx = body.get("context", [])
    if not isinstance(raw_ctx, list) or len(raw_ctx) > MAX_CONTEXT_ITEMS:
        raise BadRequest("invalid_context")
    total = 0
    for item in raw_ctx:
        if not (isinstance(item, dict) and isinstance(item.get("text"), str)
                and isinstance(item.get("source"), str) and item["source"]):
            raise BadRequest("invalid_context")
        if item["source"] in RESERVED_SOURCES:
            raise BadRequest("reserved_source")
        total += len(item["text"])
        ctx.append((item["text"], item["source"]))
    if total > MAX_CONTEXT_CHARS:
        raise BadRequest("context_too_large", 413)

    lane = body.get("lane", "auto")
    if lane not in ("auto", "private", "frontier"):
        raise BadRequest("invalid_lane")
    documents = body.get("documents", False)
    if not isinstance(documents, bool):
        raise BadRequest("invalid_documents")
    consent = body.get("consent")
    if consent is not None and not isinstance(consent, bool):
        raise BadRequest("invalid_consent")
    return ChatInput(
        user_text=last_user, has_attachments=has_attach, context=ctx, lane_pref=lane,
        consent=consent, stream=bool(body.get("stream")), model=body.get("model") or header_model,
        documents=documents, client_tools=tool_names, tool_choice=tc if isinstance(tc, str) else None,
        tool_results=tool_results,
        params={k: body[k] for k in PASSTHROUGH if k in body},
    )


def _safe_label(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:64]


def build_local_messages(cfg: Settings, fragments: List[Fragment],
                         extra_system: Optional[str] = None) -> List[Dict[str, str]]:
    system = cfg.models.system_prompt + (" " + extra_system if extra_system else "")
    msgs = [{"role": "system", "content": system}]
    for f in fragments:
        if f.role == "context":
            text = f.text.replace("</context", "<\\/context")
            msgs.append({"role": "user",
                         "content": f'<context source="{_safe_label(f.origin)}">\n{text}\n</context>'})
        elif f.role == "tool":
            name = _safe_label(f.origin.split(":", 1)[-1])
            text = f.text.replace("</tool_output", "<\\/tool_output")
            msgs.append({"role": "tool", "tool_call_id": f.tool_call_id,
                         "content": f'<tool_output name="{name}">\n{text}\n</tool_output>'})
        elif f.role == "assistant" and f.tool_calls:
            msgs.append({"role": "assistant", "content": f.text, "tool_calls": json.loads(f.tool_calls)})
        else:
            msgs.append({"role": f.role, "content": f.text})
    return msgs


def _envelope(text: str, model: str, usage: Any = None) -> Dict[str, Any]:
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24], "object": "chat.completion",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        **({"usage": usage} if usage else {}),
    }


def _decorate(payload: Dict[str, Any], lane: str, taint: Taint, offer: bool,
              status: Optional[str] = None, citations: Optional[list] = None) -> Dict[str, Any]:
    payload["lane"] = lane
    payload["taint"] = taint.name
    payload["frontier_offer"] = offer
    if status:
        payload["frontier_status"] = status
    if citations is not None:
        payload["citations"] = citations
    return payload


def _json(payload: Dict[str, Any], lane: str, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers={"X-Lane": lane})


def _sse_single(payload: Dict[str, Any], lane: str) -> StreamingResponse:
    choice = payload["choices"][0]
    delta = {"role": "assistant", "content": choice["message"].get("content") or ""}
    if choice["message"].get("tool_calls"):
        delta["tool_calls"] = [{"index": i, **c} for i, c in enumerate(choice["message"]["tool_calls"])]
    chunk = {"id": payload.get("id") or "chatcmpl-" + uuid.uuid4().hex[:24],
             "created": payload.get("created") or int(time.time()), "model": payload.get("model", ""),
             "lane": payload["lane"], "taint": payload["taint"], "frontier_offer": payload["frontier_offer"],
             "object": "chat.completion.chunk",
             "choices": [{"index": 0, "delta": delta, "finish_reason": choice.get("finish_reason", "stop")}]}

    async def gen() -> AsyncIterator[bytes]:
        yield f"data: {json.dumps(chunk)}\n\n".encode()
        yield b"data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"X-Lane": lane})


def _private_unavailable(taint: Taint) -> JSONResponse:
    return _json({"lane": "private", "taint": taint.name, "error": "local_model_unavailable"},
                 "private", 503)


def _clean_reply(cfg: Settings, sec: Optional[SecurityLog], user: str, text: str) -> str:
    """Remove text-form tool calls and (if enabled) auto-loading images / active HTML."""
    text, n1 = strip_text_tool_calls(text)
    n2 = 0
    if cfg.security.sanitize_output:
        text, n2 = strip_active_content(text)
    if (n1 or n2) and sec:
        sec.write(user, "output_sanitized", "", f"removed_{n1 + n2}")
    return text


def _enforce_mixed_tool_calls(msg: Dict[str, Any], offered: set, cfg: Settings, uid: str,
                              sec: Optional[SecurityLog]) -> Tuple[List[Dict[str, Any]], int]:
    """A model turn can offer both read-only and action tool definitions together. Each call in
    the model's reply is validated against exactly its own registry - never the other - so a
    valid action-tool call is never misclassified and dropped as an unauthorized read-only call,
    and vice versa. A name in neither registry is blocked and logged once, by the read-only
    enforcer (which every other unknown-tool test in this project already exercises)."""
    from .tools import READ_ONLY_TOOLS
    from .action_tools import ACTION_TOOLS
    raw = msg.get("tool_calls") or []
    if not isinstance(raw, list):
        raw = []

    def name_of(c):
        fn = c.get("function") if isinstance(c, dict) else None
        return fn.get("name") if isinstance(fn, dict) else None

    ro_calls = [c for c in raw if name_of(c) in READ_ONLY_TOOLS]
    action_calls = [c for c in raw if name_of(c) in ACTION_TOOLS]
    other_calls = [c for c in raw if name_of(c) not in READ_ONLY_TOOLS and name_of(c) not in ACTION_TOOLS]

    msg["tool_calls"] = ro_calls + other_calls    # unknown names fall through to the real enforcer
    kept_ro, blocked_ro = enforce_tool_calls(msg, offered, cfg.tools.enabled, uid, sec,
                                             cfg.tools.max_calls_per_turn)
    kept_action, blocked_action = actiontools.enforce_action_tool_calls(
        action_calls, offered, cfg.actions.enabled, uid, sec, cfg.tools.max_calls_per_turn)

    kept = kept_ro + kept_action
    if kept:
        msg["tool_calls"] = kept
    else:
        msg.pop("tool_calls", None)
    return kept, blocked_ro + blocked_action


async def handle_chat(cfg: Settings, registry: SourceRegistry, audit: AuditLog, session: Session,
                      inp: ChatInput, rag: Optional[Retriever] = None, sec: Optional[SecurityLog] = None):
    # ---- /new: the only way taint is ever cleared ----
    if inp.user_text.strip() == "/new":
        session.clear()
        p = _decorate(_envelope("New session started. Context, taint and consent cleared.", "gateway"),
                      "private", Taint.CLEAN, False)
        return _sse_single(p, "private") if inp.stream else _json(p, "private")

    try:
        model_id = cfg.resolve_model(inp.model)
    except KeyError:
        raise BadRequest("unknown_model")

    # ---- tools: canonical read-only definitions only; tool turns are private-lane only ----
    tools_mode = bool(inp.client_tools or inp.tool_results)
    if tools_mode and inp.lane_pref == "frontier":
        return _json({"lane": "none", "taint": session.taint.name, "error": "tools_private_lane_only"},
                     "none", 403)
    if inp.tool_results and inp.documents:
        raise BadRequest("documents_with_tool_results")
    offered_defs, offered = filter_client_tools(inp.client_tools, cfg.tools.enabled, session.user_id, sec)
    # Action tools (reminders, Gmail, Calendar) are a separate registry from the read-only tools
    # above, offered the same way: only names both in code (ACTION_TOOLS) and enabled by config
    # survive. A name that filter_client_tools already dropped as "not a read-only tool" is
    # checked again here against the action registry, so a client can request either kind by name
    # in the same `tools` list and get whichever definition actually matches.
    action_defs, action_offered = actiontools.filter_client_action_tools(
        inp.client_tools, cfg.actions.enabled, session.user_id, sec)
    offered_defs = offered_defs + action_defs
    offered = offered | action_offered

    # ---- private documents: retrieval feeds the private lane only ----
    hits = []
    if inp.documents:
        if rag is None or not cfg.rag.enabled:
            raise BadRequest("rag_disabled")
        if inp.lane_pref == "frontier":
            return _json({"lane": "none", "taint": "PRIVATE", "error": "documents_private_lane_only"},
                         "none", 403)
        try:
            hits = await rag.retrieve(session.user_id, inp.user_text)     # scoped to this user
        except RagError:
            return _json({"lane": "private", "taint": "PRIVATE", "error": "retrieval_unavailable"},
                         "private", 503)
    citations = [{"doc": h.doc_name, "chunk": h.chunk_idx, "source": h.source, "score": round(h.score, 3)}
                 for h in hits] if inp.documents else None

    fc = cfg.frontier
    async with session.lock:
        # Only calls this gateway issued can receive a result. Check everything before mutating.
        for tid, _ in inp.tool_results:
            if tid not in session.pending_calls:
                raise BadRequest("unexpected_tool_result")
        session.turn += 1
        turn = session.turn
        if inp.tool_results:
            for tid, content in inp.tool_results:
                name, arg_taint = session.pending_calls.pop(tid)
                # Output inherits the tool's registry taint (default PRIVATE) AND the taint of the
                # arguments, which the model produced from the whole context. A client cannot pick it.
                t = tool_output_taint(registry.taint_of("tool:" + name), [arg_taint])
                session.add(Fragment(content[: cfg.tools.max_result_chars], t, "tool:" + name, turn,
                                     "tool", tool_call_id=tid))
        else:
            for tid, (name, arg_taint) in list(session.pending_calls.items()):
                session.add(Fragment("error: this tool call was not executed", arg_taint, "tool:" + name,
                                     turn, "tool", tool_call_id=tid))
            session.pending_calls.clear()
            for text, source in inp.context:
                session.add(Fragment(text, registry.taint_of(source), source, turn, "context"))
            for h in hits:
                # A chunk inherits its document's taint. The registry can only make it stricter.
                session.add(Fragment(h.text, max(h.taint, registry.taint_of(h.source)),
                                     f"{h.source}/{h.doc_name}", turn, "context"))
            # Touching the private document store is itself private: the query is tagged PRIVATE
            # (an unregistered origin), so even a zero-hit query taints the session.
            session.add(Fragment(inp.user_text,
                                 registry.taint_of("document_query" if inp.documents else "user_message"),
                                 "document_query" if inp.documents else "user_message", turn, "user"))

        if inp.documents and not hits:
            session.add(Fragment(NOT_IN_DOCUMENTS, session.taint, "assistant", turn, "assistant"))
            p = _decorate(_envelope(NOT_IN_DOCUMENTS, "retrieval-gate"), "private", session.taint, False,
                          citations=[])
            return _sse_single(p, "private") if inp.stream else _json(p, "private")

        if inp.consent is not None and fc.consent_scope == "session":
            session.consent = inp.consent
        consent_present = session.consent if fc.consent_scope == "session" else bool(inp.consent)

        taint = session.taint
        wanted = not inp.documents and not tools_mode and (inp.lane_pref == "frontier" or (
            inp.lane_pref == "auto" and (fc.auto_route_clean or consent_present)))
        status: Optional[str] = None

        if wanted:
            try:
                res = await egress(cfg, audit, session, EgressRequest(
                    user_text=inp.user_text, has_attachments=inp.has_attachments,
                    request_consent=bool(inp.consent)))
            except (FrontierUnavailable, FrontierNotConfigured) as e:
                return _json({"lane": "frontier", "taint": taint.name, "error": e.reason}, "frontier", e.status)
            except EgressBlocked as e:
                if inp.lane_pref == "frontier":
                    offer = taint == Taint.CLEAN
                    return _json({"lane": "none", "taint": taint.name, "error": e.reason,
                                  "frontier_offer": offer}, "none", e.status)
                # Implicit routing that egress refused: answer privately.
                status = e.reason if isinstance(e, AttachmentsBlocked) else None
            else:
                reply = _clean_reply(cfg, sec, session.user_id, res.text)
                out = Fragment(reply, model_output_taint(session.fragments), "assistant", turn, "assistant")
                session.add(out)
                p = _decorate(_envelope(reply, res.model,
                                        {"prompt_tokens": res.tokens_in, "completion_tokens": res.tokens_out}),
                              "frontier", taint, False)
                return _sse_single(p, "frontier") if inp.stream else _json(p, "frontier")

        offer = taint == Taint.CLEAN and inp.lane_pref != "private"
        snapshot = session.fragments

    # ---- private lane (lock released; the model call can be slow) ----
    ctx_taint = model_output_taint(snapshot)
    has_tool_history = any(f.role == "tool" for f in snapshot)
    extra = " ".join(x for x in (cfg.rag.system_prompt if inp.documents else None,
                                 cfg.tools.system_prompt if (tools_mode or has_tool_history) else None) if x) or None
    body = {**cfg.models.extra_body, "model": model_id,
            "messages": build_local_messages(cfg, snapshot, extra), **inp.params}
    if offered_defs:
        body["tools"] = offered_defs
        if inp.tool_choice in ("auto", "none", "required"):
            body["tool_choice"] = inp.tool_choice
    url = cfg.model_server.base_url.rstrip("/") + "/chat/completions"
    uid = session.user_id

    def record(text: str, calls: Optional[List[Dict[str, Any]]] = None) -> None:
        if not text and not calls:
            return
        frag = Fragment(text, max(ctx_taint, session.taint), "assistant", turn, "assistant",
                        tool_calls=json.dumps(calls) if calls else None)
        session.add(frag)
        for c in calls or []:
            session.pending_calls[c["id"]] = (c["function"]["name"], frag.taint)

    # Streaming is passed through (with sanitising and tool-call dropping) only when no tools are
    # offered. With tools, the reply is buffered so every tool call is checked before it is emitted.
    if inp.stream and not offered_defs:
        return await _private_stream(cfg, url, {**body, "stream": True}, ctx_taint, offer, status, record,
                                     citations, sec, uid)

    try:
        async with httpx.AsyncClient(timeout=cfg.model_server.timeout_seconds) as c:
            r = await c.post(url, json=body)
    except httpx.HTTPError:
        return _private_unavailable(taint)
    if r.status_code >= 500:
        return _private_unavailable(taint)
    try:
        data = r.json()
    except ValueError:
        return _private_unavailable(taint)
    if r.status_code >= 400:
        return _json(_decorate(data if isinstance(data, dict) else {"error": "upstream_error"},
                               "private", ctx_taint, offer, status, citations), "private", r.status_code)
    try:
        choice = data["choices"][0]
        data["choices"] = [choice]
        msg = choice.setdefault("message", {})
        text = msg.get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        return _private_unavailable(taint)
    kept, blocked = _enforce_mixed_tool_calls(msg, offered, cfg, uid, sec)
    text = _clean_reply(cfg, sec, uid, text)
    if blocked and not kept:
        text = (text + "\n\n" if text.strip() else "") + BLOCKED_TEXT
    msg["content"] = text
    choice["finish_reason"] = "tool_calls" if kept else "stop"
    record(text, kept)
    p = _decorate(data, "private", ctx_taint, offer, status, citations)
    return _sse_single(p, "private") if inp.stream else _json(p, "private")


async def _private_stream(cfg: Settings, url: str, body: dict, taint: Taint, offer: bool,
                          status: Optional[str], record, citations: Optional[list] = None,
                          sec: Optional[SecurityLog] = None, user: str = ""):
    client = httpx.AsyncClient(timeout=cfg.model_server.timeout_seconds)
    try:
        upstream = await client.send(client.build_request("POST", url, json=body), stream=True)
    except httpx.HTTPError:
        await client.aclose()
        return _private_unavailable(taint)
    if upstream.status_code >= 500:
        await upstream.aclose()
        await client.aclose()
        return _private_unavailable(taint)

    async def gen() -> AsyncIterator[bytes]:
        parts: List[str] = []
        san = StreamSanitizer()
        last_obj: Optional[dict] = None
        dropped = 0

        def data_line(obj: dict) -> bytes:
            return f"data: {json.dumps(obj)}\n".encode()

        try:
            yield f": lane=private taint={taint.name} frontier_offer={str(offer).lower()}\n\n".encode()
            if citations is not None:
                yield f": citations={json.dumps(citations)}\n\n".encode()
            async for line in upstream.aiter_lines():
                if line.startswith("data:") and "[DONE]" not in line:
                    try:
                        obj = json.loads(line[5:].strip())
                        delta = obj["choices"][0].setdefault("delta", {})
                    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                        continue                    # unparseable data lines are dropped, not forwarded
                    if delta.pop("tool_calls", None) is not None:
                        dropped += 1                # nothing offered a tool, so none may be issued
                    piece = delta.get("content")
                    if isinstance(piece, str):
                        delta["content"] = san.feed(piece) if cfg.security.sanitize_output else strip_text_tool_calls(piece)[0]
                        parts.append(delta["content"])
                    last_obj = obj
                    yield data_line(obj)
                elif "[DONE]" in line:
                    tail = san.flush() if cfg.security.sanitize_output else ""
                    if tail and last_obj is not None:
                        tail_obj = json.loads(json.dumps(last_obj))
                        tail_obj["choices"][0] = {"index": 0, "delta": {"content": tail}, "finish_reason": None}
                        parts.append(tail)
                        yield data_line(tail_obj) + b"\n"
                    yield (line + "\n").encode()
                else:
                    yield (line + "\n").encode()
        except httpx.HTTPError:
            yield b'data: {"lane":"private","error":"local_model_unavailable"}\n\n'
        finally:
            if sec and (dropped or san.removed):
                sec.write(user, "stream_sanitized", "", f"tool_calls_{dropped}_removed_{san.removed}")
            record("".join(parts))      # output inherits the context taint, even if cut short
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(gen(), status_code=upstream.status_code, media_type="text/event-stream",
                             headers={"X-Lane": "private"})
