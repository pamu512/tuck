"""Store transactions, session retention, and interest length."""

import threading
import time

from fastapi.testclient import TestClient

from app.main import create_app
from app.session import UploadRequest
from app.store import JsonStore
from conftest import acknowledge, run_to_candidates, run_to_preview, start_session


def test_interest_shorter_than_three_characters_is_rejected(client):
    session_id, headers = start_session(client)
    for too_short in ("a", "ab", "  x "):
        response = client.post(
            f"/sessions/{session_id}/uploads",
            json={
                "photo_path": "/tmp/family/another.jpg",
                "anecdote": "A second Sunday.",
                "interests": [too_short],
            },
            headers=headers,
        )
        assert response.status_code == 422, too_short
        assert "at least 3" in response.text
    response = client.post(
        f"/sessions/{session_id}/uploads",
        json={
            "photo_path": "/tmp/family/another.jpg",
            "anecdote": "A second Sunday.",
            "interests": ["pop"],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["interests"] == ["pop"]


def test_blank_interests_are_dropped_not_rejected():
    cleaned = UploadRequest(
        photo_path="/tmp/family/photo.jpg",
        anecdote="Sunday records.",
        interests=["", "   ", "jazz"],
    )
    assert cleaned.interests == ["jazz"]


def test_concurrent_session_updates_do_not_drop_writes(tmp_path):
    """12 threads each read-increment-write. Without the stripe lock every
    thread can read 0, and the file ends at 1."""
    store = JsonStore(tmp_path / "store.json")
    store.put({"id": "s_race", "n": 0})

    def bump() -> None:
        with store.session_scope("s_race"):
            current = store.get("s_race")
            assert current is not None
            time.sleep(0.01)
            current["n"] += 1
            store.put(current)

    threads = [threading.Thread(target=bump) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert store.get("s_race")["n"] == 12


def test_concurrent_puts_of_different_sessions_keep_both(tmp_path):
    store = JsonStore(tmp_path / "store.json")

    def write(session_id: str) -> None:
        for _ in range(20):
            with store.session_scope(session_id):
                current = store.get(session_id) or {"id": session_id, "n": 0}
                current["n"] += 1
                store.put(current)

    threads = [
        threading.Thread(target=write, args=(session_id,)) for session_id in ("s_a", "s_b")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert store.get("s_a")["n"] == 20
    assert store.get("s_b")["n"] == 20


def test_session_scope_serializes_one_id(tmp_path):
    store = JsonStore(tmp_path / "store.json")
    holders = {"current": 0, "max": 0}
    gate = threading.Lock()

    def hold() -> None:
        with store.session_scope("s_one"):
            with gate:
                holders["current"] += 1
                holders["max"] = max(holders["max"], holders["current"])
            time.sleep(0.02)
            with gate:
                holders["current"] -= 1

    threads = [threading.Thread(target=hold) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert holders["max"] == 1
    assert holders["current"] == 0


def test_delete_session_route(client):
    session_id, headers = start_session(client)
    response = client.delete(f"/sessions/{session_id}", headers=headers)
    assert response.status_code == 204
    response = client.post(f"/sessions/{session_id}/preview", headers=headers)
    assert response.status_code == 404


def test_candidates_delete_the_session(client):
    session_id, headers = start_session(client)
    acknowledge(client, session_id, headers, [])
    run_to_preview(client, session_id, headers)
    body = run_to_candidates(client, session_id, headers)
    assert body["candidates"]
    response = client.post(f"/sessions/{session_id}/candidates", headers=headers)
    assert response.status_code == 404
    assert client.app.state.store.get(session_id) is None


def test_all_insight_types_failing_keeps_the_session(client, fake_qloo):
    from app.qloo import QlooUpstreamError

    def boom(entity_type, signal_tags=(), signal_entities=(), take=6):
        raise QlooUpstreamError("simulated upstream timeout")

    fake_qloo.insights = boom
    session_id, headers = start_session(client)
    acknowledge(client, session_id, headers, [])
    run_to_preview(client, session_id, headers)
    response = client.post(f"/sessions/{session_id}/candidates", headers=headers)
    assert response.status_code == 502
    assert client.app.state.store.get(session_id) is not None


def test_failed_candidates_keep_the_session(client):
    session_id, headers = start_session(client, interests=[])
    acknowledge(client, session_id, headers, [])
    run_to_preview(client, session_id, headers)
    response = client.post(f"/sessions/{session_id}/candidates", headers=headers)
    assert response.status_code == 409
    assert client.app.state.store.get(session_id) is not None


def test_expired_session_is_removed_on_read(tmp_path, fake_qloo):
    client = TestClient(
        create_app(
            store_path=tmp_path / "store.json",
            qloo=fake_qloo,
            session_ttl_seconds=3600,
        )
    )
    session_id, headers = start_session(client)
    store = client.app.state.store
    with store.session_scope(session_id):
        session = store.get(session_id)
        session["created_at"] = "2000-01-01T00:00:00+00:00"
        store.put(session)
    response = client.post(f"/sessions/{session_id}/preview", headers=headers)
    assert response.status_code == 404
    assert store.get(session_id) is None


def test_session_cap_returns_429_and_expired_rows_do_not_count(tmp_path, fake_qloo):
    client = TestClient(
        create_app(
            store_path=tmp_path / "store.json",
            qloo=fake_qloo,
            session_ttl_seconds=3600,
            max_sessions=1,
        )
    )
    first = client.post("/invites", json={"caregiver_label": "A"})
    assert first.status_code == 201, first.text
    second = client.post("/invites", json={"caregiver_label": "B"})
    assert second.status_code == 429

    store = client.app.state.store
    session_id = first.json()["session_id"]
    with store.session_scope(session_id):
        session = store.get(session_id)
        session["created_at"] = "2000-01-01T00:00:00+00:00"
        store.put(session)
    third = client.post("/invites", json={"caregiver_label": "C"})
    assert third.status_code == 201, third.text
    assert store.get(session_id) is None
    assert store.count() == 1
