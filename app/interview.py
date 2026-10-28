"""The sitting as a chat. Each caregiver turn is stored as typed.

The old 12-probe blocking order is gone. A sitting needs three things
before the preview unlocks: at least one named interest, one moment in
the caregiver's words, and a leave_out answer (which may be "none").
Every interview response carries a short reply and 2-5 suggestion cards.

Cards have two sources. Before leave_out is stored the cards are local:
the typed phrases plus music/film/place lean cards. No Qloo call is made.
After exclusions are acknowledged and the preview is signed the cards come
from Qloo via the existing GET /v2/insights client, exclusion-filtered,
with no biographical text and no anecdote or photo path anywhere in the
payload. Chat insights never seal the sitting.

Ratings (1-5) tighten the curation: a 4 or 5 on a Qloo card adds that
entity id to the next signal.interests.entities, a 1 or 2 demotes the id
without writing exclusions or clearing previewed_at, and a 4 or 5 on a
local card keeps the typed phrase. Kind leans pick the next insights type.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .looking import photo_provided
from .qloo import INSIGHT_TYPES, TYPE_LABELS
from .session import MAX_TOTAL_INTERESTS, UploadRequest, add_upload

NONE_WORDS = frozenset({"none", "nothing", "no", "n/a", "na", "skip", "nothing else"})
MAX_PHRASE = 80
MAX_LEAVE_OUT_CHARS = 400
MAX_MOMENT_CHARS = 600
MAX_QLOO_CARDS = 5
MAX_PHRASE_CARDS = 3
LOCAL_CARD_MIN = 2

SLOT_INTEREST = "interest"
SLOT_MOMENT = "moment"
SLOT_LEAVE_OUT = "leave_out"
LEAN_KINDS = ("music", "film", "place")
LEAN_LABELS = {"music": "Music", "film": "Film", "place": "Place"}

ASKS: dict[str, str] = {
    SLOT_INTEREST: (
        "What do they love? A singer, a song, a film, a place, a hobby. "
        "A few words is fine."
    ),
    SLOT_MOMENT: "Tell me one ordinary moment you want in the room.",
    SLOT_LEAVE_OUT: (
        "What should stay out? A topic, a performer, a kind of film. "
        "Say none if nothing should."
    ),
}

FEEDBACK: tuple[dict[str, str], ...] = (
    {"id": "fit", "ask": "Did this list feel like them?"},
    {"id": "next", "ask": "What should I ask differently next time?"},
)

NO_PHOTO = "not-provided"

# Legacy probe ids from the old 12-probe interview, mapped onto chat slots.
_LEGACY_INTEREST_LEANS = {
    "music": "music",
    "music_more": "music",
    "film": "film",
    "film_more": "film",
    "place": "place",
    "hands": "",
}


def fresh_interview() -> dict[str, Any]:
    return {
        "interests": [],
        "moment": "",
        "leave_out": None,
        "photo_path": "",
        "photo_type": "",
        "committed": False,
    }


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def normalize_interview(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Return a chat-shape interview, upgrading the legacy answers shape.

    Does not mutate the caller's dict. Legacy habit probes (when, span,
    company) are dropped; leave_out and leave_more phrases merge.
    """
    raw = raw if isinstance(raw, dict) else {}
    if not isinstance(raw.get("answers"), dict):
        state = fresh_interview()
        state["interests"] = [
            dict(item)
            for item in raw.get("interests") or []
            if isinstance(item, dict) and str(item.get("phrase") or "").strip()
        ]
        state["moment"] = str(raw.get("moment") or "")
        leave = raw.get("leave_out")
        state["leave_out"] = None if leave is None else str(leave)
        state["photo_path"] = str(raw.get("photo_path") or "")
        state["photo_type"] = str(raw.get("photo_type") or "")
        state["committed"] = bool(raw.get("committed"))
        return state

    answers: dict[str, str] = raw["answers"]
    state = fresh_interview()
    seen: set[str] = set()
    for probe_id, lean in _LEGACY_INTEREST_LEANS.items():
        for phrase in _phrases(answers.get(probe_id, "")):
            cleaned = soft_normalize_interest(phrase)
            key = cleaned.casefold()
            if not cleaned or key in seen:
                continue
            seen.add(key)
            state["interests"].append({"phrase": cleaned, "lean": lean, "at": ""})
    state["moment"] = str(answers.get("moment") or "")
    leave_bits = [
        text
        for text in (answers.get("leave_out"), answers.get("leave_more"))
        if text and text.strip() and text.strip().casefold() not in NONE_WORDS
    ]
    state["leave_out"] = ", ".join(leave_bits) if leave_bits else None
    state["photo_path"] = str(raw.get("photo_path") or "")
    state["photo_type"] = str(raw.get("photo_type") or "")
    state["committed"] = bool(raw.get("committed"))
    return state


