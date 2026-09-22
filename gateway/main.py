"""Private AI Agent Template gateway (FastAPI).

Two lanes: PRIVATE (default, local model only) and FRONTIER (Claude via commercial API, only
through egress()). Auth is a per-user bearer token; each user has an isolated session.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response

from . import action_tools as actionmod
from .audit import ActionAuditLog, ActionAuditRecord, AuditLog
from .config import Settings, get_settings
from . import tools as toolmod
from .lanes import BadRequest, handle_chat, parse_chat
from .permissions import SecurityLog
from .session import SessionStore
from .taint import SourceRegistry
from rag.factory import build_retriever
from rag import loaders as rag_loaders

MAX_UPLOAD_BYTES = 10 * 1024 * 1024   # 10 MB PDF cap for the UI's attach-a-file feature
MAX_EXTRACTED_CHARS = 300_000         # matches the plain-text attach cap in the UI

log = logging.getLogger("gateway")


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    cfg = settings or get_settings()
    app = FastAPI(title="Private AI Agent Template gateway")
    registry = SourceRegistry(cfg.sources)
    audit = AuditLog(cfg.audit.path)
    sec = SecurityLog(cfg.security.path)
    action_audit = ActionAuditLog(cfg.actions.audit_path)
    sessions = SessionStore()
    rag = build_retriever(cfg) if cfg.rag.enabled else None
    google: Optional[actionmod.GoogleClient] = None
    if cfg.actions.enabled:
        og = cfg.actions.oauth_google
        cid = os.environ.get(og.client_id_env, "")
        csec = os.environ.get(og.client_secret_env, "")
        rtok = os.environ.get(og.refresh_token_env, "")
        if cid and csec and rtok:
            google = actionmod.GoogleClient(cid, csec, rtok)
        else:
            log.warning("actions enabled but Google OAuth env vars are not set; "
                       "gmail_* / calendar_* actions will fail until they are")
    tokens: List[Tuple[str, str]] = []
    for u in cfg.users:
        tok = os.environ.get(u.token_env, "")
        if tok:
            tokens.append((u.id, tok))
        else:
            log.warning("user %s has no token in env %s; disabled", u.id, u.token_env)
    app.state.cfg, app.state.audit, app.state.sessions, app.state.rag = cfg, audit, sessions, rag
    app.state.sec = sec

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
            return await handle_chat(cfg, registry, audit, sessions.get(uid), inp, rag, sec)
        except BadRequest as e:
            return JSONResponse({"error": e.code}, status_code=e.status)

    @app.get("/v1/tools")
    async def tool_definitions(request: Request):
        """The canonical read-only tool definitions an agent may offer to the model."""
        if authenticate(request) is None:
            return unauthorized()
        return {"tools": [toolmod.READ_ONLY_TOOLS[n].definition() for n in cfg.tools.enabled]}

    @app.post("/v1/tools/{name}")
    async def run_tool(name: str, request: Request):
        """Execute one read-only tool for the authenticated user. Anything outside the code
        allowlist (write, exec, web, ...) is refused here and logged."""
        uid = authenticate(request)
        if uid is None:
            return unauthorized()
        if toolmod.get_spec(name, cfg.tools.enabled) is None:
            sec.write(uid, "tool_call_blocked", name, "not_in_readonly_allowlist")
            return JSONResponse({"error": "tool_not_permitted"}, status_code=403)
        try:
            payload = await request.json()
            args = payload.get("arguments", {}) if isinstance(payload, dict) else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            args = None
        ctx = toolmod.ToolContext(uid, Path(cfg.tools.files_root), cfg.tools.max_result_chars,
                                  cfg.tools.max_file_bytes, rag)
        try:
            content = await toolmod.execute(name, args, ctx, cfg.tools.enabled)
        except toolmod.ToolError as e:
            return JSONResponse({"tool": name, "error": str(e)}, status_code=400)
        # The taint shown here is advisory. The gateway assigns the real taint itself when the
        # result comes back in a chat turn, and it never trusts a client-supplied value.
        return {"tool": name, "content": content, "taint": "PRIVATE"}

    @app.get("/v1/actions")
    async def action_definitions(request: Request):
        """The canonical action-tool definitions enabled for this deployment. Empty unless the
        operator explicitly enabled some in actions.enabled (empty by default)."""
        if authenticate(request) is None:
            return unauthorized()
        return {"actions": [actionmod.ACTION_TOOLS[n].definition() for n in cfg.actions.enabled]}

    @app.post("/v1/actions/{name}")
    async def run_action(name: str, request: Request):
        """Execute or stage one action tool for the authenticated user.

        Body: {"arguments": {...}, "confirm": bool, "context_hint": str}. `context_hint` is used
        ONLY for tools whose confirmation requirement is "judge", and must be the user's own typed
        words - never text drawn from a document, email, or other untrusted source (see the
        warning in gateway/action_tools.py). If the tool requires confirmation and confirm is not
        true, nothing is executed: the call is staged and reported back, and logged as such.
        Known gap: this endpoint has no idempotency key, so repeating a confirm=true call executes
        it again (see docs/KNOWN_GAPS.md)."""
        uid = authenticate(request)
        if uid is None:
            return unauthorized()
        spec = actionmod.get_action_spec(name, cfg.actions.enabled)
        if spec is None:
            sec.write(uid, "action_call_blocked", name, "not_in_action_allowlist")
            return JSONResponse({"error": "action_not_permitted"}, status_code=403)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        args = payload.get("arguments", {})
        confirm = bool(payload.get("confirm") is True)
        context_hint = payload.get("context_hint")
        args_hash = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()

        needs_confirmation = spec.requires_confirmation
        if needs_confirmation == "judge":
            hint = context_hint if isinstance(context_hint, str) else ""
            needs_confirmation = actionmod.judge_confirmation_needed(hint)

        if needs_confirmation and not confirm:
            sec.write(uid, "action_staged", name, "confirmation_required")
            action_audit.write(ActionAuditRecord(
                ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                user=uid, tool=name, args_sha256=args_hash, status="staged"))
            return {"status": "needs_confirmation", "action": name}

        ctx = actionmod.ActionContext(uid, cfg.actions.reminders_dir, google)
        status = "error"
        try:
            content = await actionmod.execute_action(name, args, ctx, cfg.actions.enabled)
            status = "executed"
            return {"action": name, "status": "executed", "result": content}
        except actionmod.ActionError as e:
            return JSONResponse({"action": name, "status": "error", "error": str(e)}, status_code=400)
        finally:
            action_audit.write(ActionAuditRecord(
                ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                user=uid, tool=name, args_sha256=args_hash, status=status))

    @app.get("/v1/actions-audit")
    async def my_action_audit(request: Request, limit: int = 50):
        """The caller's own action records (hashes and counts only), newest first."""
        uid = authenticate(request)
        if uid is None:
            return unauthorized()
        return {"records": action_audit.records_for(uid, max(1, min(limit, 200)))}

    @app.get("/v1/audit")
    async def my_audit(request: Request, limit: int = 50):
        """The caller's own egress records (hashes and counts only), newest first."""
        uid = authenticate(request)
        if uid is None:
            return unauthorized()
        return {"records": audit.records_for(uid, max(1, min(limit, 200)))}

    @app.get("/v1/security")
    async def my_security_events(request: Request, limit: int = 50):
        """The caller's own blocked-action events (tool name and reason only), newest first."""
        uid = authenticate(request)
        if uid is None:
            return unauthorized()
        return {"events": sec.events(uid, max(1, min(limit, 200)))}

    @app.post("/v1/extract-text")
    async def extract_text(request: Request, file: UploadFile = File(...)):
        """Extract text from an uploaded PDF for the UI's file-attach feature. Stateless: nothing is
        stored, the temp file is deleted immediately, and the extracted text is returned to the
        caller only. It never touches a session, the document store, or taint - the browser adds the
        returned text as ordinary context, which is tainted the same as any other attachment."""
        uid = authenticate(request)
        if uid is None:
            return unauthorized()
        if not (file.filename or "").lower().endswith(".pdf"):
            return JSONResponse({"error": "only_pdf_supported"}, status_code=400)
        data = await file.read()
        if len(data) > MAX_UPLOAD_BYTES:
            return JSONResponse({"error": "file_too_large"}, status_code=413)
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            tmp.write(data)
            tmp.flush()
            try:
                text = rag_loaders.load_text(Path(tmp.name))
            except rag_loaders.RagError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
        if not text.strip():
            return JSONResponse({"error": "no_text_found"}, status_code=400)
        return {"text": text[:MAX_EXTRACTED_CHARS]}

    # ---- built-in test UI: static files, same origin, no outside assets ----
    ui_dir = Path(__file__).parent / "ui"
    ui_files = {"app.js": "text/javascript", "style.css": "text/css"}
    ui_headers = {
        # Nothing but our own script and stylesheet may run or load. No images at all, so a reply
        # can never make the browser fetch a URL. Requests may only go back to this origin.
        "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self'; "
                                    "connect-src 'self'; img-src 'none'; base-uri 'none'; "
                                    "form-action 'none'; frame-ancestors 'none'"),
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store", "X-Frame-Options": "DENY",
    }
    if cfg.ui.enabled:
        @app.get("/", include_in_schema=False)
        async def root():
            return RedirectResponse("/ui")

        @app.get("/ui", include_in_schema=False)
        async def ui_index():
            return Response((ui_dir / "index.html").read_bytes(), media_type="text/html", headers=ui_headers)

        @app.get("/ui/{name}", include_in_schema=False)
        async def ui_asset(name: str):
            if name not in ui_files:                     # fixed allowlist: no path is ever built from input
                return Response(status_code=404)
            return Response((ui_dir / name).read_bytes(), media_type=ui_files[name], headers=ui_headers)

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
