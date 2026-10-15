"""Shared fixtures: repo root on sys.path, a canned Qloo double, flow helpers."""

import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


class FakeQloo:
    """Canned Qloo double mirroring the docs-verified response shapes.

    Records every call so tests can prove exclusion gating and that
    withheld signals are never sent.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.tags = {"jazz": [{"id": "urn:tag:genre:music:jazz", "name": "Jazz"}]}
        self.entities = {
            "nat king cole": [
                {"entity_id": "E0CD9B0B-75BB-4DAE-BCB7-79AB5DE42A0D", "name": "Nat King Cole"}
            ]
        }
        self.catalog = {
            "urn:entity:artist": [
                {"entity_id": "A-1", "name": "Nat King Cole", "subtype": "urn:entity:artist", "image_url": None},
                {"entity_id": "A-2", "name": "Miles Davis", "subtype": "urn:entity:artist", "image_url": None},
            ],
            "urn:entity:movie": [
                {"entity_id": "M-1", "name": "World War II Documentary Collection", "subtype": "urn:entity:movie", "image_url": None},
                {"entity_id": "M-2", "name": "Singin' in the Rain", "subtype": "urn:entity:movie", "image_url": None},
            ],
            "urn:entity:place": [
                {"entity_id": "P-1", "name": "New Orleans Jazz Museum", "subtype": "urn:entity:place", "image_url": None},
            ],
        }

    def search_tags(self, query: str) -> list[dict]:
        self.calls.append(("tags", query))
        return self.tags.get(query.strip().casefold(), [])

    def search_entities(self, query: str) -> list[dict]:
        self.calls.append(("search", query))
        return self.entities.get(query.strip().casefold(), [])

    def insights(self, entity_type, signal_tags=(), signal_entities=(), take=6):
        self.calls.append(("insights", entity_type, tuple(signal_tags), tuple(signal_entities)))
        return list(self.catalog.get(entity_type, []))


@pytest.fixture
def fake_qloo() -> FakeQloo:
    return FakeQloo()


@pytest.fixture(autouse=True)
def tuck_session_secret(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "tuck-test-secret")


@pytest.fixture
def client(tmp_path, fake_qloo) -> TestClient:
    return TestClient(
        create_app(store_path=tmp_path / "store.json", qloo=fake_qloo, spotify_client_id="")
    )


def open_account(client: TestClient, email: str | None = None) -> str:
    """Register a caregiver. The session cookie stays on this client."""
    email = email or f"caregiver-{secrets.token_hex(4)}@example.com"
    response = client.post(
        "/accounts",
        json={"email": email, "password": "test-password-1"},
    )
    assert response.status_code == 201, response.text
    return email


def start_session(
    client: TestClient,
    *,
    interests=("jazz", "Nat King Cole"),
    anecdote="Sunday records after church.",
    photo="/tmp/family/photo.jpg",
) -> tuple[str, dict[str, str]]:
    """Account, invite, and one upload. Returns (session_id, auth headers)."""
    open_account(client)
    response = client.post("/invites", json={"caregiver_label": "Test caregiver"})
    assert response.status_code == 201, response.text
    invite = response.json()
    headers = {"X-Invite-Token": invite["invite_token"]}
    response = client.post(
        f"/sessions/{invite['session_id']}/uploads",
        json={"photo_path": photo, "anecdote": anecdote, "interests": list(interests)},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return invite["session_id"], headers


def acknowledge(client: TestClient, session_id: str, headers: dict, exclusions: list) -> dict:
    response = client.post(
        f"/sessions/{session_id}/exclusions", json={"exclusions": exclusions}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def run_to_preview(client: TestClient, session_id: str, headers: dict) -> dict:
    response = client.post(f"/sessions/{session_id}/preview", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def run_to_candidates(client: TestClient, session_id: str, headers: dict) -> dict:
    response = client.post(f"/sessions/{session_id}/candidates", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()
