"""The guide resolves explicit interests and refuses candidates before preview."""

import json

from conftest import acknowledge, run_to_preview, start_session


def test_guide_refuses_candidates_until_preview_is_signed(client, fake_qloo):
    session_id, headers = start_session(
        client, anecdote="She kept a pigeon loft above the bakery."
    )
    early = client.post(
        f"/sessions/{session_id}/guide",
        json={"intent": "candidates"},
        headers=headers,
    )
    assert early.status_code == 409

    acknowledge(client, session_id, headers, [])
    still = client.post(
        f"/sessions/{session_id}/guide",
        json={"intent": "candidates"},
        headers=headers,
    )
    assert still.status_code == 409
    assert [call for call in fake_qloo.calls if call[0] == "insights"] == []

    preview = run_to_preview(client, session_id, headers)
    assert preview["guide"]["candidates_open"] is True
    assert preview["guide"]["input"]["interests"] == ["jazz", "Nat King Cole"]
    blob = json.dumps(fake_qloo.calls) + json.dumps(preview["guide"])
    assert "pigeon" not in blob
    assert "bakery" not in blob
    assert "photo.jpg" not in blob

    allowed = client.post(
        f"/sessions/{session_id}/guide",
        json={"intent": "candidates"},
        headers=headers,
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["allowed"] is True
    assert allowed.json()["input"] == {"interests": ["jazz", "Nat King Cole"]}
    assert "pigeon" not in allowed.text
    assert "bakery" not in allowed.text


def test_named_music_and_film_interests_reach_insights_as_entities(client, fake_qloo):
    """Named cultural answers must resolve to entity signals in Insights params."""
    fake_qloo.entities.update(
        {
            "named singer one": [
                {"entity_id": "E-SINGER-1", "name": "Named Singer One"}
            ],
            "named film one": [
                {"entity_id": "E-FILM-1", "name": "Named Film One"}
            ],
        }
    )
    session_id, headers = start_session(
        client,
        interests=[
            "Dogs",
            "Motorcycles",
            "Cooking",
            "Garage",
            "Colombo",
            "Music",
            "Named Singer One",
            "Named Film One",
        ],
    )
    acknowledge(client, session_id, headers, [])
    preview = run_to_preview(client, session_id, headers)
    kinds = {signal["kind"] for signal in preview["signals"]}
    ids = {signal["id"] for signal in preview["signals"]}
    assert "entity" in kinds
    assert "E-SINGER-1" in ids
    assert "E-FILM-1" in ids

    response = client.post(f"/sessions/{session_id}/candidates", headers=headers)
    assert response.status_code == 200, response.text
    insight_calls = [call for call in fake_qloo.calls if call[0] == "insights"]
    assert insight_calls
    entity_args = set()
    for call in insight_calls:
        # ("insights", entity_type, signal_tags, signal_entities)
        entity_args.update(call[3])
    assert "E-SINGER-1" in entity_args
    assert "E-FILM-1" in entity_args
    for row in response.json().get("candidates") or []:
        why = row.get("why") or ""
        assert "Cultural affinity with stated interests" not in why
