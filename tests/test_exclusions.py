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
    response = client.post("/sessions/s_missing/preview", headers={"X-Invite-Token": "x"})
    assert response.status_code == 404
