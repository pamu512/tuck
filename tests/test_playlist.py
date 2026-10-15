"""Playlist export re-checks exclusions. Spotify is private, or the M3U stands in."""

import base64
import hashlib
import json

import httpx
from fastapi.testclient import TestClient

from app.main import create_app
from app.playlist import SpotifyAPI, assemble_tracks, choose_track, pkce_pair
from app.sample import SAMPLE
from conftest import acknowledge, open_account, run_to_candidates, run_to_preview, start_session


def _reach_export(client, headers, session_id, exclusions):
    acknowledge(client, session_id, headers, exclusions)
    run_to_preview(client, session_id, headers)
    run_to_candidates(client, session_id, headers)


def test_pkce_challenge_is_s256_without_padding():
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert "=" not in challenge
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    assert challenge == expected


def test_resolved_track_artist_is_rechecked_before_it_is_kept():
    candidates = [{"type": "music", "name": "Miles Davis", "entity_id": "A-2"}]
    exclusions = [{"label": "Kind of Blue", "kind": "topic", "resolved_ids": []}]

    def search(_artist):
        return [
            {
                "uri": "spotify:track:blue",
                "title": "So What",
                "album": "Kind of Blue",
                "artists": ["Miles Davis"],
                "id": "blue",
            }
        ]

    kept, withheld = assemble_tracks(candidates, exclusions, search=search)
    assert kept == []
    assert withheld == [
        {"name": "Miles Davis", "matched_exclusion": "Kind of Blue", "field": "album"}
    ]


def test_featured_artist_exclusion_drops_the_track():
    exclusions = [{"label": "War Ensemble", "kind": "entity", "resolved_ids": []}]
    tracks = [
        {
            "uri": "spotify:track:bad",
            "title": "So What",
            "album": "Kind of Blue",
            "artists": ["Miles Davis", "War Ensemble"],
            "id": "bad",
        }
    ]
    chosen, skipped = choose_track(tracks, exclusions)
    assert chosen is None
    assert skipped[0]["field"] == "artist"
    assert skipped[0]["matched_exclusion"] == "War Ensemble"


def test_album_exclusion_drops_the_track_and_keeps_the_next():
    exclusions = [{"label": "war documentaries", "kind": "topic", "resolved_ids": []}]
    tracks = [
        {
            "uri": "spotify:track:bad",
            "title": "Newsreel",
            "album": "World War II Documentary Collection",
            "artists": ["Miles Davis"],
            "id": "bad",
        },
        {
            "uri": "spotify:track:good",
            "title": "So What",
            "album": "Kind of Blue",
            "artists": ["Miles Davis"],
            "id": "good",
        },
    ]
    chosen, skipped = choose_track(tracks, exclusions)
    assert chosen["uri"] == "spotify:track:good"
    assert skipped[0]["matched_exclusion"] == "war documentaries"
    assert skipped[0]["field"] == "album"


def test_m3u_keeps_music_and_drops_excluded_film(client):
    session_id, headers = start_session(client)
    _reach_export(
        client,
        headers,
        session_id,
        [{"label": "war documentaries", "kind": "topic", "reason": "veteran trauma"}],
    )
    response = client.get(f"/sessions/{session_id}/playlist.m3u", headers=headers)
    assert response.status_code == 200, response.text
    assert "audio/x-mpegurl" in response.headers["content-type"]
    body = response.text
    assert body.startswith("#EXTM3U\n")
    assert "https://open.spotify.com/search/Nat%20King%20Cole" in body
    assert "https://open.spotify.com/search/Miles%20Davis" in body
    assert "World War II Documentary Collection" not in body
    assert "Singin" not in body
    assert "New Orleans" not in body
    assert "Sunday" not in body
    assert "church" not in body.lower()

    export = client.get(f"/sessions/{session_id}/export", headers=headers)
    assert export.status_code == 200, export.text
    plan = export.json()
    assert plan["spotify_configured"] is False
    assert plan["playlist"] is None
    assert plan["description"] == "Shared listening for Test caregiver."
    assert plan["exclusions"] == ["war documentaries"]
    assert 'filename="tuck-listening.m3u"' in response.headers["content-disposition"]
    assert "Cochrane" not in plan["description"]
    assert "therapy" not in plan["description"].lower()
    names = [track["artist"] for track in plan["tracks"]]
    assert names == ["Nat King Cole", "Miles Davis"]

    start = client.post(f"/sessions/{session_id}/spotify/start", headers=headers)
    assert start.status_code == 409
    detail = start.json()["detail"]
    assert detail["fallback"] == "m3u"
    apology = str(detail).lower()
    assert "client id" not in apology
    assert "spotify_client_id" not in apology


