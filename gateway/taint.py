"""Taint engine: source registry and pure propagation rules.

Rules, all deterministic. No model or classifier is ever consulted, and nothing in a fragment's
text can change its taint.
  * Unregistered sources are PRIVATE. CLEAN needs an explicit registry entry.
  * Retrieval: a chunk inherits its source document's taint.
  * Tool output: max(tool taint, taint of the arguments).
  * Model output: inherits the taint of the full context it was generated from.
  * History: fragments persist in the session (see session.py); only /new clears them.
"""
from __future__ import annotations

from typing import Dict, Iterable, Mapping

from .fragments import Fragment, Taint, context_taint

# Names the gateway itself assigns. A client can never claim them for context it supplies.
RESERVED_SOURCES = frozenset({"user_message", "assistant"})


def parse_taint(value: str) -> Taint:
    v = str(value).strip().upper()
    if v not in Taint.__members__:
        raise ValueError(f"taint must be CLEAN or PRIVATE, got {value!r}")
    return Taint[v]


class SourceRegistry:
    def __init__(self, sources: Mapping[str, str] | None = None):
        self._t: Dict[str, Taint] = {name: parse_taint(t) for name, t in (sources or {}).items()}

    def taint_of(self, source: str) -> Taint:
        return self._t.get(source, Taint.PRIVATE)


def retrieval_taint(doc_taint: Taint) -> Taint:
    return doc_taint


def tool_output_taint(tool_taint: Taint, arg_taints: Iterable[Taint]) -> Taint:
    return max([tool_taint, *arg_taints])


def model_output_taint(context: Iterable[Fragment]) -> Taint:
    return context_taint(context)