def phrases_of(state: dict[str, Any]) -> list[str]:
    """Stored interest phrases in order, deduped, capped."""
    seen: set[str] = set()
    ordered: list[str] = []
    for item in state.get("interests") or []:
        phrase = soft_normalize_interest(str(item.get("phrase") or ""))
        key = phrase.casefold()
        if not phrase or key in seen:
            continue
        seen.add(key)
        ordered.append(phrase)
    return ordered[:MAX_TOTAL_INTERESTS]


def leans_of(state: dict[str, Any]) -> dict[str, str]:
    """First lean recorded for each phrase, keyed by phrase."""
    leans: dict[str, str] = {}
    for item in state.get("interests") or []:
        phrase = soft_normalize_interest(str(item.get("phrase") or ""))
        lean = str(item.get("lean") or "")
        if phrase and lean in LEAN_KINDS and phrase.casefold() not in leans:
            leans[phrase.casefold()] = lean
    return leans


def next_slot(state: dict[str, Any]) -> str | None:
    """The next thing to ask for. None once the preview can be built."""
    if not phrases_of(state):
        return SLOT_INTEREST
    if not str(state.get("moment") or "").strip():
        return SLOT_MOMENT
    if state.get("leave_out") is None:
        return SLOT_LEAVE_OUT
    return None


def preview_ready(state: dict[str, Any]) -> bool:
    """One interest, one moment, and a leave_out answer unlock the preview."""
    return next_slot(state) is None


def _rich(text: str) -> bool:
    folded = text.casefold()
    return len(text) >= 40 or "," in text or " and " in folded


def _phrases(text: str) -> list[str]:
    parts = [part.strip() for part in text.replace(";", ",").split(",")]
    kept: list[str] = []
    for part in parts:
        if not part or part.casefold() in NONE_WORDS:
            continue
        kept.append(part)
    return kept


def acknowledge(answer: str) -> str:
    """One sentence in Tuck's voice. Quotes their words. Does not add a life story."""
    text = " ".join(str(answer or "").split())
    if not text:
        return ""
    if text.casefold() in NONE_WORDS:
        return "I'll leave that as none."
    if len(text) > 72:
        return f'I\'ll keep "{text[:69].rstrip()}...".'
    return f'I\'ll keep "{text.rstrip(chr(46))}."'


def validate_interest_text(text: str) -> str:
    text = " ".join(str(text or "").split())
    if not text:
        raise ValueError("Say it in a few words, or say none.")
    if text.casefold() in NONE_WORDS:
        raise ValueError("Name something they like, in a few words.")
    for part in _phrases(text):
        if len(part) < 3:
            raise ValueError("Use at least a few letters for each name.")
        if len(part) > MAX_PHRASE:
            raise ValueError("Keep each name under 80 characters. Commas split more than one.")
    if not _phrases(text):
        raise ValueError("Name something they like, in a few words.")
    return text


def validate_moment_text(text: str) -> str:
    text = " ".join(str(text or "").split())
    if not text:
        raise ValueError("Say it in a few words, or say none.")
    if len(text) > MAX_MOMENT_CHARS:
        raise ValueError("Keep the moment under 600 characters.")
    if text.casefold() in NONE_WORDS:
        raise ValueError("I need one moment in your words, even a small one.")
    return text


def validate_leave_out_text(text: str) -> str:
    text = " ".join(str(text or "").split())
    if not text:
        raise ValueError("Say none if nothing should stay out.")
    if len(text) > MAX_LEAVE_OUT_CHARS:
        raise ValueError("Keep that under 400 characters.")
    if text.casefold() not in NONE_WORDS:
        for part in _phrases(text):
            if len(part) > MAX_PHRASE:
                raise ValueError("Keep each thing that stays out under 80 characters.")
    return text


_TYPO_FIXES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bmotorcyles\b", re.IGNORECASE), "motorcycles"),
)


def soft_normalize_interest(phrase: str) -> str:
    """Fix a few obvious typos. Does not invent names or expand biography."""
    out = " ".join(str(phrase or "").split())
    for pattern, repl in _TYPO_FIXES:
        out = pattern.sub(repl, out)
    return out


