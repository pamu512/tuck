"""Adaptive probes. Each answer is stored as typed. Nothing is inferred from it.

Full path asks 12. Exit opens once 8 answers are in, and it still asks what
should stay out before a preview can be built.
"""

from __future__ import annotations

import re
from typing import Any

from .session import MAX_TOTAL_INTERESTS

NONE_WORDS = frozenset({"none", "nothing", "no", "n/a", "na", "skip", "nothing else"})
MIN_ANSWERS_BEFORE_EXIT = 8
MAX_PHRASE = 80

PROBES: tuple[dict[str, Any], ...] = (
    {
        "id": "hands",
        "topic": "hobbies",
        "writes": "interest",
        "optional": False,
        "skip_on_exit": False,
        "ask": "What do they still like to do, or what did they like doing with their hands?",
    },
    {
        "id": "place",
        "topic": "hobbies",
        "writes": "interest",
        "optional": True,
        "skip_on_exit": True,
        "ask": "Is there a place that belongs with that? A kitchen, a garden, a town.",
    },
    {
        "id": "moment",
        "topic": "memory",
        "writes": "anecdote",
        "optional": False,
        "skip_on_exit": False,
        "ask": "Tell me one ordinary moment you want in the room.",
    },
    {
        "id": "music",
        "topic": "music",
        "writes": "interest",
        "optional": False,
        "skip_on_exit": False,
        "ask": "Name a singer, a song, or a kind of music they would put on.",
    },
    {
        "id": "music_more",
        "topic": "music",
        "writes": "interest",
        "optional": True,
        "skip_on_exit": True,
        "follow": "music",
        "ask": "Is there another singer or song, or is that the one?",
    },
    {
        "id": "film",
        "topic": "movies",
        "writes": "interest",
        "optional": False,
        "skip_on_exit": False,
        "ask": "A film, or a kind of film, they would sit through?",
    },
    {
        "id": "film_more",
        "topic": "movies",
        "writes": "interest",
        "optional": True,
        "skip_on_exit": True,
        "follow": "film",
        "ask": "Another film, or is that enough?",
    },
    {
        "id": "when",
        "topic": "habits",
        "writes": "habit",
        "optional": False,
        "skip_on_exit": True,
        "ask": "What time of day is easiest to sit and listen?",
    },
    {
        "id": "span",
        "topic": "habits",
        "writes": "habit",
        "optional": False,
        "skip_on_exit": True,
        "ask": "How long is comfortable? A few songs, or closer to an hour?",
    },
    {
        "id": "company",
        "topic": "habits",
        "writes": "habit",
        "optional": True,
        "skip_on_exit": True,
        "ask": "Who else is usually in the room with you?",
    },
    {
        "id": "leave_out",
        "topic": "exclusions",
        "writes": "exclusion",
        "optional": False,
        "skip_on_exit": False,
        "ask": "What should stay out? A topic, a performer, a kind of film. Say none if nothing should.",
    },
    {
        "id": "leave_more",
        "topic": "exclusions",
        "writes": "exclusion",
        "optional": True,
        "skip_on_exit": True,
        "ask": "Anything else that should stay out?",
    },
)

FEEDBACK: tuple[dict[str, str], ...] = (
    {"id": "fit", "ask": "Did this list feel like them?"},
    {"id": "next", "ask": "What should I ask differently next time?"},
)

PROBES_BY_ID = {probe["id"]: probe for probe in PROBES}
NO_PHOTO = "not-provided"


def fresh_interview() -> dict[str, Any]:
    return {"answers": {}, "exited": False, "committed": False}


def _rich(text: str) -> bool:
    folded = text.casefold()
    return len(text) >= 40 or "," in text or " and " in folded


def _skipped(probe: dict[str, Any], answers: dict[str, str], exited: bool) -> bool:
    if probe["id"] in answers:
        return False
    follow = probe.get("follow")
    if follow and _rich(answers.get(follow, "")):
        return True
    if probe["id"] == "leave_more":
        prior = answers.get("leave_out", "").strip().casefold()
        if prior in NONE_WORDS:
            return True
    if exited and probe.get("skip_on_exit"):
        return True
    return False


