"""Session domain and safety invariants for Guided Memory Lane.

State machine:
    invite -> uploads -> exclusions acknowledged -> preview -> candidates

Invariants enforced here:
1. No Qloo call happens before trauma exclusions are acknowledged. Both
   Qloo-touching steps call require_exclusions_acknowledged() first. An
   empty exclusion list counts only when explicitly POSTed (a deliberate
   caregiver decision, not a skipped step), and re-posting exclusions after
   a preview invalidates that preview so it must be re-signed.
2. Photos are opaque path strings. Nothing in this module opens, reads, or
   inspects them. Qloo signals come exclusively from caregiver-typed
   interests; anecdote text is stored for the family record and is never
   sent to Qloo.
3. The claim boundary (locked 2026-10-13) travels with every preview and
   candidates response.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets as _secrets
from datetime import datetime, timezone
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field, field_validator

from .qloo import INSIGHT_TYPES

MAX_UPLOADS_PER_SESSION = 20
MAX_ANECDOTE_CHARS = 600
MAX_INTERESTS_PER_UPLOAD = 8
MAX_TOTAL_INTERESTS = 8
MAX_INTEREST_CHARS = 80
MAX_PHOTO_PATH_CHARS = 4096
MAX_EXCLUSIONS = 12
MAX_EXCLUSION_LABEL_CHARS = 80
MAX_EXCLUSION_REASON_CHARS = 200

CLAIMS_WILL = [
    "Comfortable shared listening and looking sessions",
    "Invite-first access; the family controls every step",
    "Caregiver in the loop with a mandatory preview before any request",
    (
        "Background only: a Cochrane 2025 review reports a small effect of "
        "preferred-music listening on depression symptoms (SMD of about -0.23); "
        "cited as background for preferred music, never as an outcome of this app"
    ),
    "Qloo supplies cultural-affinity candidates (music, film, place) after exclusions",
]

CLAIMS_WONT = [
    "Memory restoration",
    "Agitation reduction",
    "Disease treatment",
    "Claims that music plus photos outperform either alone",
    "Therapy-equivalent outcomes",
    "Biography or life story inferred from photos",
]

COCHRANE_BACKGROUND = (
    "Background only: a Cochrane 2025 review reports a small effect of "
    "preferred-music listening on depression symptoms (SMD of about -0.23). "
    "It is cited as background for comfortable preferred-music listening, "
    "never as an outcome of this app."
)

PREVIEW_DISCLAIMER = (
    "Preview before anything is requested: the signals below are the only data "
    "Qloo receives. Photos are never opened or analyzed, anecdotes stay in the "
    "family record, and no biography is inferred from photos."
)

CANDIDATES_BOUNDARY = (
    "Cultural-affinity suggestions for comfortable shared listening and looking. "
    "Not therapy, not treatment, not a medical device, and no biography is "
    "inferred from photos."
)


class SessionError(RuntimeError):
    """A state-machine violation; carries the HTTP status to return."""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def now_utc() -> str:
    """Current UTC timestamp used for all session bookkeeping."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------


class InviteRequest(BaseModel):
    caregiver_label: str = Field(min_length=1, max_length=60)

    @field_validator("caregiver_label")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("caregiver_label must not be blank")
        return value