def store_interest_turn(state: dict[str, Any], text: str, lean: str | None, at: str) -> None:
    """Add typed phrases with their lean. Existing phrases keep their first lean."""
    lean = lean if lean in LEAN_KINDS else ""
    known = {item.get("phrase", "").casefold() for item in state.get("interests") or []}
    for phrase in _phrases(text):
        cleaned = soft_normalize_interest(phrase)
        if not cleaned or cleaned.casefold() in known:
            continue
        known.add(cleaned.casefold())
        state.setdefault("interests", []).append({"phrase": cleaned, "lean": lean, "at": at})


def drop_interest_phrase(session: dict[str, Any], phrase: str) -> None:
    """Demote a typed phrase: out of the interview and out of every upload."""
    target = soft_normalize_interest(phrase).casefold()
    interview = session.get("interview") or {}
    kept = [
        item
        for item in interview.get("interests") or []
        if str(item.get("phrase") or "").casefold() != target
    ]
    if "interests" in interview:
        interview["interests"] = kept
    for upload in session.get("uploads") or []:
        if isinstance(upload, dict) and isinstance(upload.get("interests"), list):
            upload["interests"] = [
                item
                for item in upload["interests"]
                if str(item or "").casefold() != target
            ]


def ensure_interest_phrase(session: dict[str, Any], phrase: str, lean: str = "") -> None:
    """Keep a typed phrase in the signal: interview state and first upload."""
    cleaned = soft_normalize_interest(phrase)
    if not cleaned:
        return
    interview = session.setdefault("interview", fresh_interview())
    known = {str(item.get("phrase") or "").casefold() for item in interview.get("interests") or []}
    if cleaned.casefold() not in known:
        interview.setdefault("interests", []).append(
            {"phrase": cleaned, "lean": lean, "at": ""}
        )
    for upload in session.get("uploads") or []:
        if isinstance(upload, dict) and isinstance(upload.get("interests"), list):
            if cleaned not in upload["interests"] and cleaned.casefold() not in {
                str(item or "").casefold() for item in upload["interests"]
            }:
                if len(upload["interests"]) < MAX_TOTAL_INTERESTS:
                    upload["interests"].append(cleaned)
            return


def materialize(state: dict[str, Any], photo_path: str | None = None) -> dict[str, Any]:
    """Turn chat state into upload fields. Skipped and 'none' add nothing."""
    interests = phrases_of(state)
    leave_out = str(state.get("leave_out") or "")
    exclusions = _phrases(leave_out) if leave_out.casefold() not in NONE_WORDS else []
    anecdote = str(state.get("moment") or "").strip()
    chosen = str(photo_path or state.get("photo_path") or "").strip()
    if not photo_provided(chosen):
        chosen = NO_PHOTO
    return {
        "interests": interests,
        "exclusions": exclusions[:12],
        "habits": [],
        "anecdote": anecdote,
        "photo_path": chosen,
    }


def apply_looking_photo(
    session: dict[str, Any],
    *,
    href: str,
    media_type: str,
    anecdote: str,
) -> None:
    """Attach an opaque photo. Replace a not-provided placeholder when one exists."""
    interview = session.setdefault("interview", fresh_interview())
    interview["photo_path"] = href
    interview["photo_type"] = media_type
    if not interview.get("committed"):
        return
    for upload in session.get("uploads") or []:
        path = str(upload.get("photo_path") or "")
        if path.startswith("/data/") or not photo_provided(path):
            upload["photo_path"] = href
            upload["photo_type"] = media_type
            upload["anecdote"] = anecdote
            return
    add_upload(session, UploadRequest(photo_path=href, anecdote=anecdote, interests=[]))


