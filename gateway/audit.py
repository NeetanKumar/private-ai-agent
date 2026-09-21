"""Append-only audit log of egress events. Fixed schema: hashes and counts only.

The record type has no field that could hold a prompt body or user data, so none can be logged.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class AuditRecord:
    ts: str
    lane: str
    user: str
    prompt_sha256: str
    tokens_in: Optional[int]
    tokens_out: Optional[int]
    destination: str
    consent_mode: str            # auto | session | request
    status: str                  # ok | error


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class AuditLog:
    def __init__(self, path: str):
        self.path = Path(path)
        self._lock = threading.Lock()

    def write(self, rec: AuditRecord) -> None:
        line = json.dumps(asdict(rec), separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())

    def records_for(self, user: str, limit: int = 50) -> list:
        """A user's own records, newest first. Records hold hashes and counts only."""
        if not self.path.exists():
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("user") == user:
                    out.append(r)
        return out[::-1][:limit]

    def count(self) -> int:
        if not self.path.exists():
            return 0
        with open(self.path, encoding="utf-8") as f:
            return sum(1 for _ in f)