def test_sample_family_playlist_omits_the_anecdote(client):
    open_account(client)
    invite = client.post("/invites", json={"caregiver_label": SAMPLE["caregiver_label"]})
    assert invite.status_code == 201, invite.text
    session_id = invite.json()["session_id"]
    headers = {"X-Invite-Token": invite.json()["invite_token"]}
    upload = client.post(
        f"/sessions/{session_id}/uploads",
        json={
            "photo_path": SAMPLE["photo_path"],
            "anecdote": SAMPLE["anecdote"],
            "interests": SAMPLE["interests"],
        },
        headers=headers,
    )
    assert upload.status_code == 201, upload.text
    _reach_export(client, headers, session_id, SAMPLE["exclusions"])
    export = client.get(f"/sessions/{session_id}/export", headers=headers)
    assert export.status_code == 200, export.text
    plan = export.json()
    blob = json.dumps({"description": plan["description"], "tracks": plan["tracks"]})
    assert "church" not in blob.lower()
    assert "lunch" not in blob.lower()
    assert "sunday-records" not in blob.lower()
    assert "World War II" not in blob
    assert plan["description"] == "Shared listening for Asha (daughter)."
    m3u = client.get(f"/sessions/{session_id}/playlist.m3u", headers=headers).text
    assert "https://open.spotify.com/search/Nat%20King%20Cole" in m3u
    assert "Documentary" not in m3u


