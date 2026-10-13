"""Local JSON session store.

All sessions live in one JSON file. Writes are atomic (temp file plus
os.replace) and guarded by a lock, which is enough for a single-process
local demo. This is explicitly not a production data layer; see README for
the boundary.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


class StoreError(RuntimeError):
    """The session store could not be read or is corrupt."""


class JsonStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

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

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._read_unlocked().get(session_id)

    def put(self, session: dict[str, Any]) -> None:
        with self._lock:
            data = self._read_unlocked()
            data[session["id"]] = session
            self._write_unlocked(data)

    def count(self) -> int:
        with self._lock:
            return len(self._read_unlocked())
