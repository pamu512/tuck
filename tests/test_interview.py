"""Account first, then the chat: reply plus cards, ratings, preview gate."""

import json

from app.interview import (
    acknowledge,
    fresh_interview,
    materialize,
    next_slot,
    normalize_interview,
    persona_summary,
    preview_ready,
    soft_normalize_interest,
    store_interest_turn,
)
from app.session import candidate_reason, resolve_signals


def _open(client, email):
    created = client.post(
        "/accounts",
        json={"email": email, "password": "test-password-1"},
    )
    assert created.status_code == 201, created.text
    invite = client.post("/invites", json={"caregiver_label": "Ada"})
    assert invite.status_code == 201, invite.text
    body = invite.json()
    return body["session_id"], {"X-Invite-Token": body["invite_token"]}


def _get(client, session_id, headers):
    view = client.get(f"/sessions/{session_id}/interview", headers=headers)
    assert view.status_code == 200, view.text
    return view.json()


def _say(client, session_id, headers, text, **extra):
    turned = client.post(
        f"/sessions/{session_id}/interview",
        json={"text": text, **extra},
        headers=headers,
    )
    assert turned.status_code == 200, turned.text
    return turned.json()


def _drive_to_preview(client, session_id, headers, *, moment="Sunday records after lunch."):
    """Three chat turns: interest, moment, leave_out. Returns the committed view."""
    _say(client, session_id, headers, "jazz", lean="music")
    _say(client, session_id, headers, moment)
    return _say(client, session_id, headers, "war documentaries")


def test_preview_unlocks_on_interest_moment_and_leave_out():
    state = fresh_interview()
    assert next_slot(state) == "interest"
    assert not preview_ready(state)
    store_interest_turn(state, "jazz", "music", "t1")
    assert next_slot(state) == "moment"
    assert not preview_ready(state)
    state["moment"] = "Sunday records after lunch."
    assert next_slot(state) == "leave_out"
    assert not preview_ready(state)
    state["leave_out"] = "none"
    assert next_slot(state) is None
    assert preview_ready(state)


def test_none_leave_out_counts_as_answered_not_as_exclusion():
    state = fresh_interview()
    store_interest_turn(state, "jazz", "music", "t1")
    state["moment"] = "Sunday records after lunch."
    state["leave_out"] = "none"
    built = materialize(state)
    assert built["exclusions"] == []
    assert built["interests"] == ["jazz"]
    assert built["photo_path"] == "not-provided"
    assert "Sunday" in built["anecdote"]


def test_every_interview_response_carries_a_reply_and_cards(client):
    session_id, headers = _open(client, "cards@example.com")
    view = _get(client, session_id, headers)
    assert view["phase"] == "chat"
    assert view["slot"] == "interest"
    assert isinstance(view["reply"], str)
    assert 2 <= len(view["cards"]) <= 5
    assert view["rate_hint"]
    for card in view["cards"]:
        assert card["source"] == "local"
        assert card["card_id"]
        assert card["name"]
        assert card["why"]

    turned = _say(client, session_id, headers, "jazz", lean="music")
    assert turned["reply"].startswith("I'll keep")
    assert 2 <= len(turned["cards"]) <= 5
    assert turned["cards"][0]["phrase"] == "jazz"
    assert turned["slot"] == "moment"

    turned = _say(client, session_id, headers, "Sunday records after lunch.")
    assert 2 <= len(turned["cards"]) <= 5
    assert turned["slot"] == "leave_out"


