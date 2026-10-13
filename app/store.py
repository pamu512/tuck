"""Local JSON session store.

All sessions live in one JSON file. Writes are atomic (temp file plus
os.replace). session_scope holds one of a fixed set of stripe locks across
load → mutate → put so concurrent requests for the same session cannot drop
each other's writes. The file lock is taken only around the read or the
write, and never while waiting on a stripe lock.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# ponytail: fixed stripes, not a lock per session id (that map grows with
# every 404 probe on a public demo). Same id always hashes to one stripe.
# Ceiling: two live sessions can share a stripe and queue on each other.
# Upgrade path: one lock per live session, dropped when that session is deleted.
_STRIPE_COUNT = 64


class StoreError(RuntimeError):
    """The session store could not be read or is corrupt."""


class JsonStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._file_lock = threading.Lock()
        self._stripes = tuple(threading.Lock() for _ in range(_STRIPE_COUNT))

    def _stripe_for(self, session_id: str) -> threading.Lock:
        return self._stripes[hash(session_id) % _STRIPE_COUNT]

    @contextmanager
    def session_scope(self, session_id: str) -> Iterator[None]:
        """Serialize load → mutate → put for one session id."""
        lock = self._stripe_for(session_id)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            raise StoreError(f"session store {self.path} is corrupt: {exc}") from exc

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def read_all(self) -> dict[str, Any]:
        with self._file_lock:
            return self._read_unlocked()

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self._file_lock:
            return self._read_unlocked().get(session_id)

    def put(self, session: dict[str, Any]) -> None:
        with self._file_lock:
            data = self._read_unlocked()
            data[session["id"]] = session
            self._write_unlocked(data)

    def delete(self, session_id: str) -> bool:
        with self._file_lock:
            data = self._read_unlocked()
            if session_id not in data:
                return False
            del data[session_id]
            self._write_unlocked(data)
            return True

    def count(self) -> int:
        with self._file_lock:
            return len(self._read_unlocked())

    def insert_if_room(
        self,
        session: dict[str, Any],
        max_sessions: int,
        expired_ids: set[str],
    ) -> bool:
        """Drop expired ids, then insert unless the cap is already full.

        The cap check and the write share the file lock, so two invites
        cannot both observe a free slot and both persist.
        """
        with self._file_lock:
            data = self._read_unlocked()
            purged = False
            for session_id in expired_ids:
                if data.pop(session_id, None) is not None:
                    purged = True
            if session["id"] not in data and len(data) >= max_sessions:
                if purged:
                    self._write_unlocked(data)
                return False
            data[session["id"]] = session
            self._write_unlocked(data)
            return True
