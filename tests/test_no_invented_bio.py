"""No invented biography: photos stay opaque, anecdotes are display-only,
Qloo biographical text is stripped, and the claim boundary travels with
every preview."""

import builtins
import json

import httpx
import pytest

from app.qloo import QlooClient
from conftest import acknowledge, run_to_preview, start_session

PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff")


def test_photo_file_is_never_opened(client, monkeypatch):
    real_open = builtins.open

    def sabotage(file, *args, **kwargs):
        if str(file).lower().endswith(PHOTO_SUFFIXES):
            raise AssertionError("photo file must never be opened")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", sabotage)
    session_id, headers = start_session(client, photo="/tmp/family/sunday-records.jpg")
    acknowledge(client, session_id, headers, [])
    preview = run_to_preview(client, session_id, headers)
    response = client.post(f"/sessions/{session_id}/candidates", headers=headers)
    assert response.status_code == 200
    assert preview["photo_count"] == 1


def test_signals_come_only_from_explicit_interests_not_anecdote_or_photo(client):
    biographic_anecdote = (
        "She was a night nurse in Liverpool in the 1960s, raised pigeons, "
        "and loved ferry trips across the Mersey."
    )
    session_id, headers = start_session(
        client,
        interests=[],
        anecdote=biographic_anecdote,
        photo="/tmp/family/ward-1965.png",
    )
    acknowledge(client, session_id, headers, [])
    preview = run_to_preview(client, session_id, headers)
    # The anecdote is echoed back for the family record (display-only), but
    # none of its content may leak into anything Qloo receives.
    outbound = json.dumps(
        [preview["signals"], preview["what_qloo_receives"], preview["explicit_interests"]]
    )
    for forbidden in ("Liverpool", "nurse", "pigeon", "Mersey"):
        assert forbidden not in outbound, forbidden
    # The photo path appears nowhere in the preview at all.
    assert "ward-1965" not in json.dumps(preview)
    assert preview["explicit_interests"] == []
    assert preview["signals"] == []
    # With no signals, candidates refuse rather than guess.
    response = client.post(f"/sessions/{session_id}/candidates", headers=headers)
    assert response.status_code == 409


def test_candidate_payload_has_no_biography_fields(client):
    session_id, headers = start_session(client)
    acknowledge(client, session_id, headers, [])
    run_to_preview(client, session_id, headers)
    response = client.post(f"/sessions/{session_id}/candidates", headers=headers)
    assert response.status_code == 200
    allowed_keys = {"name", "entity_id", "type", "image_url", "why"}
    for candidate in response.json()["candidates"]:
        assert set(candidate) <= allowed_keys
        # Explicitly: no biographical text fields survive from Qloo.
        assert "biography" not in candidate
        assert "description" not in candidate


def test_real_qloo_client_uses_get_and_strips_biography():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "success": True,
                "results": {
                    "entities": [
                        {
                            "entity_id": "X-1",
                            "name": "Test Artist",
                            "subtype": "urn:entity:artist",
                            "properties": {
                                "image": {"url": "https://images.qloo.com/x.jpg"},
                                "biography": "Born in a small town in 1920 and raised by...",
                                "description": "Test Artist was a celebrated...",
                            },
                        }
                    ]
                },
            },
        )

    client = QlooClient(api_key="test-key", transport=httpx.MockTransport(handler))
    out = client.insights("urn:entity:artist", signal_tags=["urn:tag:genre:music:jazz"], take=5)
    client.close()

    # GET with query-string parameters, per the hackathon developer guide.
    assert seen["method"] == "GET"
    assert "filter.type=urn%3Aentity%3Aartist" in seen["url"] or "filter.type=urn:entity:artist" in seen["url"]
    assert seen["params"]["signal.interests.tags"] == "urn:tag:genre:music:jazz"
    assert seen["params"]["take"] == "5"
    # Only safe display fields survive the mapping.
    assert out == [
        {
            "entity_id": "X-1",
            "name": "Test Artist",
            "subtype": "urn:entity:artist",
            "image_url": "https://images.qloo.com/x.jpg",
        }
    ]



