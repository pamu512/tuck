"""Session domain and safety invariants for Guided Memory Lane.

State machine:
    invite -> uploads -> exclusions acknowledged -> preview -> candidates -> sealed export

Invariants enforced here:
1. No recommendation request happens before trauma exclusions are
   acknowledged. Preview and candidates call require_exclusions_acknowledged()
   first. Resolving an exclusion label to a Qloo id happens during that
   acknowledgment, and only the label is sent. An empty exclusion list counts
   only when explicitly POSTed (a deliberate caregiver decision, not a skipped
   step), and re-posting exclusions after a preview invalidates that preview
   so it must be re-signed.
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
import re
import secrets as _secrets
from datetime import datetime, timezone
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field, field_validator

from .qloo import INSIGHT_TYPES

MAX_UPLOADS_PER_SESSION = 20
MAX_ANECDOTE_CHARS = 600
MAX_INTERESTS_PER_UPLOAD = 8
MAX_TOTAL_INTERESTS = 8
MIN_INTEREST_CHARS = 3
MAX_INTEREST_CHARS = 80
MAX_PHOTO_PATH_CHARS = 4096
MAX_EXCLUSIONS = 12
MAX_EXCLUSION_LABEL_CHARS = 80
MAX_EXCLUSION_REASON_CHARS = 200
MAX_RESOLVED_IDS = 8
DEFAULT_SESSION_TTL_SECONDS = 24 * 60 * 60
DEFAULT_MAX_SESSIONS = 200

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


def session_expired(
    session: dict[str, Any],
    ttl_seconds: float,
    *,
    now: datetime | None = None,
) -> bool:
    """True when created_at is older than ttl_seconds."""
    created = session.get("created_at")
    if not isinstance(created, str) or ttl_seconds < 0:
        return False
    try:
        stamp = datetime.fromisoformat(created)
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return (current - stamp).total_seconds() > ttl_seconds


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
        cleaned: list[str] = []
        for item in value:
            if not item or not str(item).strip():
                continue
            text = str(item).strip()
            if len(text) < MIN_INTEREST_CHARS:
                raise ValueError(
                    f"each interest must be at least {MIN_INTEREST_CHARS} characters"
                )
            if len(text) > MAX_INTEREST_CHARS:
                raise ValueError(
                    f"each interest must be at most {MAX_INTEREST_CHARS} characters"
                )
            cleaned.append(text)
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
        "sealed": False,
        "export": None,
        "spotify_oauth": None,
        "playlist": None,
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
    exclusions = []
    for exclusion in request.exclusions:
        item = exclusion.model_dump()
        item["resolved_ids"] = []
        item["resolved"] = False
        exclusions.append(item)
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


def require_open(session: dict[str, Any]) -> None:
    """Block edits after the list is sealed and family text has been cleared."""
    if session.get("sealed"):
        raise SessionError(
            "This session is sealed for playlist export. The anecdote and photo "
            "path were cleared. Start a new invite to change the memory."
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

# WWII must be tried before WWI: "wwii" contains the letters "wwi", and the
# engine is left-first, so the longer pattern has to win the alternation.
_WW2_RE = re.compile(
    r"\b(?:ww\s*ii|ww\s*2|world\s+war\s+(?:ii|2|two))\b",
    re.IGNORECASE,
)
_WW1_RE = re.compile(
    r"\b(?:ww\s*i|ww\s*1|world\s+war\s+(?:i|1|one))\b",
    re.IGNORECASE,
)

# Plurals of these -ie nouns are formed with -s (movie/movies). The y-plural
# rule (story/stories) would turn "movies" into "movy".
# ponytail: closed suffix list. An -ie noun that is not here still canonical-
# stems with the y-rule. _token_variants keeps both readings, so the match
# still holds. Upgrade path: a lexicon.
_IE_NOUN_SUFFIXES = (
    "movie",
    "cookie",
    "zombie",
    "rookie",
    "talkie",
    "brownie",
    "selfie",
    "hoodie",
    "veggie",
    "smoothie",
    "calorie",
    "genie",
    "auntie",
    "collie",
    "goalie",
    "birdie",
    "doggie",
    "prairie",
    "nightie",
    "freebie",
    "groupie",
    "newbie",
    "oldie",
    "softie",
    "sweetie",
    "hippie",
    "yuppie",
    "preppie",
    "foodie",
    "techie",
)


def _normalize_phrases(text: str) -> str:
    """Map WWII / World War Two (and WWI) onto one phrase. Hyphens fold first."""
    text = text.replace("-", " ").replace("–", " ").replace("—", " ")
    text = _WW2_RE.sub(" world war 2 ", text)
    text = _WW1_RE.sub(" world war 1 ", text)
    return text


def _is_ie_plural(word: str) -> bool:
    ie_form = word[:-1]
    return any(ie_form.endswith(suffix) for suffix in _IE_NOUN_SUFFIXES)


def _stem(word: str) -> str:
    """Canonical stem. movies→movie; documentaries→documentary; stories→story."""
    if word.endswith("ies") and len(word) > 4:
        if _is_ie_plural(word):
            return word[:-1]
        return word[:-3] + "y"
    # Stem must stay at least 4 letters. "wars"→"war" would make "war movies"
    # cover "Star Wars".
    if word.endswith("s") and not word.endswith("ss") and len(word) > 4:
        return word[:-1]
    return word


def _token_variants(word: str) -> set[str]:
    """Every plural reading of one token, so movie/movies share a stem."""
    variants = {word, _stem(word)}
    if word.endswith("ies") and len(word) > 4 and not _is_ie_plural(word):
        variants.add(word[:-1])
        variants.add(word[:-3] + "y")
    elif word.endswith("s") and not word.endswith("ss") and len(word) > 4:
        variants.add(word[:-1])
    if word.endswith("ie") and len(word) > 3:
        variants.add(word + "s")
    if len(word) > 3 and word.endswith("y") and word[-2] not in "aeiou":
        # y→ies is story/stories. movies is movie+s, so "movy" must not grow it.
        ies_form = word[:-1] + "ies"
        if not _is_ie_plural(ies_form):
            variants.add(ies_form)
    return {variant for variant in variants if variant}


def _content_tokens(text: str) -> list[set[str]]:
    normalized = _normalize_phrases(text).casefold()
    groups: list[set[str]] = []
    for raw in normalized.split():
        token = raw.strip(".,;:!?\"'()[]{}")
        if not token or token in _STOPWORDS:
            continue
        groups.append(_token_variants(token))
    return groups


def _topic_covers(label: str, name: str) -> bool:
    """True when every label token has a variant in common with the name."""
    groups = _content_tokens(label)
    if not groups:
        return False
    covered: set[str] = set()
    for group in _content_tokens(name):
        covered |= group
    return all(group & covered for group in groups)


def _entity_name_hit(label: str, name: str) -> bool:
    folded = label.casefold().strip()
    candidate = name.casefold().strip()
    if not folded or not candidate:
        return False
    if folded == candidate or folded in candidate:
        return True
    label_norm = " ".join(_normalize_phrases(label).casefold().split())
    name_norm = " ".join(_normalize_phrases(name).casefold().split())
    return bool(label_norm and name_norm) and (
        label_norm == name_norm or label_norm in name_norm
    )


def _resolved_id_hit(exclusion: dict[str, Any], entity_id: str | None) -> bool:
    if not entity_id:
        return False
    folded = entity_id.casefold()
    for item in exclusion.get("resolved_ids") or []:
        resolved = item.get("id") if isinstance(item, dict) else item
        if resolved and str(resolved).casefold() == folded:
            return True
    return False


def exclusion_matches(
    exclusion: dict[str, Any],
    *,
    name: str | None = None,
    entity_id: str | None = None,
) -> bool:
    """True when an exclusion covers this id or display name.

    Resolved Qloo ids match first. Entity labels then match on exact id or
    name, including WWII / World War Two after phrase normalization. Topic
    labels match as a substring or when every stem variant of the label
    appears in the name, so "war movies" covers "World War II Movie
    Collection" and "war documentaries" still covers the documentary title.
    """
    if _resolved_id_hit(exclusion, entity_id):
        return True
    label = (exclusion.get("label") or "").strip()
    if not label:
        return False
    names = [candidate for candidate in (name,) if candidate]
    if exclusion.get("kind") == "entity":
        if entity_id and entity_id.casefold() == label.casefold():
            return True
        return any(_entity_name_hit(label, candidate) for candidate in names)
    folded = label.casefold()
    for candidate in names:
        if folded in candidate.casefold():
            return True
        if _topic_covers(label, candidate):
            return True
    return False


def resolve_exclusion_ids(label: str, kind: str, qloo: Any) -> list[dict[str, str]]:
    """Map one exclusion label to Qloo tag or entity ids.

    Reuses search_tags and search_entities. A hit is kept only when
    exclusion_matches accepts its name, so a loose search result is not
    stored as a guess. An empty list means the label stayed unresolved and
    filtering is name-only.
    """
    raw = label.strip()
    queries = [raw] if raw else []
    normalized = " ".join(_normalize_phrases(raw).split())
    if normalized and normalized.casefold() not in {query.casefold() for query in queries}:
        queries.append(normalized)

    if kind == "entity":
        lookups = (
            ("entity", qloo.search_entities, "entity_id"),
            ("tag", qloo.search_tags, "id"),
        )
    else:
        lookups = (
            ("tag", qloo.search_tags, "id"),
            ("entity", qloo.search_entities, "entity_id"),
        )

    found: list[dict[str, str]] = []
    seen: set[str] = set()
    probe = {"label": raw, "kind": kind, "resolved_ids": []}
    for query in queries:
        for item_kind, method, id_key in lookups:
            hits = method(query) or []
            for hit in hits:
                if not isinstance(hit, dict):
                    continue
                hid = hit.get(id_key) or hit.get("id") or hit.get("entity_id")
                hit_name = hit.get("name") or ""
                if not hid or not hit_name:
                    continue
                hid = str(hid)
                if hid in seen:
                    continue
                if not exclusion_matches(probe, name=str(hit_name)):
                    continue
                seen.add(hid)
                found.append({"id": hid, "name": str(hit_name), "kind": item_kind})
                if len(found) >= MAX_RESOLVED_IDS:
                    return found
        if found:
            break
    return found


def unresolved_exclusion_labels(exclusions: list[dict[str, Any]] | None) -> list[str]:
    """Labels that did not resolve to a Qloo id. Missing flags are not guessed."""
    labels: list[str] = []
    for exclusion in exclusions or []:
        if exclusion.get("resolved") is False:
            labels.append(exclusion.get("label") or "")
    return [label for label in labels if label]


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
    persona: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
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
        "unresolved_exclusions": unresolved_exclusion_labels(session.get("exclusions")),
        "disclaimer": PREVIEW_DISCLAIMER,
        "claims": {
            "will": CLAIMS_WILL,
            "wont": CLAIMS_WONT,
            "background": COCHRANE_BACKGROUND,
        },
    }
    if persona:
        body["persona_summary"] = persona.get("persona_summary") or ""
        body["persona_facets"] = persona.get("facets") or {}
        body["qloo_plain"] = persona.get("qloo_plain") or ""
    return body


def seal_for_export(session: dict[str, Any], payload: dict[str, Any]) -> None:
    """Keep exclusions and the list. Drop anecdote text and the photo path.

    The playlist step still has to re-check exclusions, so the session stays.
    Family prose does not.
    """
    session["sealed"] = True
    session["export"] = {"payload": payload, "sealed_at": now_utc()}
    session["spotify_oauth"] = None
    for upload in session.get("uploads") or []:
        upload["photo_path"] = ""
        upload["anecdote"] = ""


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
        "unresolved_exclusions": unresolved_exclusion_labels(session.get("exclusions")),
        "boundary": CANDIDATES_BOUNDARY,
    }