def persona_summary(
    *,
    interests: list[str] | None = None,
    habits: list[dict[str, str]] | list[str] | None = None,
    anecdote: str | None = None,
    exclusions: list[str] | None = None,
    caregiver_label: str | None = None,
) -> dict[str, Any]:
    """Warm first-person portrait grounded only in stated answers.

    Synthesis and rephrase are allowed. Invented biography is not.
    """
    names = [str(item).strip() for item in (interests or []) if str(item).strip()]
    habit_bits: list[str] = []
    for item in habits or []:
        if isinstance(item, dict):
            bit = str(item.get("text") or "").strip()
        else:
            bit = str(item).strip()
        if bit and bit.casefold() not in NONE_WORDS:
            habit_bits.append(bit)
    left_out = [str(item).strip() for item in (exclusions or []) if str(item).strip()]
    moment = " ".join(str(anecdote or "").split()).strip()
    who = (caregiver_label or "them").strip() or "them"

    if not names and not habit_bits and not moment:
        portrait = (
            f"I still need a few words about {who} before I can sketch who this "
            "listening is for."
        )
    else:
        parts: list[str] = []
        if names:
            if len(names) == 1:
                parts.append(f"From what you told me, this sitting leans toward {names[0]}.")
            elif len(names) == 2:
                parts.append(
                    f"From what you told me, this sitting leans toward {names[0]} and {names[1]}."
                )
            else:
                head = ", ".join(names[:-1])
                parts.append(
                    f"From what you told me, this sitting leans toward {head}, and {names[-1]}."
                )
        if habit_bits:
            parts.append("How they like to be with it: " + "; ".join(habit_bits[:3]) + ".")
        if moment:
            clipped = moment if len(moment) <= 160 else moment[:157].rstrip() + "..."
            parts.append(f"One ordinary moment you named: {clipped.rstrip(chr(46))}.")
        if left_out:
            parts.append("We will keep out: " + ", ".join(left_out[:6]) + ".")
        else:
            parts.append("You did not name anything that must stay out.")
        portrait = " ".join(parts)

    ask_plain = (
        "Next I would ask for music, film, and place ideas that sit near what you "
        "named. I only send the matched names, never the photo and never the story text."
    )
    return {
        "persona_summary": portrait,
        "facets": {
            "interests": names[:8],
            "habits": habit_bits[:6],
            "anecdote": moment,
            "exclusions": left_out[:12],
            "caregiver_label": who,
        },
        "qloo_plain": ask_plain,
    }


def pending_feedback(saved: dict[str, Any] | None) -> dict[str, str] | None:
    data = saved or {}
    for item in FEEDBACK:
        if not str(data.get(item["id"]) or "").strip():
            return item
    return None


# --------------------------------------------------------------------------
# Suggestion cards
# --------------------------------------------------------------------------


def _card_id(source: str, kind: str, key: str) -> str:
    return "c_" + _hash_token(f"{source}|{kind}|{key.casefold()}")


def _phrase_card(phrase: str, lean: str) -> dict[str, Any]:
    kind = lean if lean in LEAN_KINDS else "thing"
    return {
        "card_id": _card_id("local", kind, phrase),
        "source": "local",
        "kind": kind,
        "name": phrase,
        "entity_id": None,
        "phrase": phrase,
        "type": None,
        "image_url": None,
        "why": "You named this.",
    }


def _lean_card(kind: str) -> dict[str, Any]:
    label = LEAN_LABELS[kind]
    return {
        "card_id": _card_id("local", kind, f"lean:{kind}"),
        "source": "local",
        "kind": kind,
        "name": label,
        "entity_id": None,
        "phrase": None,
        "type": None,
        "image_url": None,
        "why": f"Rate {label} high to lean the next suggestions toward {label.lower()}.",
    }


def local_cards(state: dict[str, Any]) -> list[dict[str, Any]]:
    """2-5 local cards: the typed phrases, then music/film/place lean cards."""
    leans = leans_of(state)
    cards = [
        _phrase_card(phrase, leans.get(phrase.casefold(), ""))
        for phrase in reversed(phrases_of(state)[:MAX_PHRASE_CARDS])
    ]
    for kind in LEAN_KINDS:
        if len(cards) >= 5:
            break
        cards.append(_lean_card(kind))
    return cards[:5]


def demoted_keys(session: dict[str, Any]) -> set[str]:
    """Entity ids and phrases whose latest rating is 1 or 2."""
    return set(rating_effects(session)["demoted"])