def test_insights_joins_multiple_tags_into_one_query_param():
    """Hackathon API 500s on repeated signal.interests.tags; comma-join required.

    Live check 2026-10-15 against hackathon.api.qloo.com: repeated tags params
    raise TypeError Cannot use 'in' operator to search for 'tag' in urn:tag:...
    while a single comma-joined value returns 200. Entities stay repeatable.
    """
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        # QueryParams.get_list preserves multiplicity; dict() collapses.
        seen["tag_values"] = list(request.url.params.get_list("signal.interests.tags"))
        seen["entity_values"] = list(request.url.params.get_list("signal.interests.entities"))
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={"success": True, "results": {"entities": []}},
        )

    client = QlooClient(api_key="test-key", transport=httpx.MockTransport(handler))
    client.insights(
        "urn:entity:artist",
        signal_tags=[
            "urn:tag:genre:music:jazz",
            "urn:tag:genre:music:blues",
        ],
        signal_entities=["E1", "E2"],
        take=3,
    )
    client.close()

    assert seen["method"] == "GET"
    # Exactly one tags param, comma-joined — not two repeated keys.
    assert seen["tag_values"] == ["urn:tag:genre:music:jazz,urn:tag:genre:music:blues"]
    assert seen["params"]["signal.interests.tags"] == (
        "urn:tag:genre:music:jazz,urn:tag:genre:music:blues"
    )
    # Entities remain repeatable (verified OK on hackathon API).
    assert seen["entity_values"] == ["E1", "E2"]
    assert seen["params"]["take"] == "3"


def test_loose_tag_matches_are_rejected_not_guessed(client, fake_qloo):
    """Encodes a live failure seen on 2026-10-13: the tag endpoint loosely
    matched the person-name interest "Nat King Cole" to an unrelated
    nearby-attraction tag. Only close name matches may become signals."""
    fake_qloo.tags["nat king cole"] = [
        {"id": "urn:tag:nearby_attraction:qloo:mlk_park", "name": "Martin Luther King Jr. National Historical Park"}
    ]
    session_id, headers = start_session(client)
    acknowledge(client, session_id, headers, [])
    preview = run_to_preview(client, session_id, headers)
    signals = {(signal["kind"], signal["name"]) for signal in preview["signals"]}
    assert ("entity", "Nat King Cole") in signals
    assert ("tag", "Jazz") in signals
    assert all("nearby_attraction" not in signal["id"] for signal in preview["signals"])
    assert "Martin Luther" not in json.dumps(preview["what_qloo_receives"])


def test_preview_carries_claim_boundary(client):
    session_id, headers = start_session(client)
    acknowledge(client, session_id, headers, [])
    preview = run_to_preview(client, session_id, headers)
    claims = preview["claims"]
    assert any("Cochrane" in item and "-0.23" in item for item in claims["will"])
    assert any("biography" in item.lower() for item in claims["wont"])
    assert "never sent" in preview["what_qloo_receives"]["note"]
    assert preview["disclaimer"]


def test_qloo_client_default_timeout_is_raised():
    """Live 2026-10-15: music/film/place all ReadTimeout at 15s. Default is now 40s."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": {"entities": []}})

    client = QlooClient(api_key="test-key", transport=httpx.MockTransport(handler))
    try:
        assert client._http.timeout.read == 40.0
    finally:
        client.close()


def test_insights_retries_read_timeout_with_backoff(monkeypatch):
    """ReadTimeout on GET /v2/insights backs off once, then succeeds."""
    attempts = {"n": 0}
    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("app.qloo.time.sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ReadTimeout("simulated read timeout", request=request)
        return httpx.Response(
            200,
            json={
                "results": {
                    "entities": [
                        {
                            "entity_id": "e1",
                            "name": "Retry Artist",
                            "subtype": "urn:entity:artist",
                            "properties": {},
                        }
                    ]
                }
            },
        )

    client = QlooClient(api_key="test-key", transport=httpx.MockTransport(handler))
    try:
        out = client.insights("urn:entity:artist", signal_tags=["urn:tag:genre:music:jazz"], take=3)
    finally:
        client.close()
    assert attempts["n"] == 2
    assert sleeps and sleeps[0] >= 1.0
    assert out[0]["name"] == "Retry Artist"
    assert "biography" not in out[0]


def test_insights_read_timeout_exhausted_raises_upstream(monkeypatch):
    monkeypatch.setattr("app.qloo.time.sleep", lambda _s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated read timeout", request=request)

    client = QlooClient(api_key="test-key", transport=httpx.MockTransport(handler))
    try:
        from app.qloo import QlooUpstreamError

        with pytest.raises(QlooUpstreamError) as excinfo:
            client.insights("urn:entity:artist", signal_tags=["urn:tag:genre:music:jazz"], take=3)
        assert "ReadTimeout" in str(excinfo.value)
        assert "test-key" not in str(excinfo.value)
    finally:
        client.close()
