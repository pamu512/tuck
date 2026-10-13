"""FastAPI app for Guided Memory Lane v0.

Local routes (invite-first; every session route requires the one-time
X-Invite-Token issued by POST /invites):

    POST /invites
    POST /sessions/{id}/uploads
    POST /sessions/{id}/exclusions
    POST /sessions/{id}/preview
    POST /sessions/{id}/candidates

The only two routes that talk to Qloo are preview and candidates, and both
refuse to run until trauma exclusions have been acknowledged.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
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


def default_store_path() -> Path:
    env_path = os.environ.get("GML_STORE_PATH")
    return Path(env_path) if env_path else REPO_ROOT / "data" / "store.json"


def create_app(
    store_path: str | Path | None = None,
    qloo: Any | None = None,
) -> FastAPI:
    """Build the app. Tests inject a store path and a Qloo double."""
    app = FastAPI(title="Guided Memory Lane", version="0.1.0", description=APP_DESCRIPTION)
    store = JsonStore(store_path or default_store_path())
    qloo_client: Any | None = qloo

    def get_qloo() -> Any:
        nonlocal qloo_client
        if qloo_client is None:
            # Lazy so importing the app without a key still works; the two
            # Qloo-touching routes surface a clean 503 instead.
            qloo_client = QlooClient()
        return qloo_client

    def load_session(session_id: str, token: str | None) -> dict[str, Any]:
        session = store.get(session_id)
        if session is None:
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

    @app.post("/invites", status_code=201)
    def create_invite(request: domain.InviteRequest) -> dict[str, Any]:
        session, token = domain.new_invite(request.caregiver_label)
        store.put(session)
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
        session = load_session(session_id, x_invite_token)
        exclusions = domain.acknowledge_exclusions(session, request)
        store.put(session)
        return {
            "acknowledged": True,
            "acknowledged_at": session["exclusions_acknowledged_at"],
            "exclusions": exclusions,
            "note": (
                "Recorded before any Qloo call. An empty list still counts as a "
                "deliberate acknowledgment. Changing exclusions later requires a "
                "fresh preview."
            ),
        }

    @app.post("/sessions/{session_id}/preview")
    def preview(
        session_id: str,
        x_invite_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
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
        return domain.candidates_payload(session, kept, removed, types_failed)

    @app.exception_handler(StoreError)
    def store_error_handler(_: Any, exc: StoreError) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    return app


app = create_app()
