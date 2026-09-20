"""Tagged context fragments. Context is a list of these, never an early-concatenated string."""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable, Optional


class Taint(IntEnum):
    """Ordered so that max() is the join. PRIVATE dominates CLEAN."""
    CLEAN = 0
    PRIVATE = 1


@dataclass(frozen=True)
class Fragment:
    text: str
    taint: Taint
    origin: str          # source name from the registry, or "user_message" / "assistant"
    turn: int
    role: str = "user"   # user | assistant | context | tool
    tool_calls: Optional[str] = None      # JSON string of validated calls, on assistant fragments
    tool_call_id: Optional[str] = None    # on tool-result fragments


def context_taint(fragments: Iterable[Fragment]) -> Taint:
    """Context taint = max over fragment taints. Empty context is CLEAN."""
    return max((f.taint for f in fragments), default=Taint.CLEAN)
