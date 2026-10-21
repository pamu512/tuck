"""Private Spotify playlist, or an M3U of search links when Spotify is unset.

Tracks are chosen from music candidates only. exclusion_matches runs again
on the artist, the track title, and the album before any uri is kept.
The description names the family label and nothing else.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote, urlencode

import httpx

from .session import exclusion_matches

SPOTIFY_AUTH = "https://accounts.spotify.com"
SPOTIFY_API = "https://api.spotify.com"
SPOTIFY_SCOPE = "playlist-modify-private"
OAUTH_MAX_AGE_SECONDS = 600

SearchFn = Callable[[str], list[dict[str, Any]]]


class SpotifyError(RuntimeError):
    """Spotify failed. The message must not carry tokens or the client secret."""


def pkce_pair() -> tuple[str, str]:
    """Verifier plus S256 challenge, both URL-safe and unpadded."""
    verifier = secrets.token_urlsafe(64)[:128]
    if len(verifier) < 43:
        verifier = (verifier + ("a" * 43))[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorize_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SPOTIFY_SCOPE,
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
    )
    return f"{SPOTIFY_AUTH}/authorize?{query}"


def search_url(query: str) -> str:
    return "https://open.spotify.com/search/" + quote(query.strip(), safe="")


def _one_line(label: str) -> str:
    return " ".join((label or "").split())


def playlist_name(label: str) -> str:
    return f"Tuck: {_one_line(label)}"[:100]


def playlist_description(label: str) -> str:
    """Family label only. No anecdote, photo, or outcome claim."""
    return f"Shared listening for {_one_line(label)}."[:300]


def oauth_expired(created_at: str, *, now: datetime | None = None) -> bool:
    try:
        stamp = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return (current - stamp).total_seconds() > OAUTH_MAX_AGE_SECONDS


def track_exclusion(track: dict[str, Any], exclusions: list[dict[str, Any]]) -> tuple[str, str] | None:
    """Return (label, field) when the title, an artist, or the album is excluded.

    The Qloo entity id is checked only against the artist. A Spotify track id
    is not an exclusion id.
    """
    checks: list[tuple[str, str | None]] = [
        ("title", track.get("title")),
        ("album", track.get("album")),
    ]
    artists = track.get("artists") or []
    if not artists and track.get("artist"):
        artists = [track["artist"]]
    for artist in artists:
        checks.append(("artist", artist))
    entity_id = track.get("source_entity_id")
    for field, value in checks:
        if not value:
            continue
        for exclusion in exclusions:
            matched = exclusion_matches(
                exclusion,
                name=str(value),
                entity_id=entity_id if field == "artist" else None,
            )
            if matched:
                return (exclusion.get("label") or "", field)
    return None


def choose_track(
    tracks: list[dict[str, Any]], exclusions: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """First track that clears title, artist, and album. Earlier misses are listed."""
    skipped: list[dict[str, str]] = []
    for track in tracks:
        hit = track_exclusion(track, exclusions)
        if hit:
            skipped.append(
                {
                    "name": track.get("title") or track.get("uri") or "",
                    "matched_exclusion": hit[0],
                    "field": hit[1],
                }
            )
            continue
        return track, skipped
    return None, skipped


def uris_to_add(tracks: list[dict[str, Any]], exclusions: list[dict[str, Any]]) -> list[str]:
    """Uris that still clear exclusions at the moment of the add call."""
    uris: list[str] = []
    seen: set[str] = set()
    for track in tracks:
        if track_exclusion(track, exclusions):
            continue
        uri = track.get("spotify_uri") or track.get("uri")
        if not uri or not str(uri).startswith("spotify:track:"):
            continue
        uri = str(uri)
        if uri in seen:
            continue
        seen.add(uri)
        uris.append(uri)
    return uris


def _link_row(candidate: dict[str, Any]) -> dict[str, Any]:
    name = candidate.get("name") or ""
    return {
        "artist": name,
        "artists": [name] if name else [],
        "title": None,
        "album": "",
        "spotify_uri": None,
        "search_url": search_url(name) if name else "",
        "source_entity_id": candidate.get("entity_id"),
    }


def _resolved_row(candidate: dict[str, Any], track: dict[str, Any]) -> dict[str, Any]:
    artists = list(track.get("artists") or [])
    artist = artists[0] if artists else (candidate.get("name") or "")
    title = track.get("title") or ""
    query = f"{artist} {title}".strip()
    return {
        "artist": artist,
        "artists": artists or ([artist] if artist else []),
        "title": title or None,
        "album": track.get("album") or "",
        "spotify_uri": track.get("uri"),
        "search_url": search_url(query) if query else "",
        "source_entity_id": candidate.get("entity_id"),
    }


def assemble_tracks(
    candidates: list[dict[str, Any]],
    exclusions: list[dict[str, Any]],
    search: SearchFn | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Music candidates only. Artist exclusions skip the search entirely.

    A withheld field of "search" carries a reason: "error" when Spotify
    rejected the lookup (detail holds the scrubbed message) or
    "no-results" when Spotify answered with nothing usable.
    """
    kept: list[dict[str, Any]] = []
    withheld: list[dict[str, str]] = []
    for candidate in candidates:
        if candidate.get("type") != "music":
            continue
        name = (candidate.get("name") or "").strip()
        if not name:
            continue
        blocked = None
        for exclusion in exclusions:
            if exclusion_matches(exclusion, name=name, entity_id=candidate.get("entity_id")):
                blocked = exclusion.get("label") or ""
                break
        if blocked is not None:
            withheld.append({"name": name, "matched_exclusion": blocked, "field": "artist"})
            continue
        if search is None:
            kept.append(_link_row(candidate))
            continue
        try:
            found = search(name) or []
        except SpotifyError as exc:
            withheld.append(
                {
                    "name": name,
                    "matched_exclusion": "",
                    "field": "search",
                    "reason": "error",
                    "detail": str(exc)[:180],
                }
            )
            continue
        chosen, skipped = choose_track(found, exclusions)
        if chosen is None:
            if skipped:
                withheld.append(
                    {
                        "name": name,
                        "matched_exclusion": skipped[0]["matched_exclusion"],
                        "field": skipped[0]["field"],
                    }
                )
            else:
                withheld.append(
                    {
                        "name": name,
                        "matched_exclusion": "",
                        "field": "search",
                        "reason": "no-results",
                    }
                )
            continue
        kept.append(_resolved_row(candidate, chosen))
    return kept, withheld


