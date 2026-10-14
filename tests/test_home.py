"""The caregiver desk is the public root, not a 404."""

def test_root_is_the_caregiver_desk(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    text = response.text
    assert "Comfortable shared listening, after the family says what stays out." in text
    assert "Open the sample family" in text
    for step in ("invite", "memory", "exclusions", "preview", "list", "playlist"):
        assert f'data-step="{step}"' in text
    assert "\u2014" not in text
    assert "DEBUG" not in text
    assert "Get Started" not in text


def test_static_desk_assets(client):
    css = client.get("/static/app.css")
    js = client.get("/static/app.js")
    assert css.status_code == 200
    assert js.status_code == 200
    assert "\u2014" not in css.text
    assert "\u2014" not in js.text
    assert "furthestStep" in js.text
    assert "Remove this exclusion" in js.text
    assert 'aria-busy' in js.text


def test_sample_family_is_labeled_and_public(client):
    response = client.get("/sample-family")
    assert response.status_code == 200
    body = response.json()
    assert body["caregiver_label"] == "Asha (daughter)"
    assert body["interests"] == ["Nat King Cole", "jazz"]
    assert body["exclusions"][0]["label"] == "war documentaries"
    assert "not a Qloo signal" in body["note"]