def test_spotify_callback_creates_a_private_playlist_without_excluded_tracks(tmp_path, fake_qloo):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/token":
            body = request.content.decode()
            seen["token_body"] = body
            assert "client_secret" not in body
            return httpx.Response(200, json={"access_token": "spot-access-token", "token_type": "Bearer"})
        if request.url.path == "/v1/search":
            assert request.headers["Authorization"] == "Bearer spot-access-token"
            return httpx.Response(
                200,
                json={
                    "tracks": {
                        "items": [
                            {
                                "id": "bad",
                                "uri": "spotify:track:bad",
                                "name": "Newsreel",
                                "artists": [{"name": "Miles Davis"}],
                                "album": {"name": "World War II Documentary Collection"},
                            },
                            {
                                "id": "good",
                                "uri": "spotify:track:good",
                                "name": "So What",
                                "artists": [{"name": "Miles Davis"}],
                                "album": {"name": "Kind of Blue"},
                            },
                        ]
                    }
                },
            )
        if request.url.path == "/v1/me/playlists":
            payload = json.loads(request.content.decode())
            seen["create"] = payload
            return httpx.Response(
                201,
                json={
                    "id": "pl1",
                    "public": False,
                    "name": payload["name"],
                    "external_urls": {"spotify": "https://open.spotify.com/playlist/pl1"},
                },
            )
        if request.url.path == "/v1/playlists/pl1/items":
            seen["uris"] = json.loads(request.content.decode())["uris"]
            return httpx.Response(201, json={"snapshot_id": "snap"})
        return httpx.Response(404, json={"error": "missing"})

    def factory(redirect_uri: str) -> SpotifyAPI:
        return SpotifyAPI("test-client", redirect_uri, transport=httpx.MockTransport(handler))

    client = TestClient(
        create_app(
            store_path=tmp_path / "store.json",
            qloo=fake_qloo,
            spotify_client_id="test-client",
            spotify_factory=factory,
        )
    )
    session_id, headers = start_session(client)
    _reach_export(
        client,
        headers,
        session_id,
        [{"label": "war documentaries", "kind": "topic", "reason": "veteran trauma"}],
    )
    start = client.post(f"/sessions/{session_id}/spotify/start", headers=headers)
    assert start.status_code == 200, start.text
    authorize = start.json()["authorize_url"]
    assert authorize.startswith("https://accounts.spotify.com/authorize?")
    assert "playlist-modify-private" in authorize
    assert "playlist-modify-public" not in authorize
    assert "code_challenge_method=S256" in authorize

    state = client.app.state.store.get(session_id)["spotify_oauth"]["state"]
    denied = client.get(
        "/spotify/callback",
        params={"code": "nope", "state": "not-the-state"},
        follow_redirects=False,
    )
    assert denied.status_code == 303
    assert "code=state" in denied.headers["location"]
    assert "token_body" not in seen

    start = client.post(f"/sessions/{session_id}/spotify/start", headers=headers)
    assert start.status_code == 200, start.text
    state = client.app.state.store.get(session_id)["spotify_oauth"]["state"]
    callback = client.get(
        "/spotify/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert "playlist=ready" in callback.headers["location"]
    assert "spot-access-token" not in callback.headers["location"]
    assert "auth-code" not in callback.headers["location"]
    assert seen["create"]["public"] is False
    assert seen["create"]["name"].startswith("Tuck:")
    assert seen["create"]["description"] == "Shared listening for Test caregiver."
    assert "Sunday" not in seen["create"]["description"]
    assert "church" not in seen["create"]["description"].lower()
    assert seen["uris"] == ["spotify:track:good"]
    assert "spotify:track:bad" not in seen["uris"]

    stored = json.dumps(client.app.state.store.get(session_id))
    assert "spot-access-token" not in stored
    assert "auth-code" not in stored
    assert client.app.state.store.get(session_id)["spotify_oauth"] is None
    assert client.app.state.store.get(session_id)["playlist"]["public"] is False


def test_spotify_callback_redirects_when_the_list_is_not_ready(tmp_path, fake_qloo):
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"error": "should not be called"})

    def factory(redirect_uri: str) -> SpotifyAPI:
        return SpotifyAPI("test-client", redirect_uri, transport=httpx.MockTransport(handler))

    client = TestClient(
        create_app(
            store_path=tmp_path / "store.json",
            qloo=fake_qloo,
            spotify_client_id="test-client",
            spotify_factory=factory,
        )
    )
    session_id, headers = start_session(client)
    token = headers["X-Invite-Token"]
    session = client.app.state.store.get(session_id)
    session["spotify_oauth"] = {
        "state": "bound-state",
        "verifier": "verifier-value-verifier-value-verifier",
        "created_at": session["created_at"],
        "redirect_uri": "http://testserver/spotify/callback",
    }
    client.app.state.store.put(session)
    client.cookies.set("gml_session", session_id)
    client.cookies.set("gml_invite", token)
    response = client.get(
        "/spotify/callback",
        params={"code": "auth-code", "state": "bound-state"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert response.headers["location"].startswith("/?playlist=error")
    assert "auth-code" not in response.headers["location"]
    assert calls["n"] == 0
    assert client.app.state.store.get(session_id)["spotify_oauth"] is None


def test_a_failed_track_add_discards_the_empty_playlist(tmp_path, fake_qloo):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/token":
            return httpx.Response(200, json={"access_token": "spot-access-token"})
        if request.url.path == "/v1/search":
            return httpx.Response(
                200,
                json={
                    "tracks": {
                        "items": [
                            {
                                "id": "good",
                                "uri": "spotify:track:good",
                                "name": "So What",
                                "artists": [{"name": "Miles Davis"}],
                                "album": {"name": "Kind of Blue"},
                            }
                        ]
                    }
                },
            )
        if request.url.path == "/v1/me/playlists" and request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "id": "pl-empty",
                    "public": False,
                    "name": "Tuck: Test caregiver",
                    "external_urls": {"spotify": "https://open.spotify.com/playlist/pl-empty"},
                },
            )
        if request.url.path == "/v1/playlists/pl-empty/items":
            return httpx.Response(500, json={"error": {"message": "unavailable"}})
        if request.method == "DELETE" and request.url.path == "/v1/playlists/pl-empty/followers":
            seen["discarded"] = True
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error": "missing"})

    def factory(redirect_uri: str) -> SpotifyAPI:
        return SpotifyAPI("test-client", redirect_uri, transport=httpx.MockTransport(handler))

    client = TestClient(
        create_app(
            store_path=tmp_path / "store.json",
            qloo=fake_qloo,
            spotify_client_id="test-client",
            spotify_factory=factory,
        )
    )
    session_id, headers = start_session(client)
    _reach_export(client, headers, session_id, [])
    start = client.post(f"/sessions/{session_id}/spotify/start", headers=headers)
    assert start.status_code == 200, start.text
    state = client.app.state.store.get(session_id)["spotify_oauth"]["state"]
    callback = client.get(
        "/spotify/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert "playlist=error" in callback.headers["location"]
    assert "code=upstream" in callback.headers["location"]
    assert "spot-access-token" not in callback.headers["location"]
    assert seen.get("discarded") is True
    assert client.app.state.store.get(session_id).get("playlist") is None


def test_spotify_callback_accepts_create_response_without_public_field(tmp_path, fake_qloo):
    """Live Spotify may omit public on create; missing must not become upstream."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/token":
            return httpx.Response(200, json={"access_token": "spot-access-token"})
        if request.url.path == "/v1/search":
            return httpx.Response(
                200,
                json={
                    "tracks": {
                        "items": [
                            {
                                "id": "good",
                                "uri": "spotify:track:good",
                                "name": "So What",
                                "artists": [{"name": "Miles Davis"}],
                                "album": {"name": "Kind of Blue"},
                            }
                        ]
                    }
                },
            )
        if request.url.path == "/v1/me/playlists" and request.method == "POST":
            payload = json.loads(request.content.decode())
            seen["create"] = payload
            # Deliberately omit public — the live API often does.
            return httpx.Response(
                201,
                json={
                    "id": "pl-nopublic",
                    "name": payload["name"],
                    "external_urls": {"spotify": "https://open.spotify.com/playlist/pl-nopublic"},
                },
            )
        if request.url.path == "/v1/playlists/pl-nopublic/items":
            return httpx.Response(201, json={"snapshot_id": "snap"})
        return httpx.Response(404, json={"error": "missing"})

    def factory(redirect_uri: str) -> SpotifyAPI:
        return SpotifyAPI("test-client", redirect_uri, transport=httpx.MockTransport(handler))

    client = TestClient(
        create_app(
            store_path=tmp_path / "store.json",
            qloo=fake_qloo,
            spotify_client_id="test-client",
            spotify_factory=factory,
        )
    )
    session_id, headers = start_session(client)
    _reach_export(client, headers, session_id, [])
    start = client.post(f"/sessions/{session_id}/spotify/start", headers=headers)
    assert start.status_code == 200, start.text
    state = client.app.state.store.get(session_id)["spotify_oauth"]["state"]
    callback = client.get(
        "/spotify/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert "playlist=ready" in callback.headers["location"]
    assert "code=upstream" not in callback.headers["location"]
    assert seen["create"]["public"] is False
    stored = client.app.state.store.get(session_id)
    assert stored["playlist"]["id"] == "pl-nopublic"
    assert stored["playlist"]["public"] is False