def render_m3u(name: str, tracks: list[dict[str, Any]]) -> str:
    lines = ["#EXTM3U", f"#PLAYLIST:{name}"]
    if not tracks:
        lines.append("# No music cleared the exclusions.")
    for track in tracks:
        title = track.get("title")
        artist = track.get("artist") or "Unknown"
        label = f"{artist} - {title}" if title else artist
        url = track.get("spotify_uri")
        if url and str(url).startswith("spotify:track:"):
            url = "https://open.spotify.com/track/" + str(url).rsplit(":", 1)[-1]
        else:
            url = track.get("search_url") or ""
        if not url:
            continue
        lines.append(f"#EXTINF:-1,{label}")
        lines.append(url)
    return "\n".join(lines) + "\n"


def _scrub(text: str, *secrets_to_hide: str) -> str:
    for secret in secrets_to_hide:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text[:180]


class SpotifyAPI:
    """Authorization Code + PKCE. No client secret is sent."""

    def __init__(
        self,
        client_id: str,
        redirect_uri: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        market: str | None = None,
    ) -> None:
        self.client_id = client_id.strip()
        self.redirect_uri = redirect_uri.strip()
        self._http = httpx.Client(timeout=timeout, transport=transport)
        # After Feb 2026, User.country may be absent from /me. Search without a
        # market (and without an account country) treats catalog as unavailable.
        env_market = (os.environ.get("SPOTIFY_MARKET") or "").strip().upper()
        chosen = (market or env_market or "US").strip().upper()
        self.default_market = chosen[:2] if len(chosen) >= 2 else "US"
        self._market: str | None = None

    def close(self) -> None:
        self._http.close()

    def _raise(self, response: httpx.Response, *secrets_to_hide: str) -> None:
        if response.status_code < 400:
            return
        detail = ""
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            err = body.get("error")
            description = body.get("error_description")
            if isinstance(err, dict):
                detail = str(err.get("message") or err.get("status") or "")
            elif isinstance(err, str):
                # OAuth token errors carry both a code and a description.
                detail = f"{err}: {description}" if description else err
            elif description:
                detail = str(description)
        if not detail:
            detail = f"HTTP {response.status_code}"
        raise SpotifyError(
            f"Spotify request failed: {_scrub(detail, self.client_id, *secrets_to_hide)}"
        )

    def exchange_code(self, code: str, verifier: str) -> str:
        response = self._http.post(
            f"{SPOTIFY_AUTH}/api/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": self.client_id,
                "code_verifier": verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self._raise(response, code, verifier)
        token = ""
        try:
            token = str((response.json() or {}).get("access_token") or "")
        except ValueError:
            token = ""
        if not token:
            raise SpotifyError("Spotify token response had no access token.")
        return token

    def resolve_market(self, token: str) -> str:
        """ISO market for search. Prefer /me country, else SPOTIFY_MARKET/US."""
        if self._market:
            return self._market
        try:
            response = self._http.get(
                f"{SPOTIFY_API}/v1/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code < 400:
                country = (response.json() or {}).get("country")
                if isinstance(country, str) and len(country.strip()) == 2:
                    self._market = country.strip().upper()
                    return self._market
        except (httpx.HTTPError, ValueError, TypeError):
            pass
        self._market = self.default_market
        return self._market

    def search_artist_tracks(
        self,
        token: str,
        artist: str,
        limit: int = 5,
        market: str | None = None,
    ) -> list[dict[str, Any]]:
        # ponytail: first page only. If all `limit` hits are excluded, the artist
        # is withheld. Upgrade path: follow Spotify paging until one track clears.
        safe_artist = artist.replace('"', "").strip()
        if limit < 1:
            limit = 1
        if limit > 10:
            limit = 10
        use_market = (market or self._market or self.default_market).strip().upper()[:2]
        if len(use_market) != 2:
            use_market = self.default_market
        params: dict[str, str] = {
            "q": f'artist:"{safe_artist}"',
            "type": "track",
            "limit": str(limit),
            "market": use_market,
        }
        response = self._http.get(
            f"{SPOTIFY_API}/v1/search",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        self._raise(response, token)
        items = ((response.json() or {}).get("tracks") or {}).get("items") or []
        tracks: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_spotify_track(item)
            if normalized:
                tracks.append(normalized)
        # Broader query when the quoted artist filter returns nothing.
        if not tracks and safe_artist:
            response = self._http.get(
                f"{SPOTIFY_API}/v1/search",
                params={
                    "q": safe_artist,
                    "type": "track",
                    "limit": str(limit),
                    "market": use_market,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            self._raise(response, token)
            items = ((response.json() or {}).get("tracks") or {}).get("items") or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                normalized = _normalize_spotify_track(item)
                if normalized and _artist_matches(safe_artist, normalized.get("artists") or []):
                    tracks.append(normalized)
        return tracks

    def create_private_playlist(self, token: str, name: str, description: str) -> dict[str, Any]:
        response = self._http.post(
            f"{SPOTIFY_API}/v1/me/playlists",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "name": name,
                "description": description,
                "public": False,
                "collaborative": False,
            },
        )
        self._raise(response, token)
        body = response.json() or {}
        playlist_id = body.get("id")
        url = ((body.get("external_urls") or {}).get("spotify")) or ""
        if not playlist_id or not url:
            raise SpotifyError("Spotify playlist response was missing an id or url.")
        # We requested public:false. Live Spotify sometimes omits the public
        # field on create; treat missing as OK. Only fail if it is explicitly true.
        if body.get("public") is True:
            raise SpotifyError("Spotify created a public playlist; expected private.")
        return {"id": str(playlist_id), "url": str(url), "public": False, "name": name}

    def add_tracks(self, token: str, playlist_id: str, uris: list[str]) -> None:
        if not uris:
            return
        response = self._http.post(
            f"{SPOTIFY_API}/v1/playlists/{playlist_id}/items",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"uris": uris},
        )
        self._raise(response, token, playlist_id)

    def discard_playlist(self, token: str, playlist_id: str) -> None:
        """Unfollow a playlist we created but could not fill. Owner unfollow deletes it."""
        response = self._http.delete(
            f"{SPOTIFY_API}/v1/playlists/{playlist_id}/followers",
            headers={"Authorization": f"Bearer {token}"},
        )
        self._raise(response, token, playlist_id)


def _artist_matches(want: str, names: list[Any]) -> bool:
    """Plain-query fallback only. A quoted artist filter already constrained the hit.

    ponytail: substring match once both sides are at least 4 characters.
    Ceiling: a short shared name can match two people. Upgrade path: Spotify artist id.
    """
    needle = want.casefold().strip()
    if len(needle) < 2:
        return False
    for name in names:
        hay = str(name or "").casefold().strip()
        if not hay:
            continue
        if needle == hay:
            return True
        if len(needle) >= 4 and len(hay) >= 4 and (needle in hay or hay in needle):
            return True
    return False


def _normalize_spotify_track(item: dict[str, Any]) -> dict[str, Any] | None:
    uri = str(item.get("uri") or "")
    track_id = str(item.get("id") or "")
    if not uri.startswith("spotify:track:") and track_id:
        uri = f"spotify:track:{track_id}"
    if not uri.startswith("spotify:track:"):
        return None
    artists: list[str] = []
    for artist in item.get("artists") or []:
        if isinstance(artist, dict) and artist.get("name"):
            artists.append(str(artist["name"]))
        elif isinstance(artist, str) and artist:
            artists.append(artist)
    album = item.get("album")
    album_name = ""
    if isinstance(album, dict):
        album_name = str(album.get("name") or "")
    elif isinstance(album, str):
        album_name = album
    title = str(item.get("name") or item.get("title") or "")
    if not title:
        return None
    return {
        "id": uri.rsplit(":", 1)[-1],
        "uri": uri,
        "title": title,
        "album": album_name,
        "artists": artists,
    }
