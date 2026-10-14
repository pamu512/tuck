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
