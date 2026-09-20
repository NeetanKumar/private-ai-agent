"""THE egress chokepoint. Every outbound frontier call goes through `egress()`.

Enforcement order, all deterministic (no model or classifier is consulted):
  1. taint      context must be CLEAN. Non-overridable: no consent, flag or config can bypass it.
  2. consent    per `auto_route_clean` / `consent_scope`.
  3. strip      only the current user turn is sent. Session fragments, retrieved context, tool
                output and history are never included. Attachments are rejected.
  4. audit      one record per outbound connection attempt. Hashes and counts only.
  5. call
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from urllib.parse import urlparse

from . import frontier_client
from .audit import AuditLog, AuditRecord, now_iso
from .config import Settings
from .fragments import Taint
from .session import Session


class EgressBlocked(Exception):
    reason = "blocked"
    status = 403


class TaintBlocked(EgressBlocked):
    reason = "taint_blocked"
    status = 403


class ConsentRequired(EgressBlocked):
    reason = "consent_required"
    status = 403


class AttachmentsBlocked(EgressBlocked):
    reason = "attachments_blocked"
    status = 400


class FrontierNotConfigured(EgressBlocked):
    reason = "frontier_not_configured"
    status = 503


class FrontierUnavailable(EgressBlocked):
    reason = "frontier_unavailable"
    status = 502


@dataclass(frozen=True)
class EgressRequest:
    user_text: str
    has_attachments: bool = False
    request_consent: bool = False      # per-request consent flag (consent_scope == "request")


@dataclass(frozen=True)
class EgressResult:
    text: str
    tokens_in: int | None
    tokens_out: int | None
    consent_mode: str
    destination: str
    model: str


def _consent_mode(cfg: Settings, session: Session, req: EgressRequest) -> str:
    fc = cfg.frontier
    if fc.auto_route_clean:
        return "auto"
    if fc.consent_scope == "request":
        if req.request_consent:
            return "request"
    elif session.consent:
        return "session"
    raise ConsentRequired()


async def egress(cfg: Settings, audit: AuditLog, session: Session, req: EgressRequest) -> EgressResult:
    # 1. taint: non-overridable.
    if session.taint != Taint.CLEAN:
        raise TaintBlocked()
    # 2. consent.
    mode = _consent_mode(cfg, session, req)
    # 3. strip context, reject attachments.
    if req.has_attachments:
        raise AttachmentsBlocked()
    api_key = os.environ.get(cfg.frontier.api_key_env, "")
    if not api_key:
        raise FrontierNotConfigured()

    destination = urlparse(cfg.frontier.base_url).netloc
    prompt_hash = hashlib.sha256(req.user_text.encode("utf-8")).hexdigest()
    tokens_in = tokens_out = None
    status = "error"
    try:
        # 4/5. Audit is written in `finally`, so every connection attempt has exactly one record.
        res = await frontier_client.send(
            base_url=cfg.frontier.base_url, api_key=api_key, model=cfg.frontier.model,
            max_tokens=cfg.frontier.max_tokens, timeout=cfg.frontier.timeout_seconds,
            system=cfg.frontier.system_prompt, user_text=req.user_text,
        )
        tokens_in, tokens_out, status = res.tokens_in, res.tokens_out, "ok"
        return EgressResult(res.text, tokens_in, tokens_out, mode, destination, cfg.frontier.model)
    except frontier_client.FrontierError:
        raise FrontierUnavailable() from None
    finally:
        audit.write(AuditRecord(
            ts=now_iso(), lane="frontier", user=session.user_id, prompt_sha256=prompt_hash,
            tokens_in=tokens_in, tokens_out=tokens_out, destination=destination,
            consent_mode=mode, status=status,
        ))
