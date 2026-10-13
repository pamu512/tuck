"""Trauma exclusions: enforced before any Qloo call, on signals sent, and on
results returned."""

from conftest import acknowledge, run_to_candidates, run_to_preview, start_session


def test_no_qloo_call_before_exclusions_acknowledged(client, fake_qloo):
    session_id, headers = start_session(client)
    response = client.post(f"/sessions/{session_id}/preview", headers=headers)
    assert response.status_code == 409
    response = client.post(f"/sessions/{session_id}/candidates", headers=headers)
    assert response.status_code == 409
    assert fake_qloo.calls == []


def test_empty_exclusions_list_is_a_deliberate_acknowledgment(client, fake_qloo):
    session_id, headers = start_session(client)
    body = acknowledge(client, session_id, headers, [])
    assert body["acknowledged"] is True
    preview = run_to_preview(client, session_id, headers)
    assert preview["signals"]
    assert fake_qloo.calls  # Qloo is reached only after acknowledgment


def test_candidates_blocked_between_acknowledge_and_preview(client, fake_qloo):
    session_id, headers = start_session(client)
    acknowledge(client, session_id, headers, [])
    response = client.post(f"/sessions/{session_id}/candidates", headers=headers)
    assert response.status_code == 409
    insights_calls = [c for c in fake_qloo.calls if c[0] == "insights"]
    assert insights_calls == []


def test_excluded_entity_withheld_from_signals_and_results(client, fake_qloo):
    session_id, headers = start_session(client)
    acknowledge(
        client,
        session_id,
        headers,
        [{"label": "Nat King Cole", "kind": "entity", "reason": "too personal right now"}],
    )
    preview = run_to_preview(client, session_id, headers)
    withheld_names = [item["name"] for item in preview["withheld_by_exclusions"]]
    assert "Nat King Cole" in withheld_names
    assert "E0CD9B0B-75BB-4DAE-BCB7-79AB5DE42A0D" not in preview["what_qloo_receives"]["signal_ids"]

    body = run_to_candidates(client, session_id, headers)
    names = [candidate["name"] for candidate in body["candidates"]]
    assert "Nat King Cole" not in names
    assert "Miles Davis" in names  # unaffected candidates still flow
    for call in fake_qloo.calls:
        if call[0] == "insights":
            assert "E0CD9B0B" not in str(call[2] + call[3])


def test_excluded_topic_filtered_from_candidates_by_name(client):
    session_id, headers = start_session(client)
    acknowledge(
        client,
        session_id,
        headers,
        [{"label": "war documentaries", "kind": "topic", "reason": "veteran trauma"}],
    )
    run_to_preview(client, session_id, headers)
    body = run_to_candidates(client, session_id, headers)
    names = [candidate["name"] for candidate in body["candidates"]]
    assert "World War II Documentary Collection" not in names
    assert "Singin' in the Rain" in names
    assert [item["name"] for item in body["removed_by_exclusions"]] == [
        "World War II Documentary Collection"
    ]


def test_changed_exclusions_after_preview_force_a_new_preview(client, fake_qloo):
    session_id, headers = start_session(client)
    acknowledge(client, session_id, headers, [])
    run_to_preview(client, session_id, headers)
    acknowledge(client, session_id, headers, [{"label": "jazz", "kind": "topic", "reason": "reconsidered"}])

    response = client.post(f"/sessions/{session_id}/candidates", headers=headers)
    assert response.status_code == 409

    preview = run_to_preview(client, session_id, headers)
    withheld_names = [item["name"] for item in preview["withheld_by_exclusions"]]
    assert "Jazz" in withheld_names
    assert "urn:tag:genre:music:jazz" not in preview["what_qloo_receives"]["signal_ids"]

    body = run_to_candidates(client, session_id, headers)
    assert body["candidates"]
    for call in fake_qloo.calls:
        if call[0] == "insights":
            assert "urn:tag:genre:music:jazz" not in call[2]


def test_one_failing_insight_type_degrades_not_kills(client, fake_qloo):
    """Live evidence 2026-10-13: a place-type insights query hung >60s while
    artist/film returned in ~2s. One failing category must not kill the
    candidates response."""
    from app.qloo import QlooUpstreamError

    original = fake_qloo.insights

    def flaky(entity_type, signal_tags=(), signal_entities=(), take=6):
        if entity_type == "urn:entity:place":
            raise QlooUpstreamError("simulated upstream timeout for place")
        return original(entity_type, signal_tags, signal_entities, take)

    fake_qloo.insights = flaky
    session_id, headers = start_session(client)
    acknowledge(client, session_id, headers, [])
    run_to_preview(client, session_id, headers)
    body = run_to_candidates(client, session_id, headers)
    types = {candidate["type"] for candidate in body["candidates"]}
    assert "music" in types and "film" in types
    assert "place" not in types
    assert [item["type"] for item in body["types_failed"]] == ["place"]
    assert client.app.state.store.get(session_id) is None


