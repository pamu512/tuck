"""Account first, then probes, then the existing preview gate."""

import json

from app.interview import can_exit, materialize, next_probe, persona_summary


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

    skipped = client.post(
        f"/sessions/{session_id}/feedback",
        json={"id": "next", "text": "Ask about Sunday sooner."},
        headers=headers,
    )
    assert skipped.status_code == 409

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


def test_sign_out_ends_the_cookie(client):
    from conftest import open_account

    open_account(client, "signed-out@example.com")
    assert client.get("/me").status_code == 200
    stolen = client.cookies.get("tuck_account")
    assert stolen
    signed_out = client.post("/logout")
    assert signed_out.status_code == 200
    assert "signed-out@example.com" not in signed_out.text
    assert client.get("/me").status_code == 401
    client.cookies.set("tuck_account", stolen)
    refused = client.get("/me")
    assert refused.status_code == 401
    assert "password" not in refused.text.lower()


def test_a_rewritten_cookie_is_refused(client):
    from conftest import open_account

    open_account(client, "tamper@example.com")
    client.cookies.set("tuck_account", "a_notreal.9999999999.0." + ("ab" * 32))
    refused = client.get("/me")
    assert refused.status_code == 401
    assert "tamper@example.com" not in refused.text


def test_repeated_misses_lock_the_account(client):
    from app.accounts import clear_failures

    email = "lock@example.com"
    clear_failures(email)
    created = client.post(
        "/accounts",
        json={"email": email, "password": "test-password-1"},
    )
    assert created.status_code == 201, created.text
    for _ in range(8):
        missed = client.post("/login", json={"email": email, "password": "wrong-password-1"})
        assert missed.status_code == 401, missed.text
        assert "test-password-1" not in missed.text
    locked = client.post("/login", json={"email": email, "password": "test-password-1"})
    assert locked.status_code == 429
    assert "test-password-1" not in locked.text
    clear_failures(email)


def test_the_account_cookie_is_secure_behind_https(client):
    plain = client.post(
        "/accounts",
        json={"email": "plain-cookie@example.com", "password": "test-password-1"},
    )
    assert plain.status_code == 201, plain.text
    plain_cookie = plain.headers["set-cookie"].lower()
    assert "httponly" in plain_cookie
    assert "samesite=lax" in plain_cookie
    assert "secure" not in plain_cookie
    proxied = client.post(
        "/accounts",
        json={"email": "proxied-cookie@example.com", "password": "test-password-1"},
        headers={"x-forwarded-proto": "https"},
    )
    assert proxied.status_code == 201, proxied.text
    proxied_cookie = proxied.headers["set-cookie"].lower()
    assert "secure" in proxied_cookie
    assert "httponly" in proxied_cookie
    assert "samesite=lax" in proxied_cookie


def test_a_sitting_stays_on_the_account_that_opened_it(client):
    from fastapi.testclient import TestClient

    from conftest import start_session

    session_id, headers = start_session(client)
    stranger = TestClient(client.app)
    created = stranger.post(
        "/accounts",
        json={"email": "stranger@example.com", "password": "test-password-1"},
    )
    assert created.status_code == 201, created.text
    denied = stranger.get(f"/sessions/{session_id}/interview", headers=headers)
    assert denied.status_code == 403
    bare = TestClient(client.app)
    missing = bare.get(f"/sessions/{session_id}/interview", headers=headers)
    assert missing.status_code == 401
    allowed = client.get(f"/sessions/{session_id}/interview", headers=headers)
    assert allowed.status_code == 200


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


def test_persona_summary_from_sample_answers():
    built = materialize(_short_answers())
    portrait = persona_summary(
        interests=built["interests"],
        habits=built["habits"],
        anecdote=built["anecdote"],
        exclusions=built["exclusions"],
        caregiver_label="Ada",
    )
    text = portrait["persona_summary"]
    assert "gardening" in text or "jazz" in text or "Nat King Cole" in text
    assert "war documentaries" in text
    assert "Sunday" in text
    assert "therapy" not in text.casefold()
    assert "photo" not in text.casefold() or "never" in portrait["qloo_plain"].casefold()
    assert portrait["facets"]["interests"]
    assert "music" in portrait["qloo_plain"].casefold()


def test_persona_summary_does_not_invent_from_empty():
    portrait = persona_summary(interests=[], habits=[], anecdote="", exclusions=[])
    assert "still need" in portrait["persona_summary"].casefold()
