"""Real socket stub servers, so tests count actual TCP connections instead of mocked calls."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import List


class _CountingServer(HTTPServer):
    allow_reuse_address = True

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.connections = 0
        self.bodies: List[str] = []      # raw request bodies received
        self.paths: List[str] = []
        self._lock = threading.Lock()

    def get_request(self):
        conn = super().get_request()
        with self._lock:
            self.connections += 1
        return conn


class _Stub:
    handler = None

    def __init__(self):
        self.server = _CountingServer(("127.0.0.1", 0), self.handler)
        self.server.owner = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._stopped = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def connections(self) -> int:
        return self.server.connections

    @property
    def bodies(self) -> List[str]:
        return list(self.server.bodies)

    def stop(self) -> None:
        if not self._stopped:
            self._stopped = True
            self.server.shutdown()
            self.server.server_close()


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _read(self) -> str:
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode()
        with self.server._lock:
            self.server.bodies.append(raw)
            self.server.paths.append(self.path)
        return raw

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


class _LocalHandler(_Quiet):
    """Fake local model. By default it answers politely. Tests can queue scripted replies with
    `stub.script.append({...})` or make it permanently misbehave with `stub.always = {...}`, which
    is how a model that has been talked into obeying an injection is simulated.

    A reply dict may hold: content, tool_calls, stream_pieces, stream_tool_calls."""

    def do_GET(self):
        self._json({"models": []})

    def _next(self):
        o = self.server.owner
        if o.script:
            return o.script.pop(0)
        return o.always or {}

    def do_POST(self):
        body = json.loads(self._read())
        r = self._next()
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            pieces = r.get("stream_pieces") or (["local ", "streamed ", "answer"] if "content" not in r else [r["content"]])
            for piece in pieces:
                chunk = {"choices": [{"index": 0, "delta": {"content": piece}}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            if r.get("stream_tool_calls"):
                chunk = {"choices": [{"index": 0, "delta": {"tool_calls": r["stream_tool_calls"]}}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        msg = {"role": "assistant", "content": r.get("content", "local answer")}
        if r.get("tool_calls"):
            msg["tool_calls"] = r["tool_calls"]
        self._json({"model": body.get("model"), "choices": [
            {"index": 0, "message": msg,
             "finish_reason": "tool_calls" if r.get("tool_calls") else "stop"}]})


class _FrontierHandler(_Quiet):
    def do_POST(self):
        self._read()
        self._json({"content": [{"type": "text", "text": "frontier answer"}],
                    "usage": {"input_tokens": 11, "output_tokens": 7}})


class LocalStub(_Stub):
    handler = _LocalHandler

    def __init__(self):
        super().__init__()
        self.script = []
        self.always = None

    def last_request(self):
        return json.loads(self.bodies[-1])


def call(name, args, cid=None):
    """A tool call as an OpenAI-compatible server would return it."""
    d = {"type": "function", "function": {"name": name, "arguments": json.dumps(args) if not isinstance(args, str) else args}}
    if cid:
        d["id"] = cid
    return d


class FrontierStub(_Stub):
    handler = _FrontierHandler
