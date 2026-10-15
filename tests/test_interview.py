"""Account first, then probes, then the existing preview gate."""

import json

from app.interview import can_exit, materialize, next_probe


def _short_answers():
    return {
        "hands": "gardening",
        "place": "the kitchen",
        "moment": "Sunday records after lunch.",
        "music": "jazz",
        "music_more": "Nat King Cole",
        "film": "musicals",
        "film_more": "Singin in the Rain",
        "when": "late morning",
        "span": "a few songs",
        "company": "her daughter",
        "leave_out": "war documentaries",
        "leave_more": "none",
    }


def test_full_path_asks_eight_to_twelve_probes():
    answers = {}
    asked = []
    samples = _short_answers()
    while True:
        probe = next_probe(answers, False)
        if probe is None:
            break
        asked.append(probe["id"])
        answers[probe["id"]] = samples[probe["id"]]
        assert len(asked) <= 12
    assert 8 <= len(asked) <= 12
    assert asked[0] == "hands"
    assert "moment" in asked and "music" in asked and "film" in asked
    assert "leave_out" in asked


def test_early_exit_still_asks_what_stays_out():
    answers = {}
    exited = False
    asked = []
    while True:
        probe = next_probe(answers, exited)
        if probe is None:
            break
        asked.append(probe["id"])
        answers[probe["id"]] = _short_answers()[probe["id"]]
        if can_exit(answers, exited):
            exited = True
    assert "leave_out" in asked
    assert "span" not in asked
    assert len(asked) < 12


def test_none_is_not_stored_as_an_interest_or_an_exclusion():
    built = materialize(
        {
            "moment": "Sunday records after lunch.",
            "hands": "gardening",
            "music": "jazz",
            "film": "musicals",
            "place": "none",
            "leave_out": "none",
        }
    )
    assert built["interests"] == ["gardening", "jazz", "musicals"]
    assert built["exclusions"] == []
    assert built["photo_path"] == "not-provided"
    assert "Sunday" in built["anecdote"]


def test_invites_require_an_account(client):
    response = client.post("/invites", json={"caregiver_label": "Ada"})
    assert response.status_code == 401
    assert "password" not in response.text.lower()


def test_interview_commits_without_insights_and_stores_feedback(client, fake_qloo):
    created = client.post(
        "/accounts",
        json={"email": "ada@example.com", "password": "test-password-1"},
    )
    assert created.status_code == 201, created.text
    assert "test-password-1" not in created.text
    assert created.json()["email"] == "ada@example.com"
    named = client.post("/me/profile", json={"person_name": "Ada"})
    assert named.status_code == 200, named.text
    assert named.json()["person_name"] == "Ada"

    invite = client.post("/invites", json={"caregiver_label": "Ada"})
    assert invite.status_code == 201, invite.text
    session_id = invite.json()["session_id"]
    headers = {"X-Invite-Token": invite.json()["invite_token"]}
    samples = _short_answers()
    guard = 0
    while True:
        guard += 1
        assert guard < 20
        view = client.get(f"/sessions/{session_id}/interview", headers=headers)
        assert view.status_code == 200, view.text
        body = view.json()
        if body["committed"]:
            break
        if body["can_exit"]:
            turned = client.post(
                f"/sessions/{session_id}/interview",
                json={"exit": True},
                headers=headers,
            )
        else:
            probe_id = body["probe"]["id"]
            turned = client.post(
                f"/sessions/{session_id}/interview",
                json={"text": samples[probe_id]},
                headers=headers,
            )
        assert turned.status_code == 200, turned.text
    assert not any(call[0] == "insights" for call in fake_qloo.calls)

    early = client.post(
        f"/sessions/{session_id}/feedback",
        json={"id": "fit", "text": "It felt like her."},
        headers=headers,
    )
    assert early.status_code == 409

    preview = client.post(f"/sessions/{session_id}/preview", headers=headers)
    assert preview.status_code == 200, preview.text
    assert "war documentaries" in json.dumps(preview.json())
    listed = client.post(f"/sessions/{session_id}/candidates", headers=headers)
    assert listed.status_code == 200, listed.text
    assert any(call[0] == "insights" for call in fake_qloo.calls)

    first = client.post(
        f"/sessions/{session_id}/feedback",
        json={"id": "fit", "text": "It felt like her."},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    assert first.json()["pending"] == "next"
    second = client.post(
        f"/sessions/{session_id}/feedback",
        json={"id": "next", "text": "Ask about Sunday sooner."},
        headers=headers,
    )
    assert second.status_code == 200, second.text
    assert second.json()["pending"] is None

    stored = json.dumps(client.app.state.accounts.read_all())
    assert "test-password-1" not in stored
    assert "It felt like her." in stored
    assert "gardening" in stored or "jazz" in stored
    session = client.app.state.store.get(session_id)
    assert session["feedback"]["fit"] == "It felt like her."
    assert session["feedback"]["next"] == "Ask about Sunday sooner."
    assert session["uploads"][0]["photo_path"] == ""


def test_sign_in_is_refused_when_unconfigured(tmp_path, fake_qloo):
    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(
        create_app(
            store_path=tmp_path / "store.json",
            qloo=fake_qloo,
            spotify_client_id="",
            session_secret="",
        )
    )
    response = client.post(
        "/accounts",
        json={"email": "ada@example.com", "password": "test-password-1"},
    )
    assert response.status_code == 503
    assert "SESSION_SECRET" not in response.text
    assert "tuck-spotify" not in response.text


def test_dev_sample_stops_before_the_list(client, fake_qloo):
    response = client.post("/dev/sample")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["phase"] == "preview"
    assert body["caregiver_label"] == "Asha (daughter)"
    headers = {"X-Invite-Token": body["invite_token"]}
    blocked = client.post(f"/sessions/{body['session_id']}/candidates", headers=headers)
    assert blocked.status_code == 409
    assert not any(call[0] == "insights" for call in fake_qloo.calls)
