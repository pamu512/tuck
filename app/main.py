"""FastAPI app for Tuck (Guided Memory Lane).

An account cookie is required before a session. Session routes still require
the one-time X-Invite-Token from POST /invites. POST /dev/sample is the only
unauthenticated session, and it only loads the canned sample family.

    GET    /
    POST   /accounts
    POST   /login
    POST   /logout
    GET    /me
    POST   /me/profile
    GET    /sample-family
    POST   /dev/sample
    POST   /invites
    GET    /sessions/{id}/interview
    POST   /sessions/{id}/interview
    POST   /sessions/{id}/feedback
    POST   /sessions/{id}/uploads
    POST   /sessions/{id}/data
    GET    /data/{photo_id}
    POST   /sessions/{id}/exclusions
    POST   /sessions/{id}/preview
    POST   /sessions/{id}/guide
    POST   /sessions/{id}/candidates
    GET    /sessions/{id}/export
    GET    /sessions/{id}/playlist.m3u
    POST   /sessions/{id}/spotify/start
    GET    /spotify/callback
    DELETE /sessions/{id}

Preview and candidates refuse to run until exclusions have been acknowledged.
A successful candidates response seals the session.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import session as domain
from .accounts import (
    ACCOUNT_COOKIE,
    LoginRequest,
    ProfileNameRequest,
    RegisterRequest,
    clear_failures,
    dummy_password,
    issue_cookie,
    locked_for,
    new_account,
    note_failure,
    public_account,
    read_cookie,
    read_secret_file,
    resolve_session_secret,
    verify_password,
    write_commit,
    write_feedback,
    write_person_name,
    write_probe,
)
from .agent import (
    assert_candidates,
    interest_payload,
    prepare_preview,
    resolve_saved_exclusions,
    status as guide_status,
)
from .playlist import (
    OAUTH_MAX_AGE_SECONDS,
    SpotifyAPI,
    SpotifyError,
    assemble_tracks,
    authorize_url,
    oauth_expired,
    pkce_pair,
    playlist_description,
    playlist_name,
    render_m3u,
    uris_to_add,
)
from .interview import (
    FEEDBACK,
    persona_summary,
    NO_PHOTO,
    PROBES,
    PROBES_BY_ID,
    acknowledge,
    apply_looking_photo,
    can_exit,
    fresh_interview,
    materialize,
    next_probe,
    pending_feedback,
    validate_answer,
)
from .looking import (
    PHOTO_ID_RE,
    PhotoStoreError,
    allocate_photo_id,
    discard_opaque,
    normalize_media_type,
    photo_provided,
    read_photo_bytes,
    session_photo_media,
    store_opaque,
)
from .qloo import INSIGHT_TYPES, QlooAuthError, QlooClient, QlooUpstreamError
from .sample import SAMPLE
from .store import JsonStore, StoreError

_spotify_log = logging.getLogger("tuck.spotify")

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"

APP_DESCRIPTION = (
    "Tuck asks one question at a time. An account is required before a sitting. "
    "Photos are never analyzed, anecdotes never leave the family record, and "
    "Qloo candidates are requested only after exclusions and a preview."
)

_EXCLUSION_NOTE = (
    "Recorded before any recommendation request. An empty list still counts "
    "as a deliberate acknowledgment. Changing exclusions later requires a "
    "fresh preview. Labels that resolve to Qloo tag or entity ids are filtered "
    "by those ids. Unresolved labels are listed in unresolved_exclusions and "
    "still filter by name, which can miss paraphrases."
)

_COOKIE_SESSION = "gml_session"
_COOKIE_INVITE = "gml_invite"


def default_store_path() -> Path:
    env_path = os.environ.get("GML_STORE_PATH")
    return Path(env_path) if env_path else REPO_ROOT / "data" / "store.json"


def default_photo_root() -> Path:
    """Disk for opaque photo bytes. The public path is always /data/{id}."""
    env_path = os.environ.get("GML_DATA_DIR", "").strip()
    if env_path:
        path = Path(env_path)
        path.mkdir(parents=True, exist_ok=True)
        return path
    preferred = Path("/data")
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        probe = preferred / ".gml-write-probe"
        probe.write_bytes(b"ok")
        probe.unlink()
        return preferred
    except OSError:
        fallback = REPO_ROOT / "data" / "photos"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def request_is_https(request: Request) -> bool:
    """True when the browser is on https, including Render's forwarded proto.

    A client can only upgrade the flag. An http forward does not downgrade
    an https URL.
    """
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if forwarded == "https":
        return True
    return request.url.scheme == "https"


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


class GuideRequest(BaseModel):
    intent: Literal["status", "candidates"] = "status"


class InterviewTurn(BaseModel):
    text: str | None = None
    exit: bool = False


class FeedbackTurn(BaseModel):
    id: Literal["fit", "next"]
    text: str = Field(min_length=1, max_length=400)


def create_app(
    store_path: str | Path | None = None,
    qloo: Any | None = None,
    *,
    session_ttl_seconds: int | None = None,
    max_sessions: int | None = None,
    spotify_client_id: str | None = None,
    spotify_factory: Any | None = None,
    session_secret: str | None = None,
    photo_dir: str | Path | None = None,
) -> FastAPI:
    """Build the app. Tests inject a store path, a Qloo double, and Spotify."""
    app = FastAPI(title="Tuck", version="0.3.0", description=APP_DESCRIPTION)
    store = JsonStore(store_path or default_store_path())
    accounts = JsonStore(store.path.with_name("accounts.json"))
    app.state.session_secret = resolve_session_secret(session_secret)
    app.state.accounts = accounts
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
    app.state.photo_dir = Path(photo_dir) if photo_dir is not None else default_photo_root()
    app.state.photo_dir.mkdir(parents=True, exist_ok=True)
    app.state.session_ttl_seconds = ttl
    app.state.max_sessions = session_cap

    def resolved_spotify_id() -> str | None:
        if spotify_client_id is not None:
            value = spotify_client_id
        else:
            value = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
            if not value:
                value = read_secret_file("SPOTIFY_CLIENT_ID")
        value = value.strip()
        return value or None

    def spotify_redirect(request: Request) -> str:
        configured = os.environ.get("SPOTIFY_REDIRECT_URI", "").strip()
        if not configured:
            configured = read_secret_file("SPOTIFY_REDIRECT_URI")
        if configured:
            return configured
        return str(request.base_url).rstrip("/") + "/spotify/callback"

    def make_spotify(redirect_uri: str) -> SpotifyAPI | None:
        if spotify_factory is not None:
            return spotify_factory(redirect_uri)
        client_id = resolved_spotify_id()
        if not client_id:
            return None
        return SpotifyAPI(client_id, redirect_uri)

    def get_qloo() -> Any:
        nonlocal qloo_client
        if qloo_client is None:
            qloo_client = QlooClient()
        return qloo_client

    def load_session(session_id: str, token: str | None, request: Request) -> dict[str, Any]:
        session = store.get(session_id)
        if session is None or domain.session_expired(session, ttl):
            if session is not None:
                discard_session_photos(session)
                store.delete(session_id)
            raise HTTPException(status_code=404, detail="session not found")
        if not domain.verify_invite(session, token):
            raise HTTPException(
                status_code=401,
                detail="invalid or missing invite token (X-Invite-Token header)",
            )
        owner = session.get("account_id")
        if owner:
            account = require_account(request)
            if account["id"] != owner:
                raise HTTPException(
                    status_code=403,
                    detail="That sitting belongs to another account.",
                )
        return session

    def require_account(request: Request) -> dict[str, Any]:
        if not app.state.session_secret:
            raise HTTPException(status_code=503, detail="Sign-in is not configured on this server.")
        parsed = read_cookie(request.cookies.get(ACCOUNT_COOKIE), app.state.session_secret)
        if not parsed:
            raise HTTPException(status_code=401, detail="Sign in before opening a session.")
        account_id, version = parsed
        account = accounts.get(account_id)
        if account is None or "password" not in account:
            raise HTTPException(status_code=401, detail="Sign in before opening a session.")
        if int(account.get("session_version") or 0) != version:
            raise HTTPException(status_code=401, detail="Sign in before opening a session.")
        return account

    def set_account_cookie(response: Response, request: Request, account: dict[str, Any]) -> None:
        response.set_cookie(
            ACCOUNT_COOKIE,
            issue_cookie(
                account["id"],
                app.state.session_secret,
                int(account.get("session_version") or 0),
            ),
            httponly=True,
            samesite="lax",
            secure=request_is_https(request),
            path="/",
            max_age=14 * 24 * 60 * 60,
        )

    def clear_named_cookie(response: Response, name: str) -> None:
        # Two Set-Cookie headers: one Secure, one not. Browsers match both.
        for secure in (False, True):
            response.delete_cookie(name, path="/", secure=secure, samesite="lax", httponly=True)

    def find_account_by_email(email: str) -> dict[str, Any] | None:
        # ponytail: scans the account file. Ceiling: hundreds of accounts.
        # Upgrade path: an email index beside the file.
        for account in accounts.read_all().values():
            if isinstance(account, dict) and account.get("email") == email:
                return account
        return None

    def save_account(account: dict[str, Any]) -> None:
        with accounts.session_scope(account["id"]):
            accounts.put(account)

    def attach_account(session: dict[str, Any], account: dict[str, Any] | None) -> None:
        session["account_id"] = account["id"] if account else None
        session["interview"] = fresh_interview()
        session["feedback"] = {}

    def interview_view(session: dict[str, Any]) -> dict[str, Any]:
        interview = session.get("interview") or fresh_interview()
        answers = dict(interview.get("answers") or {})
        exited = bool(interview.get("exited"))
        committed = bool(interview.get("committed"))
        probe = None if committed else next_probe(answers, exited)
        pending = pending_feedback(session.get("feedback")) if session.get("sealed") else None
        if session.get("sealed"):
            phase = "gift"
            asked = f"Listening for {session.get('caregiver_label') or 'them'}."
        elif session.get("previewed_at"):
            phase = "list"
            asked = "The preview is signed. Ask for the list when you are ready."
        elif committed:
            phase = "preview"
            asked = "I can build the preview from what you told me. Nothing is requested until you say so."
        elif probe:
            phase = "probe"
            asked = probe["ask"]
        else:
            phase = "preview"
            asked = "I can build the preview from what you told me. Nothing is requested until you say so."
        turns: list[dict[str, str]] = []
        for item in PROBES:
            text = answers.get(item["id"])
            if text is None:
                continue
            turns.append({"id": item["id"], "ask": item["ask"], "answer": str(text)})
        if session.get("sealed"):
            saved_feedback = session.get("feedback") or {}
            for item in FEEDBACK:
                text = str(saved_feedback.get(item["id"]) or "").strip()
                if text:
                    turns.append({"id": item["id"], "ask": item["ask"], "answer": text})
        if phase in ("probe", "preview") and turns:
            heard = acknowledge(turns[-1]["answer"])
            if heard:
                asked = f"{heard} {asked}"
        attached = False
        paths = [interview.get("photo_path")]
        for upload in session.get("uploads") or []:
            if isinstance(upload, dict):
                paths.append(upload.get("photo_path"))
        for path in paths:
            href = str(path or "").strip()
            if href.startswith("/data/") and photo_provided(href):
                attached = True
                break
        return {
            "phase": phase,
            "asked": asked,
            "probe": (
                {"id": probe["id"], "topic": probe["topic"], "ask": probe["ask"]} if probe else None
            ),
            "can_exit": bool(probe) and can_exit(answers, exited) and not committed,
            "committed": committed,
            "answer_count": len(answers),
            "turns": turns,
            "photo_attached": attached,
            "caregiver_label": session.get("caregiver_label") or "",
            "feedback_id": pending["id"] if pending else None,
            "feedback_ask": pending["ask"] if pending else None,
        }

    def commit_interview(session: dict[str, Any]) -> None:
        interview = session.setdefault("interview", fresh_interview())
        if interview.get("committed"):
            return
        built = materialize(
            dict(interview.get("answers") or {}),
            photo_path=str(interview.get("photo_path") or NO_PHOTO),
        )
        if not built["anecdote"]:
            raise HTTPException(status_code=409, detail="I still need one moment in your words.")
        if not built["interests"]:
            raise HTTPException(
                status_code=409,
                detail="I still need a singer, a film, or something they like, in a few words.",
            )
        if "leave_out" not in (interview.get("answers") or {}):
            raise HTTPException(status_code=409, detail="Tell me what should stay out before the preview.")
        try:
            domain.require_open(session)
            upload = domain.add_upload(
                session,
                domain.UploadRequest(
                    photo_path=built["photo_path"],
                    anecdote=built["anecdote"],
                    interests=built["interests"],
                ),
            )
            media = interview.get("photo_type")
            if media:
                upload["photo_type"] = media
            exclusions = domain.acknowledge_exclusions(
                session,
                domain.ExclusionsRequest(
                    exclusions=[{"label": label, "kind": "topic"} for label in built["exclusions"]]
                ),
            )
        except domain.SessionError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        warning = resolve_saved_exclusions(exclusions, get_qloo)
        interview["committed"] = True
        interview["resolution_warning"] = warning
        account_id = session.get("account_id")
        if account_id:
            account = accounts.get(account_id)
            if account:
                write_commit(account, habits=built["habits"], exclusions=list(built["exclusions"]))
                save_account(account)

    def guarded_qloo_errors(exc: Exception) -> HTTPException:
        if isinstance(exc, QlooAuthError):
            return HTTPException(
                status_code=503,
                detail="I can't reach the recommendation service right now.",
            )
        if isinstance(exc, QlooUpstreamError):
            return HTTPException(status_code=502, detail=str(exc))
        return HTTPException(status_code=500, detail="unexpected Qloo client error")

    def require_music_export(session: dict[str, Any]) -> dict[str, Any]:
        try:
            domain.require_previewed(session)
        except domain.SessionError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        export = session.get("export") or {}
        payload = export.get("payload")
        if not session.get("sealed") or not isinstance(payload, dict):
            raise HTTPException(
                status_code=409,
                detail="Build the candidate list before a playlist. The preview has to be signed first.",
            )
        return payload

    def export_body(session: dict[str, Any]) -> dict[str, Any]:
        payload = require_music_export(session)
        exclusions = session.get("exclusions") or []
        saved = session.get("playlist") or {}
        if saved.get("tracks"):
            tracks = [dict(track) for track in saved["tracks"]]
            withheld = saved.get("withheld") or []
        else:
            tracks, withheld = assemble_tracks(payload.get("candidates") or [], exclusions, search=None)
            tracks = [dict(track) for track in tracks]
        why_by_name = {
            str(candidate.get("name") or "").casefold(): candidate.get("why")
            for candidate in payload.get("candidates") or []
            if candidate.get("name") and candidate.get("why")
        }
        exclusion_labels: list[str] = []
        seen_labels: set[str] = set()
        for item in exclusions:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            key = label.casefold()
            if not label or key in seen_labels:
                continue
            seen_labels.add(key)
            exclusion_labels.append(label)
        for track in tracks:
            artist = str(track.get("artist") or "").casefold()
            if artist in why_by_name and not track.get("why"):
                track["why"] = why_by_name[artist]
        label = session.get("caregiver_label") or ""
        playlist = None
        if saved.get("url"):
            playlist = {
                "id": saved.get("id"),
                "url": saved.get("url"),
                "public": False,
                "name": saved.get("name") or playlist_name(label),
            }
        return {
            "session_id": session["id"],
            "caregiver_label": label,
            "name": playlist_name(label),
            "description": playlist_description(label),
            "spotify_configured": resolved_spotify_id() is not None,
            "playlist": playlist,
            "tracks": tracks,
            "withheld": withheld,
            "exclusions": exclusion_labels,
            "candidates": payload.get("candidates") or [],
            "removed_by_exclusions": payload.get("removed_by_exclusions") or [],
            "boundary": payload.get("boundary"),
            "guide": guide_status(session),
        }

    def set_oauth_cookies(response: Response, request: Request, session_id: str, token: str) -> None:
        common = {
            "httponly": True,
            "samesite": "lax",
            "secure": request_is_https(request),
            "path": "/",
            "max_age": OAUTH_MAX_AGE_SECONDS,
        }
        response.set_cookie(_COOKIE_SESSION, session_id, **common)
        response.set_cookie(_COOKIE_INVITE, token, **common)

    def set_photo_cookies(response: Response, request: Request, session_id: str, token: str) -> None:
        common = {
            "httponly": True,
            "samesite": "lax",
            "secure": request_is_https(request),
            "path": "/",
            "max_age": ttl,
        }
        response.set_cookie(_COOKIE_SESSION, session_id, **common)
        response.set_cookie(_COOKIE_INVITE, token, **common)

    def opaque_photo_hrefs(session: dict[str, Any]) -> list[str]:
        hrefs: list[str] = []
        interview = session.get("interview") or {}
        path = interview.get("photo_path")
        if isinstance(path, str) and path.startswith("/data/"):
            hrefs.append(path)
        interview["photo_path"] = ""
        interview["photo_type"] = ""
        for upload in session.get("uploads") or []:
            if not isinstance(upload, dict):
                continue
            stored = upload.get("photo_path")
            if isinstance(stored, str) and stored.startswith("/data/"):
                hrefs.append(stored)
        return hrefs

    def discard_session_photos(session: dict[str, Any]) -> None:
        for href in opaque_photo_hrefs(session):
            discard_opaque(app.state.photo_dir, href)

    def insert_session(session: dict[str, Any]) -> None:
        expired_rows = [
            (sid, existing)
            for sid, existing in store.read_all().items()
            if isinstance(existing, dict) and domain.session_expired(existing, ttl)
        ]
        inserted = store.insert_if_room(
            session,
            session_cap,
            {sid for sid, _existing in expired_rows},
        )
        for sid, row in expired_rows:
            # File lock is already released. Take the stripe so a photo read finishes first.
            with store.session_scope(sid):
                discard_session_photos(row)
        if not inserted:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"session limit reached ({session_cap}). Delete a session "
                    "or wait for unused sessions to expire."
                ),
            )

    def playlist_redirect(flag: str, code: str | None = None) -> RedirectResponse:
        query = {"playlist": flag}
        if code:
            query["code"] = code
        response = RedirectResponse("/?" + urlencode(query), status_code=303)
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        clear_named_cookie(response, _COOKIE_SESSION)
        clear_named_cookie(response, _COOKIE_INVITE)
        return response

    @app.middleware("http")
    async def private_family_routes(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        private = (
            path.startswith("/sessions")
            or path.startswith("/invites")
            or path.startswith("/spotify")
            or path.startswith("/accounts")
            or path.startswith("/login")
            or path.startswith("/me")
            or path.startswith("/dev")
            or path.startswith("/data")
        )
        if private:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    def home() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "index.html",
            media_type="text/html",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/sample-family")
    def sample_family() -> dict[str, Any]:
        return {
            "caregiver_label": SAMPLE["caregiver_label"],
            "photo_path": SAMPLE["photo_path"],
            "anecdote": SAMPLE["anecdote"],
            "interests": list(SAMPLE["interests"]),
            "exclusions": list(SAMPLE["exclusions"]),
            "note": (
                "Sample family for the judge demo. The anecdote stays in the "
                "family record and is not a Qloo signal."
            ),
        }

    @app.post("/accounts", status_code=201)
    def register(body: RegisterRequest, request: Request) -> JSONResponse:
        if not app.state.session_secret:
            raise HTTPException(status_code=503, detail="Sign-in is not configured on this server.")
        if locked_for(body.email):
            raise HTTPException(status_code=429, detail="Too many tries. Wait a little, then try again.")
        with accounts.session_scope(body.email):
            if find_account_by_email(body.email):
                raise HTTPException(
                    status_code=409,
                    detail="That email already has an account. Say you already have one.",
                )
            account = new_account(body.email, body.password)
            accounts.put(account)
        clear_failures(body.email)
        response = JSONResponse(public_account(account), status_code=201)
        set_account_cookie(response, request, account)
        return response

    @app.post("/login")
    def login(body: LoginRequest, request: Request) -> JSONResponse:
        if not app.state.session_secret:
            raise HTTPException(status_code=503, detail="Sign-in is not configured on this server.")
        if locked_for(body.email):
            raise HTTPException(status_code=429, detail="Too many tries. Wait a little, then try again.")
        account = find_account_by_email(body.email)
        if account is None or not verify_password(body.password, account.get("password")):
            # Spend a hash when the email is missing so the two misses take similar time.
            if account is None:
                verify_password(body.password, dummy_password())
            note_failure(body.email)
            raise HTTPException(status_code=401, detail="That email or password does not match.")
        clear_failures(body.email)
        response = JSONResponse(public_account(account))
        set_account_cookie(response, request, account)
        return response

    @app.post("/logout")
    def logout(request: Request) -> JSONResponse:
        if app.state.session_secret:
            parsed = read_cookie(request.cookies.get(ACCOUNT_COOKIE), app.state.session_secret)
            if parsed:
                account_id, version = parsed
                with accounts.session_scope(account_id):
                    current = accounts.get(account_id)
                    if current and int(current.get("session_version") or 0) == version:
                        current["session_version"] = version + 1
                        accounts.put(current)
        response = JSONResponse({"signed_out": True})
        clear_named_cookie(response, ACCOUNT_COOKIE)
        return response

    @app.get("/me")
    def me(request: Request) -> dict[str, Any]:
        account = require_account(request)
        return public_account(account)

    @app.post("/me/profile")
    def set_profile(body: ProfileNameRequest, request: Request) -> dict[str, Any]:
        account = require_account(request)
        with accounts.session_scope(account["id"]):
            current = accounts.get(account["id"]) or account
            write_person_name(current, body.person_name)
            accounts.put(current)
        return public_account(current)

    @app.post("/dev/sample", status_code=201)
    def dev_sample() -> dict[str, Any]:
        """Canned family for the advanced demo. No account. Stops before Qloo."""
        session, token = domain.new_invite(SAMPLE["caregiver_label"])
        attach_account(session, None)
        try:
            domain.add_upload(
                session,
                domain.UploadRequest(
                    photo_path=SAMPLE["photo_path"],
                    anecdote=SAMPLE["anecdote"],
                    interests=list(SAMPLE["interests"]),
                ),
            )
            domain.acknowledge_exclusions(
                session,
                domain.ExclusionsRequest(exclusions=list(SAMPLE["exclusions"])),
            )
        except domain.SessionError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        session["interview"]["committed"] = True
        session["interview"]["answers"] = {"leave_out": "war documentaries"}
        insert_session(session)
        return {
            "session_id": session["id"],
            "invite_token": token,
            "caregiver_label": session["caregiver_label"],
            "phase": "preview",
            "note": "Sample family. Nothing is requested until you ask for the preview.",
        }

    @app.post("/invites", status_code=201)
    def create_invite(body: domain.InviteRequest, request: Request) -> dict[str, Any]:
        account = require_account(request)
        session, token = domain.new_invite(body.caregiver_label)
        attach_account(session, account)
        insert_session(session)
        body = {
            "session_id": session["id"],
            "invite_token": token,
            "caregiver_label": session["caregiver_label"],
            "note": "This token is shown once. Keep it private; it authorizes this session.",
        }
        body["guide"] = guide_status(session)
        return body

    @app.get("/sessions/{session_id}/interview")
    def get_interview(
        session_id: str,
        request: Request,
        x_invite_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        with store.session_scope(session_id):
            session = load_session(session_id, x_invite_token, request)
            return interview_view(session)

    @app.post("/sessions/{session_id}/interview")
    def post_interview(
        session_id: str,
        turn: InterviewTurn,
        request: Request,
        x_invite_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        with store.session_scope(session_id):
            session = load_session(session_id, x_invite_token, request)
            try:
                domain.require_open(session)
            except domain.SessionError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            interview = session.setdefault("interview", fresh_interview())
            if interview.get("committed"):
                raise HTTPException(status_code=409, detail="The preview is already waiting.")
            answers = dict(interview.get("answers") or {})
            exited = bool(interview.get("exited"))
            if turn.exit:
                if not can_exit(answers, exited):
                    raise HTTPException(
                        status_code=409,
                        detail="Stay for a few more questions. I can stop once the picture is clear.",
                    )
                interview["exited"] = True
                exited = True
            else:
                probe = next_probe(answers, exited)
                if probe is None:
                    raise HTTPException(status_code=409, detail="There is no question waiting.")
                try:
                    text = validate_answer(probe, turn.text or "")
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                answers[probe["id"]] = text
                interview["answers"] = answers
                account_id = session.get("account_id")
                if account_id:
                    account = accounts.get(account_id)
                    if account:
                        write_probe(
                            account,
                            {
                                "id": probe["id"],
                                "topic": probe["topic"],
                                "prompt": probe["ask"],
                                "answer": text,
                            },
                        )
                        save_account(account)
            if next_probe(dict(interview.get("answers") or {}), bool(interview.get("exited"))) is None:
                commit_interview(session)
            store.put(session)
            return interview_view(session)

    @app.post("/sessions/{session_id}/feedback")
    def post_feedback(
        session_id: str,
        turn: FeedbackTurn,
        request: Request,
        x_invite_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        with store.session_scope(session_id):
            session = load_session(session_id, x_invite_token, request)
            if not session.get("sealed"):
                raise HTTPException(status_code=409, detail="The list comes first.")
            text = " ".join(turn.text.split())
            if not text:
                raise HTTPException(status_code=422, detail="Say it in a few words.")
            waiting = pending_feedback(session.get("feedback"))
            if waiting is None:
                raise HTTPException(status_code=409, detail="Those notes are already saved.")
            if turn.id != waiting["id"]:
                raise HTTPException(status_code=409, detail="Answer the question I just asked.")
            saved = dict(session.get("feedback") or {})
            saved[turn.id] = text
            saved["at"] = domain.now_utc()
            session["feedback"] = saved
            store.put(session)
            account_id = session.get("account_id")
            if account_id:
                account = accounts.get(account_id)
                if account:
                    write_feedback(
                        account,
                        {
                            "session_id": session_id,
                            "fit": str(saved.get("fit") or ""),
                            "next": str(saved.get("next") or ""),
                            "at": str(saved.get("at") or ""),
                        },
                    )
                    save_account(account)
            pending = pending_feedback(saved)
            return {
                "saved": turn.id,
                "pending": pending["id"] if pending else None,
                "ask": pending["ask"] if pending else "Thank you. That is enough.",
            }

    @app.post("/sessions/{session_id}/uploads", status_code=201)
    def add_upload(
        session_id: str,
        request: domain.UploadRequest,
        http_request: Request,
        x_invite_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        with store.session_scope(session_id):
            session = load_session(session_id, x_invite_token, http_request)
            try:
                domain.require_open(session)
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
                "guide": guide_status(session),
            }

    @app.post("/sessions/{session_id}/data", status_code=201)
    def add_looking_photo(
        session_id: str,
        http_request: Request,
        response: Response,
        photo: UploadFile | None = File(default=None),
        anecdote: str = Form(default=""),
        x_invite_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        def ensure_looking_turn(session: dict[str, Any]) -> dict[str, Any]:
            try:
                domain.require_open(session)
            except domain.SessionError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            interview = session.setdefault("interview", fresh_interview())
            if interview.get("committed"):
                return interview
            answers = dict(interview.get("answers") or {})
            probe = next_probe(answers, bool(interview.get("exited")))
            if probe is None or probe["id"] != "moment":
                raise HTTPException(
                    status_code=409,
                    detail="Tell me the moment when I ask for it.",
                )
            return interview

        stored_href = ""
        previous = ""
        saved = False
        body: dict[str, Any] = {}
        try:
            # Token first, then the moment, then the bytes.
            with store.session_scope(session_id):
                load_session(session_id, x_invite_token, http_request)
            if photo is None:
                raise HTTPException(status_code=422, detail="Choose a photo to look at.")
            try:
                text = validate_answer(PROBES_BY_ID["moment"], anecdote)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            with store.session_scope(session_id):
                ensure_looking_turn(load_session(session_id, x_invite_token, http_request))
            try:
                media = normalize_media_type(photo.content_type, photo.filename)
                blob = read_photo_bytes(photo.file)
            except PhotoStoreError as exc:
                raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

            try:
                with store.session_scope(session_id):
                    session = load_session(session_id, x_invite_token, http_request)
                    interview = ensure_looking_turn(session)
                    pending_probe = None
                    if not interview.get("committed"):
                        answers = dict(interview.get("answers") or {})
                        exited = bool(interview.get("exited"))
                        probe = next_probe(answers, exited)
                        answers["moment"] = text
                        interview["answers"] = answers
                        if probe is not None:
                            pending_probe = probe
                    previous = str(interview.get("photo_path") or "")
                    try:
                        photo_id = allocate_photo_id(app.state.photo_dir)
                        href = f"/data/{photo_id}"
                        store_opaque(app.state.photo_dir, photo_id, blob)
                    except PhotoStoreError as exc:
                        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
                    stored_href = href
                    apply_looking_photo(session, href=href, media_type=media, anecdote=text)
                    if not interview.get("committed"):
                        if next_probe(
                            dict(interview.get("answers") or {}),
                            bool(interview.get("exited")),
                        ) is None:
                            commit_interview(session)
                    try:
                        store.put(session)
                    except (OSError, StoreError) as exc:
                        raise HTTPException(
                            status_code=503,
                            detail="I could not store that photo. Try again.",
                        ) from exc
                    saved = True
                    if pending_probe is not None:
                        account_id = session.get("account_id")
                        if account_id:
                            account = accounts.get(account_id)
                            if account:
                                write_probe(
                                    account,
                                    {
                                        "id": pending_probe["id"],
                                        "topic": pending_probe["topic"],
                                        "prompt": pending_probe["ask"],
                                        "answer": text,
                                    },
                                )
                                save_account(account)
                    body = interview_view(session)
                    body["photo_href"] = href
                    body["photo_on_file"] = True
                    body["anecdote_chars"] = len(text)
                    body["photo_note"] = (
                        "Stored as an opaque file. The photo is never opened or analyzed."
                    )
            except Exception:
                if stored_href and not saved:
                    discard_opaque(app.state.photo_dir, stored_href)
                    stored_href = ""
                raise
            if previous.startswith("/data/") and previous != body["photo_href"]:
                discard_opaque(app.state.photo_dir, previous)
            if x_invite_token:
                set_photo_cookies(response, http_request, session_id, x_invite_token)
            return body
        finally:
            closer = getattr(photo, "file", None)
            if closer is not None:
                try:
                    closer.close()
                except Exception:
                    pass

    @app.get("/data/{photo_id}")
    def serve_looking_photo(
        photo_id: str,
        request: Request,
        x_invite_token: str | None = Header(default=None),
    ) -> Response:
        if not PHOTO_ID_RE.fullmatch(photo_id):
            raise HTTPException(status_code=404, detail="That photo is not on file.")
        href = f"/data/{photo_id}"
        token = x_invite_token or request.cookies.get(_COOKIE_INVITE)
        owner_id = ""
        media = ""
        blob = b""
        # ponytail: scans the session file for the photo id, then reads the
        # bytes under that session's stripe so a seal cannot unlink mid-read.
        # Ceiling: GML_MAX_SESSIONS and 8 MB in memory. Upgrade path: a photo-id index.
        for sid, raw in store.read_all().items():
            if isinstance(raw, dict) and session_photo_media(raw, href):
                owner_id = sid
                break
        if not owner_id:
            raise HTTPException(status_code=404, detail="That photo is not on file.")
        with store.session_scope(owner_id):
            session = load_session(owner_id, token, request)
            media = session_photo_media(session, href)
            if not media:
                raise HTTPException(status_code=404, detail="That photo is not on file.")
            path = (app.state.photo_dir / photo_id).resolve()
            root = app.state.photo_dir.resolve()
            if path.parent != root or not path.is_file():
                raise HTTPException(status_code=404, detail="That photo is not on file.")
            try:
                blob = path.read_bytes()
            except OSError as exc:
                raise HTTPException(status_code=404, detail="That photo is not on file.") from exc
        return Response(
            content=blob,
            media_type=media,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Disposition": "inline",
                "Content-Security-Policy": "default-src 'none'",
                "Cross-Origin-Resource-Policy": "same-origin",
            },
        )

    @app.post("/sessions/{session_id}/exclusions")
    def set_exclusions(
        session_id: str,
        request: domain.ExclusionsRequest,
        http_request: Request,
        x_invite_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        with store.session_scope(session_id):
            session = load_session(session_id, x_invite_token, http_request)
            try:
                domain.require_open(session)
            except domain.SessionError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            exclusions = domain.acknowledge_exclusions(session, request)
            warning = resolve_saved_exclusions(exclusions, get_qloo)
            store.put(session)
            body: dict[str, Any] = {
                "acknowledged": True,
                "acknowledged_at": session["exclusions_acknowledged_at"],
                "exclusions": exclusions,
                "unresolved_exclusions": domain.unresolved_exclusion_labels(exclusions),
                "note": _EXCLUSION_NOTE,
                "guide": guide_status(session),
            }
            if warning:
                body["resolution_warning"] = warning
            return body


    def collect_interests_fallback(session: dict[str, Any]) -> list[str]:
        return list(domain.collect_interests(session))

    @app.post("/sessions/{session_id}/preview")
    def preview(
        session_id: str,
        request: Request,
        response: Response,
        x_invite_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        with store.session_scope(session_id):
            session = load_session(session_id, x_invite_token, request)
            try:
                domain.require_open(session)
                signals, unresolved, trace = prepare_preview(session, get_qloo())
            except domain.SessionError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            except (QlooAuthError, QlooUpstreamError) as exc:
                raise guarded_qloo_errors(exc) from exc
            kept, withheld = domain.split_signals(signals, session["exclusions"] or [])
            session["resolved_signals"] = kept
            session["unresolved_interests"] = unresolved
            session["agent_trace"] = trace
            session["previewed_at"] = domain.now_utc()
            store.put(session)
            interview = session.get("interview") or {}
            built = materialize(dict(interview.get("answers") or {}))
            persona = persona_summary(
                interests=built.get("interests") or collect_interests_fallback(session),
                habits=built.get("habits") or [],
                anecdote=built.get("anecdote")
                or next((u.get("anecdote") for u in session.get("uploads") or [] if u.get("anecdote")), ""),
                exclusions=[
                    str(item.get("label") or "")
                    for item in (session.get("exclusions") or [])
                    if isinstance(item, dict) and item.get("label")
                ],
                caregiver_label=session.get("caregiver_label") or "",
            )
            body = domain.preview_payload(
                session,
                kept_signals=kept,
                withheld=withheld,
                unresolved=unresolved,
                persona=persona,
            )
            body["guide"] = guide_status(session)
            body["guide"]["tools"] = trace["tools"]
            body["guide"]["input"] = trace["input"]
            if x_invite_token and body.get("looking"):
                set_photo_cookies(response, request, session_id, x_invite_token)
            return body

    @app.post("/sessions/{session_id}/guide")
    def guide(
        session_id: str,
        request: GuideRequest,
        http_request: Request,
        x_invite_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        with store.session_scope(session_id):
            session = load_session(session_id, x_invite_token, http_request)
            view = guide_status(session)
            if request.intent == "status":
                return view
            try:
                assert_candidates(session)
            except domain.SessionError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            if session.get("sealed"):
                return {**view, "allowed": False, "input": interest_payload(session)}
            return {
                **view,
                "allowed": True,
                "input": interest_payload(session),
            }

    @app.post("/sessions/{session_id}/candidates")
    def candidates(
        session_id: str,
        request: Request,
        x_invite_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        with store.session_scope(session_id):
            session = load_session(session_id, x_invite_token, request)
            if session.get("sealed"):
                payload = (session.get("export") or {}).get("payload")
                if not isinstance(payload, dict):
                    raise HTTPException(status_code=409, detail="This session is sealed and has no stored list.")
                return payload
            try:
                assert_candidates(session)
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
                    detail="I could not finish the list. The preview is still here. Try again in a little while.",
                )
            kept, removed = domain.filter_candidates(collected, session["exclusions"] or [])
            for candidate in kept:
                candidate["why"] = domain.candidate_reason(candidate["name"], kept_signals)
            payload = domain.candidates_payload(session, kept, removed, types_failed)
            photo_hrefs = opaque_photo_hrefs(session)
            domain.seal_for_export(session, payload)
            payload["guide"] = guide_status(session)
            store.put(session)
            for href in photo_hrefs:
                discard_opaque(app.state.photo_dir, href)
        return payload

    @app.get("/sessions/{session_id}/export")
    def export_session(
        session_id: str,
        request: Request,
        x_invite_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        with store.session_scope(session_id):
            session = load_session(session_id, x_invite_token, request)
            return export_body(session)

    @app.get("/sessions/{session_id}/playlist.m3u")
    def playlist_m3u(
        session_id: str,
        request: Request,
        x_invite_token: str | None = Header(default=None),
    ) -> Response:
        with store.session_scope(session_id):
            session = load_session(session_id, x_invite_token, request)
            body = export_body(session)
            m3u = render_m3u(body["name"], body["tracks"])
        return Response(
            content=m3u,
            media_type="audio/x-mpegurl",
            headers={
                "Content-Disposition": 'attachment; filename="tuck-listening.m3u"',
                "Cache-Control": "no-store",
            },
        )

    @app.post("/sessions/{session_id}/spotify/start")
    def spotify_start(
        session_id: str,
        request: Request,
        x_invite_token: str | None = Header(default=None),
    ) -> JSONResponse:
        redirect_uri = spotify_redirect(request)
        with store.session_scope(session_id):
            session = load_session(session_id, x_invite_token, request)
            require_music_export(session)
            client_id = resolved_spotify_id()
            api = make_spotify(redirect_uri)
            if not client_id or api is None:
                if api is not None:
                    api.close()
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": (
                            "A private Spotify playlist is not connected yet. "
                            "Download the listening file, or open the search links."
                        ),
                        "fallback": "m3u",
                    },
                )
            try:
                verifier, challenge = pkce_pair()
                state = secrets.token_urlsafe(24)
                session["spotify_oauth"] = {
                    "state": state,
                    "verifier": verifier,
                    "created_at": domain.now_utc(),
                    "redirect_uri": redirect_uri,
                }
                store.put(session)
                url = authorize_url(client_id, redirect_uri, state, challenge)
            finally:
                api.close()
        response = JSONResponse({"authorize_url": url})
        set_oauth_cookies(response, request, session_id, x_invite_token or "")
        return response

    @app.get("/spotify/callback", name="spotify_callback")
    def spotify_callback(request: Request) -> RedirectResponse:
        code = (request.query_params.get("code") or "").strip()
        state = (request.query_params.get("state") or "").strip()
        session_id = request.cookies.get(_COOKIE_SESSION, "")
        token = request.cookies.get(_COOKIE_INVITE, "")
        access_token = ""
        hidden: list[str] = []

        def log_failure(err_code: str, detail: str) -> str:
            safe = detail
            for secret in (code, access_token, token, *hidden):
                if secret:
                    safe = safe.replace(secret, "[REDACTED]")
            safe = safe[:180]
            _spotify_log.warning(
                "spotify callback failed session=%s err=%s detail=%s",
                session_id or "-",
                err_code,
                safe,
            )
            return safe

        def fail(err_code: str, detail: str) -> RedirectResponse:
            """Log scrubbed detail, persist playlist_error, redirect.

            Do not call this while session_scope is held: the stripe lock is not reentrant.
            """
            safe = log_failure(err_code, detail)
            if session_id:
                try:
                    with store.session_scope(session_id):
                        stored = store.get(session_id)
                        if stored is not None:
                            stored["playlist_error"] = {"code": err_code, "detail": safe}
                            store.put(stored)
                except Exception:
                    _spotify_log.warning(
                        "spotify callback failed session=%s err=%s detail=%s",
                        session_id,
                        err_code,
                        "playlist_error was not saved",
                    )
            return playlist_redirect("error", err_code)

        spotify_error = (request.query_params.get("error") or "").strip()
        if spotify_error:
            if len(spotify_error) > 40 or not spotify_error.replace("-", "").replace("_", "").isalnum():
                spotify_error = "denied"
            return fail("denied", f"authorization error {spotify_error}")
        if not session_id or not token:
            return fail("cookie", "oauth cookies were missing on the return")
        if not code or not state:
            return fail("state", "authorization code or state was missing")

        # Verifier is single-use. Clear it before Spotify so a slow search
        # does not hold the session stripe, and a second callback cannot reuse it.
        job: dict[str, Any] | None = None
        with store.session_scope(session_id):
            try:
                session = load_session(session_id, token, request)
            except HTTPException as exc:
                if exc.status_code == 404:
                    flag = "session"
                elif exc.status_code in (401, 403):
                    flag = "cookie"
                else:
                    flag = "invite"
                safe = log_failure(flag, f"session load failed status={exc.status_code}")
                if exc.status_code != 404:
                    stored = store.get(session_id)
                    if stored is not None:
                        stored["playlist_error"] = {"code": flag, "detail": safe}
                        store.put(stored)
                return playlist_redirect("error", flag)

            def fail_locked(err_code: str, detail: str, *, clear_oauth: bool = False) -> RedirectResponse:
                if clear_oauth:
                    session["spotify_oauth"] = None
                session["playlist_error"] = {
                    "code": err_code,
                    "detail": log_failure(err_code, detail),
                }
                store.put(session)
                return playlist_redirect("error", err_code)

            oauth = session.get("spotify_oauth") or {}
            if not oauth or oauth.get("state") != state:
                return fail_locked("state", "oauth state did not match this sitting")
            if oauth_expired(str(oauth.get("created_at") or "")):
                return fail_locked("stale", "oauth step expired", clear_oauth=True)
            try:
                payload = require_music_export(session)
            except HTTPException:
                return fail_locked("state", "candidate list is not ready", clear_oauth=True)
            verifier = str(oauth.get("verifier") or "")
            if verifier:
                hidden.append(verifier)
            job = {
                "verifier": verifier,
                "redirect_uri": str(oauth.get("redirect_uri") or spotify_redirect(request)),
                "candidates": list(payload.get("candidates") or []),
                "exclusions": list(session.get("exclusions") or []),
                "label": session.get("caregiver_label") or "",
            }
            session["spotify_oauth"] = None
            store.put(session)

        if job is None:
            return fail("state", "oauth job was empty")

        created: dict[str, Any] | None = None
        tracks: list[dict[str, Any]] = []
        withheld: list[dict[str, Any]] = []
        api = make_spotify(job["redirect_uri"])
        if api is None:
            return fail("upstream", "spotify client id missing at callback time")
        try:
            access_token = api.exchange_code(code, job["verifier"])
        except SpotifyError as exc:
            api.close()
            return fail("token", str(exc))
        except httpx.HTTPError as exc:
            api.close()
            return fail("upstream", f"{type(exc).__name__} during token exchange")
        try:
            market = api.resolve_market(access_token)

            def search(artist: str) -> list[dict[str, Any]]:
                return api.search_artist_tracks(access_token, artist, market=market)

            tracks, withheld = assemble_tracks(
                job["candidates"],
                job["exclusions"],
                search=search,
            )
            uris = uris_to_add(tracks, job["exclusions"])
            if not uris:
                search_errors = [
                    item
                    for item in withheld
                    if item.get("field") == "search" and item.get("reason") == "error"
                ]
                if search_errors:
                    # Spotify rejected the lookups. Detail leads so the 180-char
                    # scrub keeps the API message instead of the artist list.
                    first_detail = str(search_errors[0].get("detail") or "search failed")
                    return fail(
                        "search",
                        f"{len(search_errors)} artist lookups failed: {first_detail}",
                    )
                music_count = sum(
                    1
                    for candidate in job["candidates"]
                    if candidate.get("type") == "music" and str(candidate.get("name") or "").strip()
                )
                withheld_bits: list[str] = []
                for item in withheld:
                    field = str(item.get("field") or "")
                    reason = str(item.get("reason") or "")
                    withheld_bits.append(f"{field}/{reason}" if reason else field)
                summary = ", ".join(withheld_bits)[:100]
                return fail(
                    "empty",
                    f"no playable track from {music_count} music candidates ({summary or 'none withheld'})",
                )
            created = api.create_private_playlist(
                access_token,
                playlist_name(job["label"]),
                playlist_description(job["label"]),
            )
            api.add_tracks(access_token, created["id"], uris)
        except SpotifyError as exc:
            if created is not None and access_token:
                try:
                    api.discard_playlist(access_token, str(created["id"]))
                except (SpotifyError, httpx.HTTPError):
                    pass
            detail = str(exc)
            for secret in (access_token, code, str(job.get("verifier") or "")):
                if secret:
                    detail = detail.replace(secret, "[REDACTED]")
            detail = detail[:180]
            folded = detail.casefold()
            err_code = (
                "token"
                if ("access token" in folded or "token response" in folded)
                else "upstream"
            )
            return fail(err_code, detail)
        except httpx.HTTPError as exc:
            if created is not None and access_token:
                try:
                    api.discard_playlist(access_token, str(created["id"]))
                except (SpotifyError, httpx.HTTPError):
                    pass
            return fail("upstream", f"{type(exc).__name__} during spotify call")
        finally:
            api.close()

        if created is None:
            return fail("upstream", "playlist create produced no result")
        with store.session_scope(session_id):
            try:
                session = load_session(session_id, token, request)
            except HTTPException as exc:
                if exc.status_code == 404:
                    flag = "session"
                elif exc.status_code in (401, 403):
                    flag = "cookie"
                else:
                    flag = "invite"
                session = store.get(session_id)
                if session is None:
                    log_failure(
                        flag,
                        f"playlist created but session reload failed status={exc.status_code}",
                    )
                    return playlist_redirect("error", flag)
                log_failure(
                    flag,
                    f"session reload status={exc.status_code}; kept the playlist on the stored row",
                )
            session["playlist"] = {
                "id": created["id"],
                "url": created["url"],
                "public": False,
                "name": created["name"],
                "tracks": tracks,
                "withheld": withheld,
            }
            session["playlist_error"] = None
            store.put(session)
        return playlist_redirect("ready")

    @app.delete("/sessions/{session_id}", status_code=204)
    def delete_session(
        session_id: str,
        request: Request,
        x_invite_token: str | None = Header(default=None),
    ) -> Response:
        with store.session_scope(session_id):
            session = load_session(session_id, x_invite_token, request)
            store.delete(session_id)
            discard_session_photos(session)
        return Response(status_code=204)

    @app.exception_handler(StoreError)
    def store_error_handler(_: Any, exc: StoreError) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app


app = create_app()
