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
from .taint import RESERVED_SOURCES, SourceRegistry, model_output_taint
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
    # message is taken as this turn's input.
    if last_user is None or not last_user.strip():
        raise BadRequest("no_user_message")

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
        documents=documents,
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
    text = payload["choices"][0]["message"]["content"]
    chunk = {**{k: payload[k] for k in ("id", "created", "model", "lane", "taint", "frontier_offer")},
             "object": "chat.completion.chunk",
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                          "finish_reason": "stop"}]}

    async def gen() -> AsyncIterator[bytes]:
        yield f"data: {json.dumps(chunk)}\n\n".encode()
        yield b"data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"X-Lane": lane})


def _private_unavailable(taint: Taint) -> JSONResponse:
    return _json({"lane": "private", "taint": taint.name, "error": "local_model_unavailable"},
                 "private", 503)


async def handle_chat(cfg: Settings, registry: SourceRegistry, audit: AuditLog, session: Session,
                      inp: ChatInput, rag: Optional[Retriever] = None):
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
        session.turn += 1
        turn = session.turn
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
        wanted = not inp.documents and (inp.lane_pref == "frontier" or (
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
                out = Fragment(res.text, model_output_taint(session.fragments), "assistant", turn, "assistant")
                session.add(out)
                p = _decorate(_envelope(res.text, res.model,
                                        {"prompt_tokens": res.tokens_in, "completion_tokens": res.tokens_out}),
                              "frontier", taint, False)
                return _sse_single(p, "frontier") if inp.stream else _json(p, "frontier")

        offer = taint == Taint.CLEAN and inp.lane_pref != "private"
        snapshot = session.fragments

    # ---- private lane (lock released; the model call can be slow) ----
    ctx_taint = model_output_taint(snapshot)
    extra = cfg.rag.system_prompt if inp.documents else None
    body = {"model": model_id, "messages": build_local_messages(cfg, snapshot, extra), **inp.params}
    url = cfg.model_server.base_url.rstrip("/") + "/chat/completions"

    def record(text: str) -> None:
        if text:
            session.add(Fragment(text, max(ctx_taint, session.taint), "assistant", turn, "assistant"))

    if inp.stream:
        return await _private_stream(cfg, url, {**body, "stream": True}, ctx_taint, offer, status, record,
                                     citations)

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
        record(data["choices"][0]["message"].get("content") or "")
    except (KeyError, IndexError, TypeError, AttributeError):
        pass
    return _json(_decorate(data, "private", ctx_taint, offer, status, citations), "private")


async def _private_stream(cfg: Settings, url: str, body: dict, taint: Taint, offer: bool,
                          status: Optional[str], record, citations: Optional[list] = None):
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
        try:
            yield f": lane=private taint={taint.name} frontier_offer={str(offer).lower()}\n\n".encode()
            if citations is not None:
                yield f": citations={json.dumps(citations)}\n\n".encode()
            async for line in upstream.aiter_lines():
                if line.startswith("data:") and "[DONE]" not in line:
                    try:
                        delta = json.loads(line[5:].strip())["choices"][0].get("delta", {})
                        parts.append(delta.get("content") or "")
                    except (ValueError, KeyError, IndexError, TypeError):
                        pass
                yield (line + "\n").encode()
        except httpx.HTTPError:
            yield b'data: {"lane":"private","error":"local_model_unavailable"}\n\n'
        finally:
            record("".join(parts))      # output inherits the context taint, even if cut short
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(gen(), status_code=upstream.status_code, media_type="text/event-stream",
                             headers={"X-Lane": "private"})