def test_local_cards_until_leave_out_then_no_insights_before_the_preview(client, fake_qloo):
    session_id, headers = _open(client, "gate@example.com")
    committed = _drive_to_preview(client, session_id, headers)
    assert committed["committed"] is True
    assert committed["phase"] == "preview"
    assert committed["can_preview"] is True
    # The whole chat ran on local cards; Qloo insights never fired.
    assert not any(call[0] == "insights" for call in fake_qloo.calls)
    for card in committed["cards"]:
        assert card["source"] == "local"

    # Acknowledged exclusions alone do not open the card gate.
    between = _get(client, session_id, headers)
    assert between["phase"] == "preview"
    for card in between["cards"]:
        assert card["source"] == "local"
    assert not any(call[0] == "insights" for call in fake_qloo.calls)

    preview = client.post(f"/sessions/{session_id}/preview", headers=headers)
    assert preview.status_code == 200, preview.text
    assert "war documentaries" in json.dumps(preview.json())

    after = _get(client, session_id, headers)
    assert after["phase"] == "list"
    assert after["cards"]
    for card in after["cards"]:
        assert card["source"] == "qloo"
        assert card["entity_id"]
        assert card["phrase"] is None
    assert any(call[0] == "insights" for call in fake_qloo.calls)


def test_qloo_cards_carry_no_biography_and_respect_exclusions(client, fake_qloo):
    fake_qloo.catalog["urn:entity:artist"] = [
        {"entity_id": "A-9", "name": "World War II Documentary Collection", "subtype": "urn:entity:artist", "image_url": None},
        {"entity_id": "A-1", "name": "Nat King Cole", "subtype": "urn:entity:artist", "image_url": "https://img.example/nat.jpg"},
    ]
    session_id, headers = _open(client, "bio@example.com")
    _drive_to_preview(client, session_id, headers)
    preview = client.post(f"/sessions/{session_id}/preview", headers=headers)
    assert preview.status_code == 200, preview.text
    cards = _get(client, session_id, headers)["cards"]
    names = [card["name"] for card in cards]
    assert "World War II Documentary Collection" not in names
    assert "Nat King Cole" in names
    text = json.dumps(cards)
    assert "biography" not in text
    assert "description" not in text
    assert "Sunday records" not in text


def test_ratings_one_to_five_stay_in_range(client):
    session_id, headers = _open(client, "rate-bounds@example.com")
    view = _get(client, session_id, headers)
    card_id = view["cards"][0]["card_id"]
    for bad in (0, 6, -1):
        refused = client.post(
            f"/sessions/{session_id}/ratings",
            json={"card_id": card_id, "score": bad},
            headers=headers,
        )
        assert refused.status_code == 422, (bad, refused.text)
    missing = client.post(
        f"/sessions/{session_id}/ratings",
        json={"card_id": "c_notondesk", "score": 3},
        headers=headers,
    )
    assert missing.status_code == 404
    stored = client.post(
        f"/sessions/{session_id}/ratings",
        json={"card_id": card_id, "score": 3},
        headers=headers,
    )
    assert stored.status_code == 200, stored.text
    body = stored.json()
    assert body["stored"] is True
    assert body["card_id"] == card_id
    assert body["score"] == 3
    assert body["reply"]
    assert set(body["leans"]) == {"music", "film", "place"}


