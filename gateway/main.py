"""Phase 1 gateway: health + OpenAI-compatible proxy to the local model server.

Fails closed: if the local model is unreachable the caller gets a 503 from the private lane.
There is no other upstream in this phase, and none is ever used as a fallback.
"""
from __future__ import annotations

import json
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import Settings, get_settings

app = FastAPI(title="Private AI Agent Template gateway")
LANE = "private"


def _client(cfg: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=cfg.model_server.timeout_seconds)


def _unavailable() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"lane": LANE, "error": "local_model_unavailable"},
        headers={"X-Lane": LANE},
    )


@app.get("/health")
async def health() -> JSONResponse:
    cfg = get_settings()
    try:
        async with _client(cfg) as c:
            r = await c.get(cfg.model_server.health_url, timeout=5)
            r.raise_for_status()
        return JSONResponse({"status": "ok", "model_server": "up"})
    except httpx.HTTPError:
        return JSONResponse({"status": "degraded", "model_server": "down"}, status_code=503)


@app.get("/v1/models")
async def list_models() -> dict:
    cfg = get_settings()
    return {
        "object": "list",
        "data": [{"id": alias, "object": "model", "owned_by": "local"} for alias in cfg.models.aliases],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    cfg = get_settings()
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "invalid_json"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "invalid_body"})
    try:
        body["model"] = cfg.resolve_model(body.get("model") or request.headers.get("X-Model"))
    except KeyError:
        return JSONResponse(status_code=400, content={"error": "unknown_model"})

    url = cfg.model_server.base_url.rstrip("/") + "/chat/completions"

    if body.get("stream"):
        return await _stream(cfg, url, body)

    try:
        async with _client(cfg) as c:
            r = await c.post(url, json=body)
    except httpx.HTTPError:
        return _unavailable()
    if r.status_code >= 500:
        return _unavailable()
    data = r.json()
    if isinstance(data, dict):
        data["lane"] = LANE
    return JSONResponse(data, status_code=r.status_code, headers={"X-Lane": LANE})


async def _stream(cfg: Settings, url: str, body: dict):
    client = _client(cfg)
    try:
        req = client.build_request("POST", url, json=body)
        upstream = await client.send(req, stream=True)
    except httpx.HTTPError:
        await client.aclose()
        return _unavailable()
    if upstream.status_code >= 500:
        await upstream.aclose()
        await client.aclose()
        return _unavailable()

    async def gen() -> AsyncIterator[bytes]:
        try:
            yield b": lane=private\n\n"
            async for chunk in upstream.aiter_raw():
                yield chunk
        except httpx.HTTPError:
            yield b'data: {"lane":"private","error":"local_model_unavailable"}\n\n'
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        gen(), status_code=upstream.status_code, media_type="text/event-stream",
        headers={"X-Lane": LANE},
    )
