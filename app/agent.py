"""Thin guide for the session order.

Qloo is reached only to resolve caregiver-typed interests and exclusion
labels. Anecdote text and photo paths are never placed in a tool payload.
Candidates stay closed until the preview has been signed.
"""

from __future__ import annotations

from typing import Any, Callable

from .qloo import QlooAuthError, QlooUpstreamError
from .session import (
    SessionError,
    collect_interests,
    require_exclusions_acknowledged,
    require_previewed,
    require_uploads,
    resolve_exclusion_ids,
    resolve_signals,
)

_RESOLUTION_WARNING = (
    "Exclusions are saved, but Qloo could not resolve them to tag or entity "
    "ids. Those labels still filter by name, which can miss paraphrases."
)


def interest_payload(session: dict[str, Any]) -> dict[str, list[str]]:
    """The only interest text a Qloo tool may see."""
    return {"interests": collect_interests(session)}


def status(session: dict[str, Any]) -> dict[str, Any]:
    """Where the session is, and whether a candidates call is allowed."""
    if session.get("sealed"):
        return {
            "step": "export",
            "qloo": "closed",
            "candidates_open": False,
            "note": (
                "The list is signed. The anecdote and photo path are cleared. "
                "Playlist export can still re-check exclusions."
            ),
        }
    if not session.get("uploads"):
        return {
            "step": "upload",
            "qloo": "closed",
            "candidates_open": False,
            "note": "Qloo is not called yet. Add a memory and an interest first.",
        }
    if session.get("exclusions_acknowledged_at") is None:
        return {
            "step": "exclusions",
            "qloo": "closed",
            "candidates_open": False,
            "note": (
                "Record exclusions before any recommendation. An empty list "
                "still has to be acknowledged."
            ),
        }
    if not session.get("previewed_at"):
        return {
            "step": "preview",
            "qloo": "closed",
            "candidates_open": False,
            "note": "Candidates stay closed until you sign the preview.",
        }
    return {
        "step": "candidates",
        "qloo": "open",
        "candidates_open": True,
        "note": "Preview is signed. The list may be requested from the signal ids you just saw.",
    }


def assert_candidates(session: dict[str, Any]) -> None:
    """Refuse a new candidates call until the preview is signed.

    A sealed session is not a new call; the route replays the stored list.
    """
    if session.get("sealed"):
        return
    require_previewed(session)


def prepare_preview(session: dict[str, Any], qloo: Any) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """Resolve explicit interests. Refuses to run before exclusions are acknowledged."""
    require_uploads(session)
    require_exclusions_acknowledged(session)
    payload = interest_payload(session)
    signals, unresolved = resolve_signals(payload["interests"], qloo)
    trace = {
        "tools": ["search_tags", "search_entities"],
        "input": payload,
    }
    return signals, unresolved, trace


def resolve_saved_exclusions(
    exclusions: list[dict[str, Any]],
    qloo_getter: Callable[[], Any],
) -> str | None:
    """Attach Qloo ids when the lookup works. A failure still keeps the labels."""
    if not exclusions:
        return None
    try:
        client_qloo = qloo_getter()
    except (QlooAuthError, QlooUpstreamError):
        return _RESOLUTION_WARNING
    failed = False
    for exclusion in exclusions:
        try:
            exclusion["resolved_ids"] = resolve_exclusion_ids(
                exclusion["label"], exclusion["kind"], client_qloo
            )
        except (QlooAuthError, QlooUpstreamError):
            exclusion["resolved_ids"] = []
            failed = True
        exclusion["resolved"] = bool(exclusion["resolved_ids"])
    return _RESOLUTION_WARNING if failed else None