def test_invite_token_required_for_session_routes(client):
    session_id, _ = start_session(client)
    valid_bodies = {
        "uploads": {"photo_path": "/tmp/x.jpg", "anecdote": "text", "interests": []},
        "exclusions": {"exclusions": []},
        "preview": None,
        "candidates": None,
    }
    for path, body in valid_bodies.items():
        response = client.post(
            f"/sessions/{session_id}/{path}", json=body, headers={"X-Invite-Token": "wrong"}
        )
        assert response.status_code == 401, (path, response.status_code)
    response = client.delete(
        f"/sessions/{session_id}", headers={"X-Invite-Token": "wrong"}
    )
    assert response.status_code == 401
    response = client.post("/sessions/s_missing/preview", headers={"X-Invite-Token": "x"})
    assert response.status_code == 404


def test_stem_movies_and_y_plurals():
    from app.session import _stem

    assert _stem("movies") == "movie"
    assert _stem("movie") == "movie"
    assert _stem("talkies") == "talkie"
    assert _stem("documentaries") == "documentary"
    assert _stem("stories") == "story"
    assert _stem("cities") == "city"
    assert _stem("navies") == "navy"
    assert _stem("films") == "film"
    assert _stem("wars") == "wars"


def test_war_movies_wwii_and_world_war_two_match_collection():
    from app.session import exclusion_matches

    title = "World War II Movie Collection"
    for label in (
        "war movies",
        "WWII",
        "World War Two",
        "ww ii",
        "World War 2",
        "WW-II",
        "World-War-Two",
    ):
        assert exclusion_matches({"label": label, "kind": "topic"}, name=title), label
        assert not exclusion_matches(
            {"label": label, "kind": "topic"}, name="Singin' in the Rain"
        ), label
    assert not exclusion_matches(
        {"label": "war movies", "kind": "topic"}, name="The Star Wars Movie Collection"
    )
    assert not exclusion_matches(
        {"label": "war movies", "kind": "topic"}, name="War Movy Collection"
    )
    assert exclusion_matches(
        {"label": "war movies", "kind": "topic"}, name="War Movie Marathon"
    )
    assert not exclusion_matches(
        {"label": "WW-I", "kind": "topic"}, name=title
    )
    assert exclusion_matches(
        {"label": "war documentaries", "kind": "topic"},
        name="World War II Documentary Collection",
    )
    assert not exclusion_matches(
        {"label": "WWII", "kind": "topic"}, name="World War I Documentary"
    )
    assert not exclusion_matches(
        {"label": "World War Two", "kind": "topic"}, name="World War One Newsreel"
    )
    assert not exclusion_matches(
        {"label": "war movies", "kind": "topic"}, name="World War I Documentary"
    )


def test_unlisted_ie_plural_still_matches_its_singular():
    from app.session import exclusion_matches

    assert exclusion_matches(
        {"label": "foobie films", "kind": "topic"},
        name="Foobie Film Festival",
    )


def test_resolved_id_matches_when_the_display_name_does_not():
    from app.session import exclusion_matches

    exclusion = {
        "label": "Singin' in the Rain",
        "kind": "entity",
        "resolved_ids": [{"id": "M-2", "name": "Singin' in the Rain", "kind": "entity"}],
    }
    assert exclusion_matches(exclusion, name="Rain Musical", entity_id="M-2")
    assert not exclusion_matches(exclusion, name="Rain Musical", entity_id="M-9")


def test_loose_qloo_hit_stays_unresolved_and_close_hit_is_kept(client, fake_qloo):
    session_id, headers = start_session(client)
    fake_qloo.tags["war movies"] = [{"id": "urn:tag:nearby", "name": "Nearby cafes"}]
    body = acknowledge(
        client,
        session_id,
        headers,
        [{"label": "war movies", "kind": "topic", "reason": "veteran trauma"}],
    )
    assert body["unresolved_exclusions"] == ["war movies"]
    assert body["exclusions"][0]["resolved"] is False
    assert body["exclusions"][0]["resolved_ids"] == []

    fake_qloo.tags["wwii"] = [{"id": "urn:tag:topic:ww2", "name": "World War II"}]
    body = acknowledge(
        client,
        session_id,
        headers,
        [{"label": "WWII", "kind": "topic", "reason": "veteran trauma"}],
    )
    assert body["unresolved_exclusions"] == []
    assert body["exclusions"][0]["resolved_ids"] == [
        {"id": "urn:tag:topic:ww2", "name": "World War II", "kind": "tag"}
    ]