def test_rating_narrows_signals_without_exclusions_or_unsigning(client, fake_qloo):
    session_id, headers = _open(client, "narrow@example.com")
    _drive_to_preview(client, session_id, headers)
    preview = client.post(f"/sessions/{session_id}/preview", headers=headers)
    assert preview.status_code == 200, preview.text
    view = _get(client, session_id, headers)
    cards = view["cards"]
    assert cards and all(card["source"] == "qloo" for card in cards)

    def insights_calls():
        return [call for call in fake_qloo.calls if call[0] == "insights"]

    before_calls = len(insights_calls())
    keep = cards[-1]
    raise_score = client.post(
        f"/sessions/{session_id}/ratings",
        json={"card_id": keep["card_id"], "score": 5},
        headers=headers,
    )
    assert raise_score.status_code == 200, raise_score.text
    saved = raise_score.json()
    assert saved["score"] == 5
    assert saved["reply"].startswith("Kept.")

    stored = client.app.state.store.get(session_id)
    exclusions_after_keep = json.dumps(stored["exclusions"])
    previewed_after_keep = stored["previewed_at"]
    assert "Nat King Cole" not in exclusions_after_keep or keep["name"] not in exclusions_after_keep
    assert stored["sealed"] is not True

    # A 1 demotes without writing exclusions or clearing previewed_at.
    demote_target = cards[0]
    lowered = client.post(
        f"/sessions/{session_id}/ratings",
        json={"card_id": demote_target["card_id"], "score": 1},
        headers=headers,
    )
    assert lowered.status_code == 200, lowered.text
    assert lowered.json()["reply"].startswith("Noted.")

    stored = client.app.state.store.get(session_id)
    assert json.dumps(stored["exclusions"]) == exclusions_after_keep
    assert stored["previewed_at"] == previewed_after_keep
    assert not stored.get("sealed")

    # The next insights request carries the promoted entity id.
    refreshed = _get(client, session_id, headers)
    assert 2 <= len(refreshed["cards"]) <= 5
    latest = insights_calls()[-1]
    assert len(insights_calls()) > before_calls
    assert keep["entity_id"] in latest[3]
    assert demote_target["entity_id"] not in latest[3]
    demoted_names = [card["name"] for card in refreshed["cards"]]
    assert demote_target["name"] not in demoted_names


def test_rating_a_typed_phrase_down_keeps_it_out_of_the_signal(client, fake_qloo):
    session_id, headers = _open(client, "phrase-down@example.com")
    first = _say(client, session_id, headers, "gardening")
    jazz_card = next(card for card in first["cards"] if card["phrase"] == "gardening")
    _say(client, session_id, headers, "Sunday records after lunch.")
    _say(client, session_id, headers, "none")

    dropped = client.post(
        f"/sessions/{session_id}/ratings",
        json={"card_id": jazz_card["card_id"], "score": 2},
        headers=headers,
    )
    assert dropped.status_code == 200, dropped.text

    view = _get(client, session_id, headers)
    phrases = [card.get("phrase") for card in view["cards"]]
    assert "gardening" not in phrases
    stored = client.app.state.store.get(session_id)
    assert stored["uploads"][0]["interests"] == []
    assert "gardening" not in json.dumps(stored["interview"]["interests"])
    assert stored["exclusions"] == []


def test_full_chat_path_to_candidates_and_feedback(client, fake_qloo):
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
    committed = _drive_to_preview(client, session_id, headers)
    assert committed["committed"] is True
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
    assert "jazz" in stored
    session = client.app.state.store.get(session_id)
    assert session["feedback"]["fit"] == "It felt like her."
    assert session["feedback"]["next"] == "Ask about Sunday sooner."
    assert session["uploads"][0]["anecdote"] == ""
    assert session["uploads"][0]["photo_path"] == ""


def test_bare_ping_after_the_preview_returns_fresh_cards(client, fake_qloo):
    session_id, headers = _open(client, "ping@example.com")
    _drive_to_preview(client, session_id, headers)
    preview = client.post(f"/sessions/{session_id}/preview", headers=headers)
    assert preview.status_code == 200, preview.text
    before = _get(client, session_id, headers)
    turn_count = len(before["turns"])
    assert before["cards"]

    pinged = client.post(
        f"/sessions/{session_id}/interview",
        json={"text": ""},
        headers=headers,
    )
    assert pinged.status_code == 200, pinged.text
    body = pinged.json()
    assert body["reply"] == "Here are more suggestions."
    assert len(body["turns"]) == turn_count
    assert 2 <= len(body["cards"]) <= 5


def test_moment_and_leave_out_are_frozen_after_commit(client):
    session_id, headers = _open(client, "frozen@example.com")
    _drive_to_preview(client, session_id, headers)
    moment = client.post(
        f"/sessions/{session_id}/interview",
        json={"slot": "moment", "text": "A different Sunday."},
        headers=headers,
    )
    assert moment.status_code == 409
    leave = client.post(
        f"/sessions/{session_id}/interview",
        json={"slot": "leave_out", "text": "nothing at all"},
        headers=headers,
    )
    assert leave.status_code == 409


