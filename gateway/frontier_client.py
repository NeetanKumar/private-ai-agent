"""Raw HTTP call to the frontier API.

Do not import this module from anywhere except gateway/egress.py. A test enforces that, so the
egress chokepoint is the only path to the frontier.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx


class FrontierError(Exception):
    pass


@dataclass(frozen=True)
class FrontierResult:
    text: str
    tokens_in: Optional[int]
    tokens_out: Optional[int]


async def send(*, base_url: str, api_key: str, model: str, max_tokens: int, timeout: float,
               system: str, user_text: str) -> FrontierResult:
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user_text}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "connection": "close",     # one request == one connection, so audit and network counts match
    }
    try:
        # A fresh client per call, no pooling or keep-alive reuse.
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(base_url.rstrip("/") + "/v1/messages", json=body, headers=headers)
    except httpx.HTTPError as e:
        raise FrontierError(type(e).__name__) from None
    if r.status_code >= 400:
        raise FrontierError(f"http_{r.status_code}")
    try:
        data = r.json()
        text = "".join(p.get("text", "") for p in data.get("content", []) if p.get("type") == "text")
        usage = data.get("usage") or {}
        return FrontierResult(text, usage.get("input_tokens"), usage.get("output_tokens"))
    except (ValueError, AttributeError):
        raise FrontierError("bad_response") from None
