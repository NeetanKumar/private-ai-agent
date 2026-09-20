"""Private AI Agent Template gateway (FastAPI).

Two lanes: PRIVATE (default, local model only) and FRONTIER (Claude via commercial API, only
through egress()). Auth is a per-user bearer token; each user has an isolated session.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
from typing import Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .audit import AuditLog
from .config import Settings, get_settings
from .lanes import BadRequest, handle_chat, parse_chat
from .session import SessionStore
from .taint import SourceRegistry
from rag.factory import build_retriever

log = logging.getLogger("gateway")


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    cfg = settings or get_settings()
    app = FastAPI(title="Private AI Agent Template gateway")
    registry = SourceRegistry(cfg.sources)
    audit = AuditLog(cfg.audit.path)
    sessions = SessionStore()
    rag = build_retriever(cfg) if cfg.rag.enabled else None
    tokens: List[Tuple[str, str]] = []
    for u in cfg.users:
        tok = os.environ.get(u.token_env, "")
        if tok:
            tokens.append((u.id, tok))
        else:
            log.warning("user %s has no token in env %s; disabled", u.id, u.token_env)
    app.state.cfg, app.state.audit, app.state.sessions, app.state.rag = cfg, audit, sessions, rag

    def authenticate(request: Request) -> Optional[str]:
        h = request.headers.get("authorization", "")
        if not h.lower().startswith("bearer "):
            return None
        presented = h[7:].strip().encode()
        found: Optional[str] = None
        for uid, tok in tokens:                      # no early exit: constant work per user
            if hmac.compare_digest(presented, tok.encode()):
                found = uid
        return found

    def unauthorized() -> JSONResponse:
        return JSONResponse({"error": "unauthorized"}, status_code=401,
                            headers={"WWW-Authenticate": "Bearer"})

    @app.get("/health")
    async def health() -> JSONResponse:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(cfg.model_server.health_url)
                r.raise_for_status()
            return JSONResponse({"status": "ok", "model_server": "up"})
        except httpx.HTTPError:
            return JSONResponse({"status": "degraded", "model_server": "down"}, status_code=503)

    @app.get("/v1/models")
    async def list_models(request: Request):
        if authenticate(request) is None:
            return unauthorized()
        return {"object": "list", "data": [
            {"id": a, "object": "model", "owned_by": "local"} for a in cfg.models.aliases]}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        uid = authenticate(request)
        if uid is None:
            return unauthorized()
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        try:
            inp = parse_chat(body, request.headers.get("X-Model"))
            return await handle_chat(cfg, registry, audit, sessions.get(uid), inp, rag)
        except BadRequest as e:
            return JSONResponse({"error": e.code}, status_code=e.status)

    @app.get("/session")
    async def session_info(request: Request):
        uid = authenticate(request)
        if uid is None:
            return unauthorized()
        s = sessions.get(uid)
        return {"user": uid, "taint": s.taint.name, "fragments": len(s.fragments),
                "consent": s.consent, "consent_scope": cfg.frontier.consent_scope,
                "auto_route_clean": cfg.frontier.auto_route_clean}

    @app.post("/session/new")
    async def session_new(request: Request):
        uid = authenticate(request)
        if uid is None:
            return unauthorized()
        sessions.get(uid).clear()
        return {"user": uid, "taint": "CLEAN", "fragments": 0}

    @app.post("/session/consent")
    async def session_consent(request: Request):
        uid = authenticate(request)
        if uid is None:
            return unauthorized()
        if cfg.frontier.consent_scope != "session":
            return JSONResponse({"error": "consent_scope_is_request"}, status_code=400)
        try:
            val = (await request.json()).get("consent")
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            val = None
        if not isinstance(val, bool):
            return JSONResponse({"error": "invalid_consent"}, status_code=400)
        sessions.get(uid).consent = val
        return {"user": uid, "consent": val}

    return app


app = create_app() if os.environ.get("GATEWAY_CONFIG") or os.path.exists("infra/config.yaml") else None
