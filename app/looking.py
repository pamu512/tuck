"""Opaque family-photo bytes. Stored and served. Never decoded.

ponytail: one file per id, raw bytes only, no image parser.
Ceiling: 8 MB buffered in memory per request.
Upgrade path: stream the upload straight to disk.
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Any, BinaryIO

MAX_PHOTO_BYTES = 8 * 1024 * 1024
ALLOWED_MEDIA = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "image/heic",
        "image/heif",
    }
)
PHOTO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_PLACEHOLDERS = frozenset({"not-provided", "not provided"})
# Suffix is a label when the browser sends no image type. The bytes are not decoded.
_EXT_MEDIA = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".heif": "image/heif",
}


class PhotoStoreError(Exception):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def looking_ready(has_photo: bool, anecdote: str) -> bool:
    """True only when a file is chosen and the moment is non-blank."""
    return bool(has_photo) and bool(" ".join(str(anecdote or "").split()))


def photo_provided(path: str | None) -> bool:
    """A real stored path. The not-provided placeholder does not count."""
    text = str(path or "").strip()
    return bool(text) and text.casefold() not in _PLACEHOLDERS


def normalize_media_type(value: str | None, filename: str | None = None) -> str:
    raw = (value or "").split(";", 1)[0].strip().lower()
    if raw == "image/jpg":
        raw = "image/jpeg"
    if raw in ALLOWED_MEDIA:
        return raw
    if raw in ("", "application/octet-stream"):
        mapped = _EXT_MEDIA.get(Path(str(filename or "")).suffix.casefold())
        if mapped:
            return mapped
    raise PhotoStoreError("Choose a photo file.", 422)


def read_photo_bytes(source: BinaryIO, *, limit: int = MAX_PHOTO_BYTES) -> bytes:
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            block = source.read(65536)
            if not block:
                break
            total += len(block)
            if total > limit:
                raise PhotoStoreError("That photo is too large. Keep it under 8 MB.", 413)
            chunks.append(block)
    except OSError as exc:
        raise PhotoStoreError("I could not store that photo. Try again.", 503) from exc
    if total == 0:
        raise PhotoStoreError("Choose a photo to look at.", 422)
    return b"".join(chunks)


def new_photo_id() -> str:
    return secrets.token_urlsafe(18)


def allocate_photo_id(root: Path) -> str:
    """A new id that does not already name a file. Does not open photo bytes."""
    base = root.resolve()
    for _ in range(5):
        photo_id = new_photo_id()
        if PHOTO_ID_RE.fullmatch(photo_id) and not (base / photo_id).exists():
            return photo_id
    raise PhotoStoreError("I could not store that photo. Try again.", 503)


def store_opaque(root: Path, photo_id: str, blob: bytes) -> Path:
    if not PHOTO_ID_RE.fullmatch(photo_id):
        raise PhotoStoreError("Choose a photo file.", 400)
    base = root.resolve()
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PhotoStoreError("I could not store that photo. Try again.", 503) from exc
    dest = (base / photo_id).resolve()
    if dest.parent != base:
        raise PhotoStoreError("Choose a photo file.", 400)
    tmp = base / f".{photo_id}.tmp"
    try:
        tmp.write_bytes(blob)
        os.replace(tmp, dest)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise PhotoStoreError("I could not store that photo. Try again.", 503) from exc
    return dest


def discard_opaque(root: Path, href: str) -> None:
    if not href.startswith("/data/"):
        return
    photo_id = href[len("/data/") :].split("/", 1)[0]
    if not PHOTO_ID_RE.fullmatch(photo_id):
        return
    base = root.resolve()
    dest = (base / photo_id).resolve()
    if dest.parent != base:
        return
    try:
        dest.unlink(missing_ok=True)
    except OSError:
        return


def session_photo_media(session: dict[str, Any], href: str) -> str | None:
    """Image type if this session still points at the opaque href."""
    if not isinstance(session, dict) or not isinstance(href, str) or not href.startswith("/data/"):
        return None
    photo_id = href[len("/data/") :].split("/", 1)[0]
    if not PHOTO_ID_RE.fullmatch(photo_id) or href != f"/data/{photo_id}":
        return None
    records: list[Any] = []
    interview = session.get("interview")
    if isinstance(interview, dict):
        records.append(interview)
    for item in session.get("uploads") or []:
        if isinstance(item, dict):
            records.append(item)
    for record in records:
        if record.get("photo_path") == href and record.get("photo_type") in ALLOWED_MEDIA:
            return str(record["photo_type"])
    return None


def looking_entries(uploads: list[Any]) -> list[dict[str, str]]:
    """Preview rows for photos that are actually on the opaque path.

    Client-supplied labels and the not-provided placeholder are left out.
    """
    items: list[dict[str, str]] = []
    for upload in uploads or []:
        if not isinstance(upload, dict):
            continue
        href = str(upload.get("photo_path") or "").strip()
        if not href.startswith("/data/"):
            continue
        photo_id = href[len("/data/") :]
        if not PHOTO_ID_RE.fullmatch(photo_id):
            continue
        items.append(
            {
                "photo_href": f"/data/{photo_id}",
                "anecdote": str(upload.get("anecdote") or ""),
            }
        )
    return items
