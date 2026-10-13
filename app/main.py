"""FastAPI app for Guided Memory Lane v0.

Local routes (invite-first; every session route requires the one-time
X-Invite-Token issued by POST /invites):

    POST   /invites
    POST   /sessions/{id}/uploads
    POST   /sessions/{id}/exclusions
    POST   /sessions/{id}/preview
    POST   /sessions/{id}/candidates
    DELETE /sessions/{id}

Preview and candidates refuse to run until trauma exclusions have been
acknowledged. Acknowledgment may look up Qloo tag/entity ids for the
exclusion labels themselves. A successful candidates response deletes the
session.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import JSONResponse

from . import session as domain
from .qloo import INSIGHT_TYPES, QlooAuthError, QlooClient, QlooUpstreamError
from .store import JsonStore, StoreError

REPO_ROOT = Path(__file__).resolve().parents[1]

APP_DESCRIPTION = (
    "Invite-first shared listening and looking sessions for families living "
    "with dementia. Photos are never analyzed, anecdotes never leave the "
    "family record, and Qloo cultural-affinity candidates are requested only "
    "after trauma exclusions and a caregiver preview."
)

_RESOLUTION_WARNING = (
    "Exclusions are saved, but Qloo could not resolve them to tag or entity "
    "ids. Those labels still filter by name, which can miss paraphrases."
)

_EXCLUSION_NOTE = (
    "Recorded before any recommendation request. An empty list still counts "
    "as a deliberate acknowledgment. Changing exclusions later requires a "
    "fresh preview. Labels that resolve to Qloo tag or entity ids are filtered "
    "by those ids. Unresolved labels are listed in unresolved_exclusions and "
    "still filter by name, which can miss paraphrases."
)


def default_store_path() -> Path:
    env_path = os.environ.get("GML_STORE_PATH")
    return Path(env_path) if env_path else REPO_ROOT / "data" / "store.json"


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def create_app(
    store_path: str | Path | None = None,
    qloo: Any | None = None,
    *,
    session_ttl_seconds: int | None = None,
    max_sessions: int | None = None,
) -> FastAPI:
    """Build the app. Tests inject a store path and a Qloo double."""
    app = FastAPI(title="Guided Memory Lane", version="0.1.0", description=APP_DESCRIPTION)
    store = JsonStore(store_path or default_store_path())
    qloo_client: Any | None = qloo
    ttl = (
        session_ttl_seconds
        if session_ttl_seconds is not None
        else _env_positive_int("GML_SESSION_TTL_SECONDS", domain.DEFAULT_SESSION_TTL_SECONDS)
    )
    session_cap = (
        max_sessions
        if max_sessions is not None
        else _env_positive_int("GML_MAX_SESSIONS", domain.DEFAULT_MAX_SESSIONS)
    )
    app.state.store = store
    app.state.session_ttl_seconds = ttl
    app.state.max_sessions = session_cap

    def get_qloo() -> Any:
        nonlocal qloo_client
        if qloo_client is None:
            # Lazy so importing the app without a key still works; the two
            # Qloo-touching routes surface a clean 503 instead.
            qloo_client = QlooClient()
        return qloo_client

    def load_session(session_id: str, token: str | None) -> dict[str, Any]:
        session = store.get(session_id)
        if session is None or domain.session_expired(session, ttl):
            if session is not None:
                store.delete(session_id)
            raise HTTPException(status_code=404, detail="session not found")
        if not domain.verify_invite(session, token):
            raise HTTPException(
                status_code=401,
                detail="invalid or missing invite token (X-Invite-Token header)",
            )
        return session

    def guarded_qloo_errors(exc: Exception) -> HTTPException:
        if isinstance(exc, QlooAuthError):
            return HTTPException(status_code=503, detail=str(exc))
        if isinstance(exc, QlooUpstreamError):
            return HTTPException(status_code=502, detail=str(exc))
        return HTTPException(status_code=500, detail="unexpected Qloo client error")

    def resolve_saved_exclusions(exclusions: list[dict[str, Any]]) -> str | None:
        """Attach Qloo ids when the lookup works. Never blocks the save."""
        if not exclusions:
            return None
        try:
            client_qloo = get_qloo()
        except (QlooAuthError, QlooUpstreamError):
            return _RESOLUTION_WARNING
        failed = False
        for exclusion in exclusions:
            try:
                exclusion["resolved_ids"] = domain.resolve_exclusion_ids(
                    exclusion["label"], exclusion["kind"], client_qloo
                )
            except (QlooAuthError, QlooUpstreamError):
                exclusion["resolved_ids"] = []
                failed = True
            exclusion["resolved"] = bool(exclusion["resolved_ids"])
        return _RESOLUTION_WARNING if failed else None

    @app.post("/invites", status_code=201)
    def create_invite(request: domain.InviteRequest) -> dict[str, Any]:
        session, token = domain.new_invite(request.caregiver_label)
        expired = {
            sid
            for sid, existing in store.read_all().items()
            if domain.session_expired(existing, ttl)
        }
        if not store.insert_if_room(session, session_cap, expired):
            raise HTTPException(
                status_code=429,
                detail=(
                    f"session limit reached ({session_cap}). Delete a session "
                    "or wait for unused sessions to expire."
                ),
            )
        return {
            "session_id": session["id"],
            "invite_token": token,
            "caregiver_label": session["caregiver_label"],
            "note": "This token is shown once. Keep it private; it authorizes this session.",
        }

    @app.post("/sessions/{session_id}/uploads", status_code=201)
    def add_upload(
        session_id: str,
        request: domain.UploadRequest,
        x_invite_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        with store.session_scope(session_id):
            session = load_session(session_id, x_invite_token)
            try:
                upload = domain.add_upload(session, request)
            except domain.SessionError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            store.put(session)
            return {
                "upload_id": upload["id"],
                "photo_on_file": True,
                "photo_note": "Path stored as metadata only. The photo is never opened or analyzed.",
                "anecdote_chars": len(upload["anecdote"]),
                "interests": upload["interests"],
                "upload_count": len(session["uploads"]),
            }

    @app.post("/sessions/{session_id}/exclusions")
    def set_exclusions(
        session_id: str,
        request: domain.ExclusionsRequest,
        x_invite_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        with store.session_scope(session_id):
            session = load_session(session_id, x_invite_token)
            exclusions = domain.acknowledge_exclusions(session, request)
            warning = resolve_saved_exclusions(exclusions)
            store.put(session)
            body: dict[str, Any] = {
                "acknowledged": True,
                "acknowledged_at": session["exclusions_acknowledged_at"],
                "exclusions": exclusions,
                "unresolved_exclusions": domain.unresolved_exclusion_labels(exclusions),
                "note": _EXCLUSION_NOTE,
            }
            if warning:
                body["resolution_warning"] = warning
            return body

    @app.post("/sessions/{session_id}/preview")
    def preview(
        session_id: str,
        x_invite_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        with store.session_scope(session_id):
            session = load_session(session_id, x_invite_token)
            try:
                domain.require_uploads(session)
                domain.require_exclusions_acknowledged(session)
                interests = domain.collect_interests(session)
                qloo = get_qloo()
                signals, unresolved = domain.resolve_signals(interests, qloo)
            except domain.SessionError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            except (QlooAuthError, QlooUpstreamError) as exc:
                raise guarded_qloo_errors(exc) from exc
            kept, withheld = domain.split_signals(signals, session["exclusions"] or [])
            session["resolved_signals"] = kept
            session["unresolved_interests"] = unresolved
            session["previewed_at"] = domain.now_utc()
            store.put(session)
            return domain.preview_payload(
                session, kept_signals=kept, withheld=withheld, unresolved=unresolved
            )

    @app.post("/sessions/{session_id}/candidates")
    def candidates(
        session_id: str,
        x_invite_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        with store.session_scope(session_id):
            session = load_session(session_id, x_invite_token)
            try:
                domain.require_previewed(session)
            except domain.SessionError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            signals = session["resolved_signals"] or []
            if not signals:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Preview produced no usable interest signals. Add explicit "
                        "caregiver interests, then re-run preview. Interests are never "
                        "guessed from photos or anecdotes."
                    ),
                )
            kept_signals, _ = domain.split_signals(signals, session["exclusions"] or [])
            if not kept_signals:
                raise HTTPException(
                    status_code=409,
                    detail="All interest signals are withheld by exclusions; nothing will be requested from Qloo.",
                )
            tag_ids = [signal["id"] for signal in kept_signals if signal["kind"] == "tag"]
            entity_ids = [signal["id"] for signal in kept_signals if signal["kind"] == "entity"]

            collected: list[dict[str, Any]] = []
            types_failed: list[dict[str, Any]] = []
            try:
                qloo = get_qloo()
                for label, urn in INSIGHT_TYPES.items():
                    try:
                        entities = qloo.insights(
                            entity_type=urn,
                            signal_tags=tag_ids,
                            signal_entities=entity_ids,
                            take=6,
                        )
                    except QlooUpstreamError as exc:
                        # One slow or failing category degrades, never kills, the
                        # response (a place-type query was observed hanging >60s
                        # live on 2026-10-13 while artist/film returned in ~2s).
                        types_failed.append({"type": label, "error": str(exc)[:200]})
                        continue
                    for entity in entities:
                        collected.append(
                            {
                                "entity_id": entity["entity_id"],
                                "name": entity["name"],
                                "type": label,
                                "image_url": entity.get("image_url"),
                            }
                        )
            except QlooAuthError as exc:
                raise guarded_qloo_errors(exc) from exc

            if not collected and types_failed:
                raise HTTPException(
                    status_code=502,
                    detail=f"all insight categories failed: {types_failed}",
                )
            kept, removed = domain.filter_candidates(collected, session["exclusions"] or [])
            for candidate in kept:
                candidate["why"] = domain.candidate_reason(candidate["name"], kept_signals)
            payload = domain.candidates_payload(session, kept, removed, types_failed)
            store.delete(session_id)
            return payload

    @app.delete("/sessions/{session_id}", status_code=204)
    def delete_session(
        session_id: str,
        x_invite_token: str | None = Header(default=None),
    ) -> Response:
        with store.session_scope(session_id):
            load_session(session_id, x_invite_token)
            store.delete(session_id)
        return Response(status_code=204)

    @app.exception_handler(StoreError)
    def store_error_handler(_: Any, exc: StoreError) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    return app


app = create_app()
