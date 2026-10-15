"""The public root is Tuck: an account, then one question."""

from pathlib import Path


def test_root_is_tuck_account_first(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    text = response.text
    assert "Tuck" in text
    assert "What email should I use for your account?" in text
    assert "Make this my account" in text
    assert "I have an account" in text
    assert "<details" in text and "Advanced" in text
    assert "Open the sample family" in text
    assert text.index("Advanced") < text.index("Open the sample family")
    assert "half an hour to two hours" in text
    assert "not therapy" in text
    assert "Get Started" not in text
    assert "Open a family session" not in text
    assert "\u2014" not in text
    assert "DEBUG" not in text
    assert "SPOTIFY_CLIENT_ID" not in text
    assert "client id" not in text.lower()


def test_static_guide_assets(client):
    css = client.get("/static/app.css")
    js = client.get("/static/app.js")
    assert css.status_code == 200
    assert js.status_code == 200
    assert "\u2014" not in css.text
    assert "\u2014" not in js.text
    assert "SPOTIFY_CLIENT_ID" not in js.text
    assert "client id" not in js.text.lower()
    assert "webkitSpeechRecognition" in js.text
    assert 'id="mic" hidden' in client.get("/").text
    assert "Speak, then edit" in client.get("/").text
    assert "Who are you helping?" in js.text
    assert "aria-busy" in js.text
    assert "sessionStorage" in js.text
    assert "textContent" in js.text
    # The invite token is kept in session storage, not written into the page.
    assert "invite-token" not in client.get("/").text


def test_sample_family_is_labeled_and_public(client):
    response = client.get("/sample-family")
    assert response.status_code == 200
    body = response.json()
    assert body["caregiver_label"] == "Asha (daughter)"
    assert body["interests"] == ["Nat King Cole", "jazz"]
    assert body["exclusions"][0]["label"] == "war documentaries"
    assert "not a Qloo signal" in body["note"]


def test_playlist_module_keeps_private_endpoints():
    source = (Path(__file__).resolve().parents[1] / "app" / "playlist.py").read_text(encoding="utf-8")
    assert "/v1/me/playlists" in source
    assert "/v1/playlists/{playlist_id}/items" in source