def qloo_cards(
    session: dict[str, Any],
    entities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build qloo-source cards. Exclusion-filtered, demoted ids dropped.

    The input entities come from the GET /v2/insights client, which already
    drops biographical properties. Only safe display fields are copied, so
    no biography, anecdote, or photo path can appear on a card.
    """
    from .session import exclusion_matches

    exclusions = session.get("exclusions") or []
    demoted = {str(key).casefold() for key in demoted_keys(session)}
    cards: list[dict[str, Any]] = []
    for entity in entities:
        entity_id = str(entity.get("entity_id") or "")
        name = str(entity.get("name") or "")
        if not entity_id or not name or entity_id.casefold() in demoted:
            continue
        if any(
            exclusion_matches(exclusion, name=name, entity_id=entity_id)
            for exclusion in exclusions
            if isinstance(exclusion, dict)
        ):
            continue
        type_label = entity.get("type") or TYPE_LABELS.get(entity.get("subtype") or "") or "music"
        cards.append(
            {
                "card_id": _card_id("qloo", type_label, entity_id),
                "source": "qloo",
                "kind": type_label,
                "name": name,
                "entity_id": entity_id,
                "phrase": None,
                "type": type_label,
                "image_url": entity.get("image_url"),
                "why": "Near what you named.",
            }
        )
        if len(cards) >= MAX_QLOO_CARDS:
            break
    return cards


# --------------------------------------------------------------------------
# Ratings
# --------------------------------------------------------------------------


def record_rating(
    session: dict[str, Any],
    card: dict[str, Any],
    score: int,
    at: str,
) -> dict[str, Any]:
    """Store one 1-5 rating and apply its narrowing immediately.

    A 4-5 on a qloo card promotes the entity id into the next
    signal.interests.entities. A 1-2 demotes: the id or typed phrase leaves
    the signal, but exclusions are never written and previewed_at is never
    cleared. A 4-5 on a local phrase card keeps the typed phrase.
    """
    entry = {
        "card_id": str(card.get("card_id") or ""),
        "score": score,
        "source": str(card.get("source") or "local"),
        "kind": str(card.get("kind") or ""),
        "name": str(card.get("name") or ""),
        "entity_id": card.get("entity_id") or None,
        "phrase": card.get("phrase") or None,
        "at": at,
    }
    ledger = session.setdefault("ratings", [])
    if not isinstance(ledger, list):
        ledger = []
        session["ratings"] = ledger
    ledger.append(entry)
    phrase = entry["phrase"]
    if phrase:
        if score >= 4:
            ensure_interest_phrase(session, phrase, lean=entry["kind"] if entry["kind"] in LEAN_KINDS else "")
        elif score <= 2:
            drop_interest_phrase(session, phrase)
    return entry


def rating_effects(session: dict[str, Any]) -> dict[str, Any]:
    """Aggregates from the rating ledger. Latest score per card wins."""
    latest: dict[str, dict[str, Any]] = {}
    for entry in session.get("ratings") or []:
        if isinstance(entry, dict) and entry.get("card_id"):
            latest[str(entry["card_id"])] = entry
    promoted: list[str] = []
    demoted: set[str] = set()
    leans: dict[str, int] = {kind: 0 for kind in LEAN_KINDS}
    for entry in latest.values():
        score = int(entry.get("score") or 0)
        kind = str(entry.get("kind") or "")
        entity_id = entry.get("entity_id") or None
        phrase = entry.get("phrase") or None
        if 4 <= score <= 5:
            if entity_id and str(entity_id) not in promoted:
                promoted.append(str(entity_id))
            if kind in leans:
                leans[kind] += 1
        elif 1 <= score <= 2:
            if entity_id:
                demoted.add(str(entity_id))
            if phrase:
                demoted.add(str(phrase))
            if kind in leans:
                leans[kind] -= 1
    return {"promoted": promoted, "demoted": demoted, "leans": leans}


def next_insight_urn(session: dict[str, Any]) -> str:
    """The kind leans pick the next insights type. Ties and no-lean go to music."""
    leans = rating_effects(session)["leans"]
    best_kind = "music"
    best_score = 0
    for kind in LEAN_KINDS:
        score = leans.get(kind, 0)
        if score > best_score:
            best_kind = kind
            best_score = score
    return INSIGHT_TYPES[best_kind]


def chat_signal_ids(session: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(tag ids, entity ids) for the next insights call, ratings applied.

    Resolved entity signals minus demoted ids, plus promoted ids. Tag
    signals are unchanged; demoted phrases were already removed from the
    stored interests, so they never resolve again.
    """
    effects = rating_effects(session)
    demoted = {str(key).casefold() for key in effects["demoted"]}
    tag_ids: list[str] = []
    entity_ids: list[str] = []
    for signal in session.get("resolved_signals") or []:
        if not isinstance(signal, dict):
            continue
        signal_id = str(signal.get("id") or "")
        if not signal_id or signal_id.casefold() in demoted:
            continue
        if signal.get("kind") == "tag" and signal_id not in tag_ids:
            tag_ids.append(signal_id)
        elif signal.get("kind") == "entity" and signal_id not in entity_ids:
            entity_ids.append(signal_id)
    for promoted in effects["promoted"]:
        if promoted.casefold() not in {existing.casefold() for existing in entity_ids}:
            entity_ids.append(promoted)
    return tag_ids, entity_ids