def test_invites_require_an_account(client):
    response = client.post("/invites", json={"caregiver_label": "Ada"})
    assert response.status_code == 401
    assert "password" not in response.text.lower()


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

    session_id, headers = _open(client, "owner@example.com")
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
    view = client.get(f"/sessions/{body['session_id']}/interview", headers=headers)
    assert view.status_code == 200, view.text
    assert view.json()["phase"] == "preview"
    blocked = client.post(f"/sessions/{body['session_id']}/candidates", headers=headers)
    assert blocked.status_code == 409
    assert not any(call[0] == "insights" for call in fake_qloo.calls)


def test_persona_summary_from_sample_answers():
    state = normalize_interview({"answers": _short_answers()})
    built = materialize(state)
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


def test_legacy_answers_upgrade_to_the_chat_shape():
    state = normalize_interview({"answers": _short_answers()})
    assert state["moment"] == "Sunday records after lunch."
    assert state["leave_out"] == "war documentaries"
    phrases = [item["phrase"] for item in state["interests"]]
    # Music and film land before hobby overflow; habit probes are dropped.
    assert phrases.index("jazz") < phrases.index("gardening")
    assert "late morning" not in phrases
    built = materialize(state)
    assert "war documentaries" in built["exclusions"]
    assert "none" not in built["exclusions"]


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


def test_acknowledge_quotes_their_words():
    assert acknowledge("gardening") == 'I\'ll keep "gardening."'
    assert acknowledge("Sunday records after lunch.") == 'I\'ll keep "Sunday records after lunch."'
    assert acknowledge("  none  ") == "I'll leave that as none."
    assert acknowledge("skip") == "I'll leave that as none."
    clipped = acknowledge("word " * 40)
    assert clipped.startswith('I\'ll keep "')
    assert clipped.endswith('...".')
    assert "\u2014" not in clipped
    assert acknowledge("") == ""


def test_persona_summary_does_not_invent_from_empty():
    portrait = persona_summary(interests=[], habits=[], anecdote="", exclusions=[])
    assert "still need" in portrait["persona_summary"].casefold()


def test_materialize_keeps_music_and_film_when_hands_fill_early_slots():
    """Hobby comma-lists must not zero out later music/film probe answers."""
    state = normalize_interview(
        {
            "answers": {
                "hands": (
                    "Dogs, Detallado Inteligente Motorcyles, Music, Audiophile, "
                    "Cooking, Woodworking supply store"
                ),
                "place": "Garage, Colombo",
                "moment": "An ordinary afternoon.",
                "music": "Named Singer One, Named Band Two, Named Song Three",
                "film": "Named Film One, Named Film Two",
            }
        }
    )
    built = materialize(state)
    interests = built["interests"]
    for name in (
        "Named Singer One",
        "Named Band Two",
        "Named Song Three",
        "Named Film One",
        "Named Film Two",
    ):
        assert name in interests, interests
    # Cultural answers come before hobby overflow.
    assert interests.index("Named Singer One") < interests.index("Dogs")
    assert "Detallado Inteligente motorcycles" in interests
    assert soft_normalize_interest("Motorcyles") == "motorcycles"


def test_candidate_reason_is_short_not_a_laundry_list():
    signals = [
        {"name": name, "kind": "tag", "id": f"t-{i}", "interest": name}
        for i, name in enumerate(
            ["Dogs", "Motorcycles", "Cooking", "Garage", "Colombo", "Music"]
        )
    ]
    why = candidate_reason("Some Suggested Artist", signals)
    assert "Cultural affinity with stated interests" not in why
    assert why.count(",") == 0
    assert why.startswith("Near ")
    assert len(why) < 80
    direct = candidate_reason(
        "Named Singer One",
        [{"name": "Named Singer One", "kind": "entity", "id": "E1", "interest": "Named Singer One"}],
    )
    assert direct.startswith("Matches what you named:")
