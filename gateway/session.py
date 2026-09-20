"""Per-user sessions. One isolated session per user id; nothing is shared across users.

Taint is sticky: it is a high-water mark that only `clear()` (the /new command) resets. Even if a
future change truncates history for context length, taint cannot drop.
"""
from __future__ import annotations

import asyncio
from typing import Dict, List, Optional, Tuple

from .fragments import Fragment, Taint


class Session:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self._lock: Optional[asyncio.Lock] = None
        self._fragments: List[Fragment] = []
        self._taint = Taint.CLEAN
        self.consent = False
        self.turn = 0
        # tool_call_id -> (tool name, taint of the call's arguments). Only calls the gateway itself
        # issued (after permission checks) can ever receive a result.
        self.pending_calls: Dict[str, Tuple[str, Taint]] = {}

    @property
    def lock(self) -> asyncio.Lock:
        """Created on first use, inside the running event loop, so a Session can be built anywhere."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @property
    def taint(self) -> Taint:
        return self._taint

    @property
    def fragments(self) -> List[Fragment]:
        return list(self._fragments)

    def add(self, fragment: Fragment) -> None:
        self._fragments.append(fragment)
        self._taint = max(self._taint, fragment.taint)

    def clear(self) -> None:
        """The only way taint is ever lowered."""
        self._fragments.clear()
        self._taint = Taint.CLEAN
        self.consent = False
        self.turn = 0
        self.pending_calls.clear()


class SessionStore:
    def __init__(self) -> None:
        self._s: Dict[str, Session] = {}

    def get(self, user_id: str) -> Session:
        if user_id not in self._s:
            self._s[user_id] = Session(user_id)
        return self._s[user_id]