class UploadRequest(BaseModel):
    photo_path: str = Field(min_length=1, max_length=MAX_PHOTO_PATH_CHARS)
    anecdote: str = Field(min_length=1, max_length=MAX_ANECDOTE_CHARS)
    interests: list[str] = Field(default_factory=list, max_length=MAX_INTERESTS_PER_UPLOAD)

    @field_validator("photo_path", "anecdote")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("interests")
    @classmethod
    def _clean_interests(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        if any(len(item) > MAX_INTEREST_CHARS for item in cleaned):
            raise ValueError(f"each interest must be at most {MAX_INTEREST_CHARS} characters")
        return cleaned


class ExclusionInput(BaseModel):
    label: str = Field(min_length=1, max_length=MAX_EXCLUSION_LABEL_CHARS)
    kind: Literal["topic", "entity"] = "topic"
    reason: str | None = Field(default=None, max_length=MAX_EXCLUSION_REASON_CHARS)


class ExclusionsRequest(BaseModel):
    exclusions: list[ExclusionInput] = Field(default_factory=list, max_length=MAX_EXCLUSIONS)


# --------------------------------------------------------------------------
# Session lifecycle
# --------------------------------------------------------------------------


def new_invite(caregiver_label: str) -> tuple[dict[str, Any], str]:
    """Create a session and its one-time invite token."""
    token = _secrets.token_urlsafe(18)
    session: dict[str, Any] = {
        "id": "s_" + _secrets.token_hex(6),
        "caregiver_label": caregiver_label,
        "invite_token_hash": _hash_token(token),
        "created_at": now_utc(),
        "uploads": [],
        "exclusions": None,
        "exclusions_acknowledged_at": None,
        "resolved_signals": None,
        "unresolved_interests": None,
        "previewed_at": None,
    }
    return session, token


def verify_invite(session: dict[str, Any], token: str | None) -> bool:
    """Constant-time check of the invite token against its stored hash."""
    if not token:
        return False
    return hmac.compare_digest(session["invite_token_hash"], _hash_token(token))


def add_upload(session: dict[str, Any], request: UploadRequest) -> dict[str, Any]:
    if len(session["uploads"]) >= MAX_UPLOADS_PER_SESSION:
        raise SessionError(f"upload limit reached ({MAX_UPLOADS_PER_SESSION})")
    upload = {
        "id": "u_" + _secrets.token_hex(4),
        # Opaque string, stored as metadata only. The photo is never opened.
        "photo_path": request.photo_path,
        "anecdote": request.anecdote,
        "interests": request.interests,
        "uploaded_at": now_utc(),
    }
    session["uploads"].append(upload)
    return upload


def acknowledge_exclusions(
    session: dict[str, Any], request: ExclusionsRequest
) -> list[dict[str, Any]]:
    """Record exclusions (possibly none) as a deliberate caregiver decision.

    Always invalidates any earlier preview: the caregiver must re-sign the
    exact signals that will be sent after exclusions change.
    """
    exclusions = [exclusion.model_dump() for exclusion in request.exclusions]
    session["exclusions"] = exclusions
    session["exclusions_acknowledged_at"] = now_utc()
    session["resolved_signals"] = None
    session["unresolved_interests"] = None
    session["previewed_at"] = None
    return exclusions


def require_uploads(session: dict[str, Any]) -> None:
    if not session["uploads"]:
        raise SessionError("Add at least one upload (photo path + anecdote) first.")


def require_exclusions_acknowledged(session: dict[str, Any]) -> None:
    if session["exclusions_acknowledged_at"] is None:
        raise SessionError(
            "Trauma exclusions must be recorded before any Qloo call. "
            "POST /sessions/{id}/exclusions first; an empty list is a valid "
            "deliberate acknowledgment."
        )


def require_previewed(session: dict[str, Any]) -> None:
    require_exclusions_acknowledged(session)
    if not session["previewed_at"]:
        raise SessionError(
            "Caregiver preview is required before candidates. "
            "POST /sessions/{id}/preview first."
        )


# --------------------------------------------------------------------------
# Interests and signals
# --------------------------------------------------------------------------


def collect_interests(session: dict[str, Any]) -> list[str]:
    """Explicit caregiver-typed interests across uploads, deduped, capped."""
    seen: set[str] = set()
    ordered: list[str] = []
    for upload in session["uploads"]:
        for interest in upload["interests"]:
            key = interest.casefold()
            if key not in seen:
                seen.add(key)
                ordered.append(interest)
    return ordered[:MAX_TOTAL_INTERESTS]


def _name_matches(interest: str, name: str) -> bool:
    """The resolved Qloo object must actually be what the caregiver typed.

    Qloo tag search matches loosely (a person-name interest can return an
    unrelated nearby-attraction tag), so a signal is accepted only when the
    resolved name equals or contains the typed interest (or vice versa).
    Everything else is reported as unresolved rather than guessed.
    """
    a = interest.casefold().strip()
    b = (name or "").casefold().strip()
    return bool(a and b) and (a == b or a in b or b in a)


def resolve_signals(
    interests: Iterable[str], qloo: Any
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve caregiver-typed interests to Qloo tag or entity IDs.

    Tags first (genres and cultural categories), then entities (named
    artists, films, places). A resolution is accepted only on a name match;
    interests that do not closely resolve are reported as unresolved and
    nothing is ever guessed, expanded, or paraphrased from anecdotes or
    photos.
    """
    signals: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for interest in interests:
        tags = qloo.search_tags(interest)
        close_tag = next((tag for tag in tags if _name_matches(interest, tag["name"])), None)
        if close_tag:
            signals.append(
                {
                    "interest": interest,
                    "kind": "tag",
                    "id": close_tag["id"],
                    "name": close_tag["name"],
                }
            )
            continue
        entities = qloo.search_entities(interest)
        close_entity = next(
            (entity for entity in entities if _name_matches(interest, entity["name"])), None
        )
        if close_entity:
            signals.append(
                {
                    "interest": interest,
                    "kind": "entity",
                    "id": close_entity["entity_id"],
                    "name": close_entity["name"],
                }
            )
            continue
        unresolved.append(interest)
    return signals, unresolved


# --------------------------------------------------------------------------
# Exclusion matching
# --------------------------------------------------------------------------

_STOPWORDS = {"the", "a", "an", "and", "of", "in", "on", "for"}


def _stem(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and len(word) > 3:
        return word[:-1]
    return word


def _tokens(text: str) -> set[str]:
    return {
        _stem(token)
        for token in text.casefold().replace("-", " ").split()
        if token not in _STOPWORDS
    }


def exclusion_matches(
    exclusion: dict[str, Any],
    *,
    name: str | None = None,
    entity_id: str | None = None,
) -> bool:
    """True when an exclusion covers the named thing.

    Entity exclusions match on exact id or name (case-insensitive), with a
    substring fallback. Topic exclusions match when the label appears as a
    substring or all of its stem tokens appear in the display name, so
    "war documentaries" covers "World War II Documentary Collection".
    """
    label = exclusion["label"].strip()
    folded = label.casefold()
    names = [candidate for candidate in (name,) if candidate]
    if exclusion.get("kind") == "entity":
        if entity_id and entity_id.casefold() == folded:
            return True
        return any(candidate.casefold() == folded or folded in candidate.casefold() for candidate in names)
    label_tokens = _tokens(label)
    for candidate in names:
        candidate_folded = candidate.casefold()
        if folded in candidate_folded:
            return True
        if label_tokens and label_tokens <= _tokens(candidate):
            return True
    return False


def split_signals(
    signals: list[dict[str, Any]], exclusions: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split resolved signals into (sendable, withheld-by-exclusion)."""
    kept: list[dict[str, Any]] = []
    withheld: list[dict[str, Any]] = []
    for signal in signals:
        hit = next(
            (
                exclusion
                for exclusion in exclusions
                if exclusion_matches(
                    exclusion, name=signal.get("name"), entity_id=signal.get("id")
                )
            ),
            None,
        )
        if hit:
            withheld.append({**signal, "matched_exclusion": hit["label"]})
        else:
            kept.append(signal)
    return kept, withheld


def filter_candidates(
    candidates: list[dict[str, Any]], exclusions: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split Qloo candidates into (kept, removed) using the exclusions."""
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for candidate in candidates:
        hit = next(
            (
                exclusion
                for exclusion in exclusions
                if exclusion_matches(
                    exclusion, name=candidate.get("name"), entity_id=candidate.get("entity_id")
                )
            ),
            None,
        )
        if hit:
            removed.append(
                {
                    "name": candidate["name"],
                    "entity_id": candidate["entity_id"],
                    "matched_exclusion": hit["label"],
                }
            )
        else:
            kept.append(candidate)
    return kept, removed


def candidate_reason(name: str, signals: list[dict[str, Any]]) -> str:
    """Why a candidate is shown, anchored only in stated interests."""
    folded = name.casefold()
    for signal in signals:
        if signal["name"].casefold() == folded:
            return f"Directly matches the family's stated interest: {signal['name']}"
    names = ", ".join(dict.fromkeys(signal["name"] for signal in signals))
    return f"Cultural affinity with stated interests: {names or 'family interests'}"


# --------------------------------------------------------------------------
# Response payloads
# --------------------------------------------------------------------------


def preview_payload(
    session: dict[str, Any],
    *,
    kept_signals: list[dict[str, Any]],
    withheld: list[dict[str, Any]],
    unresolved: list[str],
) -> dict[str, Any]:
    return {
        "session_id": session["id"],
        "caregiver_label": session["caregiver_label"],
        "photo_count": len(session["uploads"]),
        "anecdotes": [upload["anecdote"] for upload in session["uploads"]],
        "explicit_interests": collect_interests(session),
        "signals": kept_signals,
        "withheld_by_exclusions": withheld,
        "unresolved_interests": unresolved,
        "what_qloo_receives": {
            "signal_ids": [signal["id"] for signal in kept_signals],
            "insight_types": list(INSIGHT_TYPES),
            "note": (
                "Only these resolved signal IDs are sent to Qloo. Photos, photo "
                "paths, and anecdote text are never sent."
            ),
        },
        "exclusions": session["exclusions"],
        "disclaimer": PREVIEW_DISCLAIMER,
        "claims": {
            "will": CLAIMS_WILL,
            "wont": CLAIMS_WONT,
            "background": COCHRANE_BACKGROUND,
        },
    }


def candidates_payload(
    session: dict[str, Any],
    candidates: list[dict[str, Any]],
    removed: list[dict[str, Any]],
    types_failed: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "session_id": session["id"],
        "generated_at": now_utc(),
        "types_queried": list(INSIGHT_TYPES),
        "candidates": candidates,
        "removed_by_exclusions": removed,
        "types_failed": types_failed or [],
        "boundary": CANDIDATES_BOUNDARY,
    }
