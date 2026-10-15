"""Email and password accounts. The password hash never leaves this module's store.

Cookie value is account id, expiry, and an HMAC. The secret stays on the server.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

ACCOUNT_COOKIE = "tuck_account"
COOKIE_MAX_AGE = 14 * 24 * 60 * 60
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ponytail: failures live in process memory. Ceiling: one worker, and a restart
# clears the lockout. Upgrade path: store the counter next to the account row.
_FAILS: dict[str, tuple[int, float]] = {}
_FAIL_LIMIT = 8
_LOCK_SECONDS = 15 * 60


class AccountError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=10, max_length=200)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        value = value.strip().lower()
        if not _EMAIL.match(value):
            raise ValueError("Enter an email address.")
        return value

    @field_validator("password")
    @classmethod
    def _password(cls, value: str) -> str:
        if value.strip() != value or not value.strip():
            raise ValueError("Use at least 10 characters, without leading spaces.")
        return value


class LoginRequest(RegisterRequest):
    pass


class ProfileNameRequest(BaseModel):
    person_name: str = Field(min_length=1, max_length=60)

    @field_validator("person_name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Say who you are helping.")
        return value


def read_secret_file(key: str) -> str:
    """Read one key from the local secrets file. Never log the value."""
    path = Path.home() / ".config" / "ax" / "secrets" / "tuck-spotify.env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, raw = stripped.split("=", 1)
        if name.strip() != key:
            continue
        return raw.strip().strip('"').strip("'")
    return ""


def resolve_session_secret(explicit: str | None) -> str:
    if explicit is not None:
        return explicit.strip()
    env = os.environ.get("SESSION_SECRET", "").strip()
    if env:
        return env
    return read_secret_file("SESSION_SECRET")


def hash_password(password: str) -> dict[str, Any]:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
    )
    return {
        "salt": salt.hex(),
        "hash": digest.hex(),
        "n": _SCRYPT_N,
        "r": _SCRYPT_R,
        "p": _SCRYPT_P,
    }


_DUMMY_PASSWORD: dict[str, Any] | None = None


def dummy_password() -> dict[str, Any]:
    """One throwaway hash so a missing email still pays for a password check."""
    global _DUMMY_PASSWORD
    if _DUMMY_PASSWORD is None:
        _DUMMY_PASSWORD = hash_password("timing-password")
    return _DUMMY_PASSWORD


def verify_password(password: str, stored: dict[str, Any] | None) -> bool:
    if not isinstance(stored, dict):
        return False
    try:
        n = int(stored["n"])
        r = int(stored["r"])
        p = int(stored["p"])
        salt = bytes.fromhex(str(stored["salt"]))
        expected = str(stored["hash"])
    except (KeyError, TypeError, ValueError):
        return False
    if n < 2**12 or n > 2**15 or r < 1 or r > 16 or p < 1 or p > 4:
        return False
    try:
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    except ValueError:
        return False
    return hmac.compare_digest(digest.hex(), expected)


def issue_cookie(
    account_id: str,
    secret: str,
    version: int = 0,
    *,
    now: float | None = None,
) -> str:
    exp = int((now if now is not None else time.time()) + COOKIE_MAX_AGE)
    body = f"{account_id}.{exp}.{int(version)}"
    sig = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_cookie(
    value: str | None,
    secret: str,
    *,
    now: float | None = None,
) -> tuple[str, int] | None:
    if not value or not secret:
        return None
    parts = value.split(".")
    if len(parts) != 4:
        return None
    account_id, exp, version, sig = parts
    if not account_id or not exp or version == "" or not sig:
        return None
    body = f"{account_id}.{exp}.{version}"
    expected = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        expires = int(exp)
        current_version = int(version)
    except ValueError:
        return None
    if current_version < 0 or expires < int(now if now is not None else time.time()):
        return None
    return account_id, current_version


def fresh_profile() -> dict[str, Any]:
    return {"person_name": "", "probes": [], "habits": [], "exclusions": [], "feedback": []}


def new_account(email: str, password: str) -> dict[str, Any]:
    from .session import now_utc

    return {
        "id": "a_" + secrets.token_hex(8),
        "email": email,
        "password": hash_password(password),
        "created_at": now_utc(),
        "session_version": 0,
        "profile": fresh_profile(),
    }


def public_account(account: dict[str, Any]) -> dict[str, Any]:
    profile = account.get("profile") or fresh_profile()
    return {
        "email": account.get("email") or "",
        "person_name": profile.get("person_name") or "",
    }


def note_failure(email: str, *, now: float | None = None) -> None:
    current = now if now is not None else time.time()
    count, locked = _FAILS.get(email, (0, 0.0))
    if locked and locked > current:
        return
    count += 1
    lock = current + _LOCK_SECONDS if count >= _FAIL_LIMIT else 0.0
    _FAILS[email] = (count, lock)


def clear_failures(email: str) -> None:
    _FAILS.pop(email, None)


def locked_for(email: str, *, now: float | None = None) -> bool:
    current = now if now is not None else time.time()
    count, locked = _FAILS.get(email, (0, 0.0))
    if locked and locked <= current:
        _FAILS.pop(email, None)
        return False
    return bool(locked and locked > current and count >= _FAIL_LIMIT)


def write_person_name(account: dict[str, Any], person_name: str) -> None:
    profile = account.setdefault("profile", fresh_profile())
    profile["person_name"] = person_name


def write_probe(account: dict[str, Any], probe: dict[str, str]) -> None:
    profile = account.setdefault("profile", fresh_profile())
    probes = [item for item in profile.get("probes") or [] if item.get("id") != probe["id"]]
    probes.append(probe)
    profile["probes"] = probes


def write_commit(account: dict[str, Any], *, habits: list[dict[str, str]], exclusions: list[str]) -> None:
    profile = account.setdefault("profile", fresh_profile())
    profile["habits"] = habits
    profile["exclusions"] = exclusions


def write_feedback(account: dict[str, Any], record: dict[str, str]) -> None:
    profile = account.setdefault("profile", fresh_profile())
    rows = list(profile.get("feedback") or [])
    rows = [row for row in rows if row.get("session_id") != record["session_id"]]
    rows.append(record)
    # ponytail: keep the last 20 sittings on the profile. Upgrade path: a log file.
    profile["feedback"] = rows[-20:]