def test_adversarial_labels_filter_movie_collection_and_report_unresolved(client, fake_qloo):
    fake_qloo.catalog["urn:entity:movie"] = [
        {
            "entity_id": "M-9",
            "name": "World War II Movie Collection",
            "subtype": "urn:entity:movie",
            "image_url": None,
        },
        {
            "entity_id": "M-2",
            "name": "Singin' in the Rain",
            "subtype": "urn:entity:movie",
            "image_url": None,
        },
    ]
    for label in ("war movies", "WWII", "World War Two", "WW-II", "World-War-Two"):
        session_id, headers = start_session(client)
        recorded = acknowledge(
            client,
            session_id,
            headers,
            [{"label": label, "kind": "topic", "reason": "veteran trauma"}],
        )
        assert recorded["unresolved_exclusions"] == [label]
        preview = run_to_preview(client, session_id, headers)
        assert preview["unresolved_exclusions"] == [label]
        body = run_to_candidates(client, session_id, headers)
        names = [candidate["name"] for candidate in body["candidates"]]
        assert "World War II Movie Collection" not in names
        assert "Singin' in the Rain" in names
        assert body["unresolved_exclusions"] == [label]
        assert [item["name"] for item in body["removed_by_exclusions"]] == [
            "World War II Movie Collection"
        ]


def test_wwii_resolved_id_filters_a_renamed_candidate(client, fake_qloo):
    fake_qloo.tags["wwii"] = [{"id": "urn:tag:topic:ww2", "name": "World War II"}]
    fake_qloo.catalog["urn:entity:movie"] = [
        {
            "entity_id": "urn:tag:topic:ww2",
            "name": "Sunday Matinee",
            "subtype": "urn:entity:movie",
            "image_url": None,
        },
        {
            "entity_id": "M-2",
            "name": "Singin' in the Rain",
            "subtype": "urn:entity:movie",
            "image_url": None,
        },
    ]
    session_id, headers = start_session(client)
    recorded = acknowledge(
        client,
        session_id,
        headers,
        [{"label": "WWII", "kind": "topic", "reason": "veteran trauma"}],
    )
    assert recorded["unresolved_exclusions"] == []
    assert recorded["exclusions"][0]["resolved_ids"][0]["id"] == "urn:tag:topic:ww2"
    run_to_preview(client, session_id, headers)
    body = run_to_candidates(client, session_id, headers)
    names = [candidate["name"] for candidate in body["candidates"]]
    assert "Sunday Matinee" not in names
    assert "Singin' in the Rain" in names
    assert body["unresolved_exclusions"] == []


def test_resolved_entity_id_filters_a_renamed_candidate(client, fake_qloo):
    fake_qloo.entities["singin' in the rain"] = [
        {"entity_id": "M-2", "name": "Singin' in the Rain"}
    ]
    fake_qloo.catalog["urn:entity:movie"] = [
        {
            "entity_id": "M-2",
            "name": "Rain Musical",
            "subtype": "urn:entity:movie",
            "image_url": None,
        },
        {
            "entity_id": "M-1",
            "name": "World War II Documentary Collection",
            "subtype": "urn:entity:movie",
            "image_url": None,
        },
    ]
    session_id, headers = start_session(client)
    recorded = acknowledge(
        client,
        session_id,
        headers,
        [{"label": "Singin' in the Rain", "kind": "entity", "reason": "too tender"}],
    )
    assert recorded["unresolved_exclusions"] == []
    assert recorded["exclusions"][0]["resolved_ids"][0]["id"] == "M-2"
    run_to_preview(client, session_id, headers)
    body = run_to_candidates(client, session_id, headers)
    names = [candidate["name"] for candidate in body["candidates"]]
    assert "Rain Musical" not in names
    assert "World War II Documentary Collection" in names


def test_empty_exclusion_list_does_not_call_qloo(client, fake_qloo):
    session_id, headers = start_session(client)
    acknowledge(client, session_id, headers, [])
    assert fake_qloo.calls == []


def test_qloo_lookup_failure_still_saves_exclusions(client, fake_qloo):
    from app.qloo import QlooUpstreamError

    original_tags = fake_qloo.search_tags
    original_entities = fake_qloo.search_entities

    def boom(query: str):
        raise QlooUpstreamError("simulated tag search failure")

    fake_qloo.search_tags = boom
    fake_qloo.search_entities = boom
    session_id, headers = start_session(client)
    body = acknowledge(
        client,
        session_id,
        headers,
        [{"label": "war documentaries", "kind": "topic", "reason": "veteran trauma"}],
    )
    assert body["acknowledged"] is True
    assert body["unresolved_exclusions"] == ["war documentaries"]
    assert "resolution_warning" in body
    fake_qloo.search_tags = original_tags
    fake_qloo.search_entities = original_entities
    preview = run_to_preview(client, session_id, headers)
    assert preview["unresolved_exclusions"] == ["war documentaries"]