def next_probe(answers: dict[str, str], exited: bool) -> dict[str, Any] | None:
    for probe in PROBES:
        if probe["id"] in answers:
            continue
        if _skipped(probe, answers, exited):
            continue
        return probe
    return None


def can_exit(answers: dict[str, str], exited: bool) -> bool:
    if exited or len(answers) < MIN_ANSWERS_BEFORE_EXIT:
        return False
    if "moment" not in answers:
        return False
    return any(PROBES_BY_ID[key]["writes"] == "interest" for key in answers if key in PROBES_BY_ID)


def _phrases(text: str) -> list[str]:
    parts = [part.strip() for part in text.replace(";", ",").split(",")]
    kept: list[str] = []
    for part in parts:
        if not part or part.casefold() in NONE_WORDS:
            continue
        kept.append(part)
    return kept


def validate_answer(probe: dict[str, Any], text: str) -> str:
    text = " ".join(text.split())
    if not text:
        raise ValueError("Say it in a few words, or say none.")
    kind = probe["writes"]
    if kind == "anecdote":
        if len(text) > 600:
            raise ValueError("Keep the moment under 600 characters.")
        if text.casefold() in NONE_WORDS:
            raise ValueError("I need one moment in your words, even a small one.")
        return text
    if kind == "habit":
        if len(text) > 200:
            raise ValueError("Keep that to a short note.")
        return text
    if text.casefold() in NONE_WORDS:
        if kind == "exclusion" or probe.get("optional"):
            return text
        raise ValueError("Name something they like, in a few words.")
    for part in _phrases(text):
        if len(part) < 3:
            raise ValueError("Use at least a few letters for each name.")
        if len(part) > MAX_PHRASE:
            raise ValueError("Keep each name under 80 characters. Commas split more than one.")
    if not _phrases(text):
        raise ValueError("Name something they like, in a few words.")
    return text


_TYPO_FIXES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bmotorcyles\b", re.IGNORECASE), "motorcycles"),
)

# Music and film answers must survive when early hobby probes fill many phrases.
_CULTURAL_TOPICS = frozenset({"music", "movies"})


def soft_normalize_interest(phrase: str) -> str:
    """Fix a few obvious typos. Does not invent names or expand biography."""
    out = " ".join(phrase.split())
    for pattern, repl in _TYPO_FIXES:
        out = pattern.sub(repl, out)
    return out


def materialize(answers: dict[str, str]) -> dict[str, Any]:
    """Turn stored answers into upload fields. Skipped and 'none' add nothing.

    Early hobby probes can emit many comma-separated phrases. Cap retention is
    topic-aware: music and film answers are kept before hobbies/places so a
    long hands/place list cannot zero out named cultural entities.
    """
    cultural: list[str] = []
    other: list[str] = []
    exclusions: list[str] = []
    habits: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(phrase: str, bucket: list[str]) -> None:
        cleaned = soft_normalize_interest(phrase)
        key = cleaned.casefold()
        if not cleaned or key in seen:
            return
        seen.add(key)
        bucket.append(cleaned)

    for probe in PROBES:
        text = answers.get(probe["id"])
        if not text:
            continue
        kind = probe["writes"]
        if kind == "interest":
            bucket = cultural if probe["topic"] in _CULTURAL_TOPICS else other
            for phrase in _phrases(text):
                _add(phrase, bucket)
        elif kind == "habit" and text.casefold() not in NONE_WORDS:
            habits.append({"id": probe["id"], "text": text})
        elif kind == "exclusion":
            exclusions.extend(_phrases(text))
    # Cultural first, then fill remaining slots with hobbies/places.
    interests = list(cultural)
    if len(interests) > MAX_TOTAL_INTERESTS:
        interests = interests[:MAX_TOTAL_INTERESTS]
    else:
        room = MAX_TOTAL_INTERESTS - len(interests)
        interests.extend(other[:room])
    anecdote = (answers.get("moment") or "").strip()
    return {
        "interests": interests,
        "exclusions": exclusions[:12],
        "habits": habits,
        "anecdote": anecdote,
        "photo_path": NO_PHOTO,
    }


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
