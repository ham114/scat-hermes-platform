"""Identity comes from trusted server code, never from model arguments."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    user_id: str
    thread_id: str

    def __post_init__(self) -> None:
        for value in (self.user_id, self.thread_id):
            if not isinstance(value, str) or not _ID.fullmatch(value):
                raise ValueError("Invalid server execution identity")

    @property
    def scope_key(self) -> str:
        canonical = json.dumps([self.user_id, self.thread_id], separators=(",", ":"))
        return "scat:" + hashlib.sha256(canonical.encode()).hexdigest()
