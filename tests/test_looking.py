"""Family photo on the looking path: required moment, opaque /data bytes, preview."""

import io
import json
from pathlib import Path

import pytest

from app.interview import apply_looking_photo, materialize
from app.looking import (
    PhotoStoreError,
    looking_ready,
    normalize_media_type,
    photo_provided,
    read_photo_bytes,
    session_photo_media,
    store_opaque,
)

ROOT = Path(__file__).resolve().parents[1]
PHOTO = b"\xff\xd8\xff\xd9family-photo-bytes"
CHAT = {
    "interest": "jazz, Nat King Cole",
    "moment": "Sunday records after lunch.",
    "leave_out": "war documentaries",
}


def test_looking_ready_requires_a_photo_and_an_anecdote():
    assert looking_ready(False, "Sunday records after lunch.") is False
    assert looking_ready(True, "   ") is False
    assert looking_ready(True, "") is False
    assert looking_ready(True, "Sunday records after lunch.") is True


def test_placeholder_is_not_a_provided_photo():
    assert photo_provided("not-provided") is False
    assert photo_provided("not provided") is False
    assert photo_provided("") is False
    assert photo_provided("/data/" + "a" * 20) is True


def test_materialize_drops_the_placeholder_when_a_photo_is_stored():
    bare = materialize({"moment": "Sunday records after lunch."})
    assert bare["photo_path"] == "not-provided"
    marked = materialize(
        {"moment": "Sunday records after lunch."},
        photo_path="not-provided",
    )
    assert marked["photo_path"] == "not-provided"
    href = "/data/" + "b" * 22
    stored = materialize({"moment": "Sunday records after lunch."}, photo_path=href)
    assert stored["photo_path"] == href
    assert stored["photo_path"] != "not-provided"


def test_apply_looking_photo_replaces_not_provided():
    href = "/data/" + "c" * 22
    session = {
        "uploads": [
            {
                "id": "u_1",
                "photo_path": "not-provided",
                "anecdote": "old",
                "interests": ["jazz"],
            }
        ],
        "interview": {"committed": True, "answers": {}, "exited": False},
    }
    apply_looking_photo(
        session,
        href=href,
        media_type="image/jpeg",
        anecdote="Sunday records after lunch.",
    )
    upload = session["uploads"][0]
    assert upload["photo_path"] == href
    assert upload["photo_path"] != "not-provided"
    assert upload["anecdote"] == "Sunday records after lunch."
    assert upload["interests"] == ["jazz"]
    assert len(session["uploads"]) == 1


def test_photo_bytes_are_stored_without_decoding(tmp_path):
    source = (ROOT / "app" / "looking.py").read_text(encoding="utf-8")
    assert "PIL" not in source
    assert "Image.open" not in source
    assert "qloo" not in source.casefold()
    with pytest.raises(PhotoStoreError):
        read_photo_bytes(io.BytesIO(b""))

    class Unreadable(io.BytesIO):
        def read(self, _n=-1):
            raise OSError("disk read failed")

    with pytest.raises(PhotoStoreError) as unreadable:
        read_photo_bytes(Unreadable())
    assert unreadable.value.status == 503
    with pytest.raises(PhotoStoreError) as too_big:
        read_photo_bytes(io.BytesIO(b"a" * (8 * 1024 * 1024 + 1)))
    assert too_big.value.status == 413
    with pytest.raises(PhotoStoreError):
        normalize_media_type("text/plain")
    assert normalize_media_type("image/jpg") == "image/jpeg"
    assert normalize_media_type("image/jpeg; charset=binary") == "image/jpeg"
    assert normalize_media_type("application/octet-stream", filename="Sunday.HEIC") == "image/heic"
    assert normalize_media_type("", filename="kitchen.png") == "image/png"
    with pytest.raises(PhotoStoreError):
        normalize_media_type("application/octet-stream", filename="notes.txt")
    with pytest.raises(PhotoStoreError):
        normalize_media_type(None, filename="../../x.jpg.exe")
    dest = store_opaque(tmp_path, "d" * 22, PHOTO)
    assert dest.name == "d" * 22
    assert dest.suffix == ""
    assert dest.read_bytes() == PHOTO
    assert not list(tmp_path.glob(".*.tmp"))
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("x", encoding="utf-8")
    with pytest.raises(PhotoStoreError) as cannot:
        store_opaque(blocked, "e" * 22, PHOTO)
    assert cannot.value.status == 503
    href = "/data/" + "d" * 22
    assert session_photo_media({"interview": {"photo_path": "not-provided"}}, href) is None
    assert session_photo_media({"interview": {"photo_path": href, "photo_type": "image/jpeg"}}, href) == "image/jpeg"
    assert session_photo_media({"uploads": [{"photo_path": "", "photo_type": "image/jpeg"}]}, href) is None


def _open(client):
    created = client.post(
        "/accounts",
        json={"email": "ada-looking@example.com", "password": "test-password-1"},
    )
    assert created.status_code == 201, created.text
    named = client.post("/me/profile", json={"person_name": "Ada"})
    assert named.status_code == 200, named.text
    invite = client.post("/invites", json={"caregiver_label": "Ada"})
    assert invite.status_code == 201, invite.text
    body = invite.json()
    return body["session_id"], {"X-Invite-Token": body["invite_token"]}


def _chat_view(client, session_id, headers):
    view = client.get(f"/sessions/{session_id}/interview", headers=headers)
    assert view.status_code == 200, view.text
    return view.json()


def _answer_slot(client, session_id, headers, body):
    turned = client.post(
        f"/sessions/{session_id}/interview",
        json={"text": CHAT[body["slot"]]},
        headers=headers,
    )
    assert turned.status_code == 200, turned.text
    return turned.json()


def _until(client, session_id, headers, slot):
    """Answer chat turns until the sitting is asking for `slot`."""
    for _ in range(6):
        body = _chat_view(client, session_id, headers)
        if body.get("committed") or body.get("phase") != "chat":
            return body
        if body.get("slot") == slot:
            return body
        _answer_slot(client, session_id, headers, body)
    raise AssertionError("interview did not reach " + slot)


def _finish(client, session_id, headers):
    """Answer every chat turn until the sitting commits to the preview."""
    for _ in range(6):
        body = _chat_view(client, session_id, headers)
        if body.get("committed") or body.get("phase") == "preview":
            return body
        if body.get("phase") == "chat" and body.get("slot"):
            _answer_slot(client, session_id, headers, body)
            continue
        raise AssertionError("interview stalled in phase " + str(body.get("phase")))
    raise AssertionError("interview did not finish")


def _post_photo(client, session_id, headers, *, anecdote="Sunday records after lunch.", photo=PHOTO, name="kitchen.jpg", content_type="image/jpeg"):
    kwargs = {"headers": headers}
    if photo is not None:
        kwargs["files"] = {"photo": (name, photo, content_type)}
    if anecdote is not None:
        kwargs["data"] = {"anecdote": anecdote}
    return client.post(f"/sessions/{session_id}/data", **kwargs)


def test_data_upload_requires_photo_and_anecdote(client, fake_qloo):
    session_id, headers = _open(client)
    _until(client, session_id, headers, "moment")
    before = list(fake_qloo.calls)

    missing_photo = _post_photo(client, session_id, headers, photo=None)
    assert missing_photo.status_code == 422
    assert "photo" in missing_photo.text.casefold()

    missing_moment = _post_photo(client, session_id, headers, anecdote="   ")
    assert missing_moment.status_code == 422

    refused = _post_photo(client, session_id, headers, anecdote="none")
    assert refused.status_code == 422

    wrong_type = _post_photo(
        client, session_id, headers, content_type="text/plain", photo=b"not-a-photo"
    )
    assert wrong_type.status_code == 422
    assert fake_qloo.calls == before

    early_id, early_headers = _open_other(client)
    early = _post_photo(client, early_id, early_headers)
    assert early.status_code == 409
    assert list(client.app.state.photo_dir.glob("*")) == []


def _open_other(client):
    invite = client.post("/invites", json={"caregiver_label": "Bea"})
    assert invite.status_code == 201, invite.text
    body = invite.json()
    return body["session_id"], {"X-Invite-Token": body["invite_token"]}


def test_multipart_upload_stores_an_opaque_data_path(client, fake_qloo):
    session_id, headers = _open(client)
    _until(client, session_id, headers, "moment")
    before = list(fake_qloo.calls)
    saved = _post_photo(client, session_id, headers)
    assert saved.status_code == 201, saved.text
    body = saved.json()
    assert body["photo_href"].startswith("/data/")
    assert body["photo_on_file"] is True
    assert "kitchen.jpg" not in saved.text
    assert "never opened" in body["photo_note"]
    assert fake_qloo.calls == before

    photo_id = body["photo_href"].rsplit("/", 1)[-1]
    stored = client.app.state.photo_dir / photo_id
    assert stored.is_file()
    assert stored.suffix == ""
    assert stored.read_bytes() == PHOTO
    assert "kitchen" not in stored.name

    session = client.app.state.store.get(session_id)
    blob = json.dumps(session)
    assert "kitchen.jpg" not in blob
    assert session["interview"]["moment"] == "Sunday records after lunch."
    assert session["interview"]["photo_path"] == body["photo_href"]
    account = client.app.state.accounts.get(session["account_id"])
    moment = next(item for item in account["profile"]["probes"] if item["id"] == "moment")
    assert moment["answer"] == "Sunday records after lunch."

    _finish(client, session_id, headers)
    session = client.app.state.store.get(session_id)
    upload = session["uploads"][0]
    assert upload["photo_path"] == body["photo_href"]
    assert upload["photo_path"] != "not-provided"
    assert upload["anecdote"] == "Sunday records after lunch."
    dumped = json.dumps(fake_qloo.calls)
    assert "family-photo-bytes" not in dumped
    assert photo_id not in dumped


def test_preview_serves_the_photo_and_omits_not_provided(client):
    session_id, headers = _open(client)
    _until(client, session_id, headers, "moment")
    saved = _post_photo(client, session_id, headers)
    assert saved.status_code == 201, saved.text
    href = saved.json()["photo_href"]
    _finish(client, session_id, headers)

    turned = client.post(
        f"/sessions/{session_id}/exclusions",
        json={"exclusions": [{"label": "war documentaries", "kind": "topic"}]},
        headers=headers,
    )
    assert turned.status_code == 200, turned.text
    preview = client.post(f"/sessions/{session_id}/preview", headers=headers)
    assert preview.status_code == 200, preview.text
    sheet = preview.json()
    assert "not-provided" not in preview.text
    assert "not provided" not in preview.text.casefold()
    assert sheet["looking"] == [
        {"photo_href": href, "anecdote": "Sunday records after lunch."}
    ]
    assert "kitchen.jpg" not in preview.text

    photo = client.get(href, headers=headers)
    assert photo.status_code == 200, photo.text
    assert photo.content == PHOTO
    assert photo.headers["content-type"].startswith("image/jpeg")
    assert photo.headers["x-content-type-options"] == "nosniff"
    assert photo.headers["content-security-policy"] == "default-src 'none'"
    assert photo.headers["cross-origin-resource-policy"] == "same-origin"

    client.cookies.clear()
    hidden = client.get(href)
    assert hidden.status_code == 401

    other = client.post(
        "/accounts",
        json={"email": "bea-looking@example.com", "password": "test-password-1"},
    )
    assert other.status_code == 201, other.text
    named = client.post("/me/profile", json={"person_name": "Bea"})
    assert named.status_code == 200, named.text
    foreign = client.post("/invites", json={"caregiver_label": "Bea"})
    assert foreign.status_code == 201, foreign.text
    stolen = client.get(href, headers={"X-Invite-Token": foreign.json()["invite_token"]})
    assert stolen.status_code == 401


def test_committed_placeholder_is_removed_when_a_photo_arrives(client, fake_qloo):
    session_id, headers = _open(client)
    _finish(client, session_id, headers)
    session = client.app.state.store.get(session_id)
    assert session["uploads"][0]["photo_path"] == "not-provided"
    before = list(fake_qloo.calls)
    saved = _post_photo(client, session_id, headers, anecdote="Sunday records after lunch.")
    assert saved.status_code == 201, saved.text
    href = saved.json()["photo_href"]
    session = client.app.state.store.get(session_id)
    assert session["uploads"][0]["photo_path"] == href
    assert session["uploads"][0]["photo_path"] != "not-provided"
    assert "not-provided" not in json.dumps(session["uploads"])
    assert fake_qloo.calls == before

    turned = client.post(
        f"/sessions/{session_id}/exclusions",
        json={"exclusions": []},
        headers=headers,
    )
    assert turned.status_code == 200, turned.text
    preview = client.post(f"/sessions/{session_id}/preview", headers=headers)
    assert preview.status_code == 200, preview.text
    assert preview.json()["looking"][0]["photo_href"] == href
    assert preview.json()["looking"][0]["anecdote"] == "Sunday records after lunch."
    assert "not-provided" not in preview.text

    listed = client.post(f"/sessions/{session_id}/candidates", headers=headers)
    assert listed.status_code == 200, listed.text
    gone = client.get(href, headers=headers)
    assert gone.status_code == 404
    photo_id = href.rsplit("/", 1)[-1]
    assert not (client.app.state.photo_dir / photo_id).exists()


def test_looking_page_is_one_reply_and_an_optional_photo():
    html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "app" / "static" / "app.css").read_text(encoding="utf-8")
    assert 'id="transcript"' in html
    assert 'id="form-looking"' not in html
    assert 'id="looking-anecdote"' not in html
    assert 'id="photo"' in html
    assert 'type="file"' in html
    assert 'id="looking-skip"' in html
    assert "Skip the photo" in html
    photo = html.split('id="photo"', 1)[1].split(">", 1)[0]
    assert "required" not in photo
    assert "required" in html
    assert "\u2014" not in html
    assert "\u2014" not in js
    assert "function photoProvided" in js
    assert "function lookingMoment" in js
    assert "function lookingFileProblem" in js
    assert "function renderTranscript" in js
    onresult = js.split("rec.onresult", 1)[1].split("rec.onerror", 1)[0]
    assert "syncLookingSend()" in onresult
    assert 'folded !== "not-provided"' in js
    assert 'folded !== "not provided"' in js
    assert "syncLookingSend" in js
    assert "skipPhoto" in js
    assert 'node.type === "file"' in js
    assert "8 * 1024 * 1024" in js
    assert "nothing else" in js
    assert "FormData" in js
    assert "/data" in js
    assert 'className = "looking-photo looking-thumb"' in js
    assert js.index("persona_summary") < js.index("looking-photo")
    assert js.index("looking-photo") < js.index("qloo_plain")
    assert js.index("qloo_plain") < js.index('textContent = "Ask for the list"')
    assert js.index('className = "tech"') < js.index("Signal ids")
    assert "not-provided" not in html
    assert "image/heic" in html
    assert "image/*" not in html
    assert "DEBUG" not in html
    assert "client id" not in html.lower()
    assert ".looking-photo" in css
    assert ".looking-thumb" in css
    assert "max-height: 22rem" in css
    assert "width: 100%" in css
    assert "overflow-wrap: anywhere" in css
    assert "max-height: 16rem" in css


def test_data_without_a_token_does_not_read_the_rules(client):
    session_id, _headers = _open(client)
    _until(client, session_id, _headers, "moment")
    denied = client.post(
        f"/sessions/{session_id}/data",
        data={"anecdote": ""},
    )
    assert denied.status_code == 401
    assert "photo" not in denied.text.casefold()
    assert list(client.app.state.photo_dir.iterdir()) == []


def test_moment_can_skip_the_photo(client, fake_qloo):
    session_id, headers = _open(client)
    _until(client, session_id, headers, "moment")
    before = list(fake_qloo.calls)
    turned = client.post(
        f"/sessions/{session_id}/interview",
        json={"text": "Sunday records after lunch."},
        headers=headers,
    )
    assert turned.status_code == 200, turned.text
    body = turned.json()
    assert body["photo_attached"] is False
    assert body["turns"][-1]["id"] == "moment"
    assert body["turns"][-1]["answer"] == "Sunday records after lunch."
    assert body["asked"].startswith("I'll keep")
    assert "family-photo-bytes" not in turned.text
    assert fake_qloo.calls == before
    assert list(client.app.state.photo_dir.iterdir()) == []
    session = client.app.state.store.get(session_id)
    assert session["interview"]["moment"] == "Sunday records after lunch."
    assert str(session["interview"].get("photo_path") or "") in ("", "not-provided")


def test_missing_token_stores_nothing(client):
    session_id, headers = _open(client)
    _until(client, session_id, headers, "moment")
    denied = _post_photo(client, session_id, {})
    assert denied.status_code == 401
    assert list(client.app.state.photo_dir.iterdir()) == []


def test_octet_stream_keeps_the_suffix_and_drops_the_filename(client, fake_qloo):
    session_id, headers = _open(client)
    _until(client, session_id, headers, "moment")
    before = list(fake_qloo.calls)
    saved = _post_photo(
        client,
        session_id,
        headers,
        photo=b"heic-family-bytes",
        name="sunday.heic",
        content_type="application/octet-stream",
    )
    assert saved.status_code == 201, saved.text
    href = saved.json()["photo_href"]
    assert "sunday.heic" not in saved.text
    assert "sunday.heic" not in json.dumps(client.app.state.store.get(session_id))
    photo = client.get(href, headers=headers)
    assert photo.status_code == 200, photo.text
    assert photo.content == b"heic-family-bytes"
    assert photo.headers["content-type"].startswith("image/heic")
    assert fake_qloo.calls == before


def test_dotted_photo_id_is_not_a_file(client):
    session_id, headers = _open(client)
    missing = client.get("/data/" + ("a" * 10) + "." + ("b" * 10), headers=headers)
    assert missing.status_code == 404
    assert session_id


def test_cleared_path_is_not_served_even_if_the_file_remains(client):
    session_id, headers = _open(client)
    _until(client, session_id, headers, "moment")
    saved = _post_photo(client, session_id, headers)
    assert saved.status_code == 201, saved.text
    href = saved.json()["photo_href"]
    photo_id = href.rsplit("/", 1)[-1]
    session = client.app.state.store.get(session_id)
    session["interview"]["photo_path"] = ""
    session["interview"]["photo_type"] = ""
    client.app.state.store.put(session)
    hidden = client.get(href, headers=headers)
    assert hidden.status_code == 404
    assert (client.app.state.photo_dir / photo_id).is_file()


def test_replacing_a_photo_discards_the_previous_file(client, fake_qloo):
    session_id, headers = _open(client)
    _finish(client, session_id, headers)
    before = list(fake_qloo.calls)
    first = _post_photo(client, session_id, headers)
    assert first.status_code == 201, first.text
    href = first.json()["photo_href"]
    photo_id = href.rsplit("/", 1)[-1]
    second_bytes = b"\x89PNG\r\nsecond"
    second = _post_photo(
        client,
        session_id,
        headers,
        photo=second_bytes,
        name="other.png",
        content_type="image/png",
    )
    assert second.status_code == 201, second.text
    href2 = second.json()["photo_href"]
    assert href2 != href
    assert "other.png" not in second.text
    assert not (client.app.state.photo_dir / photo_id).exists()
    got = client.get(href2, headers=headers)
    assert got.status_code == 200, got.text
    assert got.content == second_bytes
    assert got.headers["content-type"].startswith("image/png")
    assert client.get(href, headers=headers).status_code == 404
    dumped = json.dumps(fake_qloo.calls)
    assert "second" not in dumped
    assert fake_qloo.calls[: len(before)] == before


def test_failed_save_keeps_the_previous_photo(client, monkeypatch):
    session_id, headers = _open(client)
    _finish(client, session_id, headers)
    first = _post_photo(client, session_id, headers)
    assert first.status_code == 201, first.text
    href = first.json()["photo_href"]
    photo_id = href.rsplit("/", 1)[-1]

    def boom(_session):
        raise OSError("disk full")

    monkeypatch.setattr(client.app.state.store, "put", boom)
    second = _post_photo(
        client,
        session_id,
        headers,
        photo=b"second-photo-bytes",
        name="other.png",
        content_type="image/png",
    )
    assert second.status_code == 503
    assert "other.png" not in second.text
    assert "second-photo-bytes" not in second.text
    monkeypatch.undo()
    session = client.app.state.store.get(session_id)
    assert session["uploads"][0]["photo_path"] == href
    assert (client.app.state.photo_dir / photo_id).read_bytes() == PHOTO
    assert [path.name for path in client.app.state.photo_dir.iterdir()] == [photo_id]


def test_failed_moment_save_does_not_record_the_account(client, monkeypatch):
    session_id, headers = _open(client)
    _until(client, session_id, headers, "moment")
    session = client.app.state.store.get(session_id)
    account_id = session["account_id"]
    before = [
        item.get("id")
        for item in (client.app.state.accounts.get(account_id).get("profile") or {}).get("probes") or []
    ]
    assert "moment" not in before

    def boom(_session):
        raise OSError("disk full")

    monkeypatch.setattr(client.app.state.store, "put", boom)
    saved = _post_photo(client, session_id, headers)
    assert saved.status_code == 503
    monkeypatch.undo()
    session = client.app.state.store.get(session_id)
    assert not (session.get("interview") or {}).get("moment")
    after = [
        item.get("id")
        for item in (client.app.state.accounts.get(account_id).get("profile") or {}).get("probes") or []
    ]
    assert after == before
    assert list(client.app.state.photo_dir.iterdir()) == []


def test_delete_removes_the_photo_file(client):
    session_id, headers = _open(client)
    _until(client, session_id, headers, "moment")
    saved = _post_photo(client, session_id, headers)
    assert saved.status_code == 201, saved.text
    href = saved.json()["photo_href"]
    photo_id = href.rsplit("/", 1)[-1]
    gone = client.delete(f"/sessions/{session_id}", headers=headers)
    assert gone.status_code == 204
    assert not (client.app.state.photo_dir / photo_id).exists()
    assert client.get(href, headers=headers).status_code == 404


def test_expired_session_removes_the_photo_file(client):
    session_id, headers = _open(client)
    _until(client, session_id, headers, "moment")
    saved = _post_photo(client, session_id, headers)
    assert saved.status_code == 201, saved.text
    href = saved.json()["photo_href"]
    photo_id = href.rsplit("/", 1)[-1]
    session = client.app.state.store.get(session_id)
    session["created_at"] = "2000-01-01T00:00:00+00:00"
    client.app.state.store.put(session)
    missing = client.get(f"/sessions/{session_id}/interview", headers=headers)
    assert missing.status_code == 404
    assert not (client.app.state.photo_dir / photo_id).exists()


def test_next_invite_removes_an_expired_photo(client):
    session_id, headers = _open(client)
    _until(client, session_id, headers, "moment")
    saved = _post_photo(client, session_id, headers)
    assert saved.status_code == 201, saved.text
    photo_id = saved.json()["photo_href"].rsplit("/", 1)[-1]
    session = client.app.state.store.get(session_id)
    session["created_at"] = "2000-01-01T00:00:00+00:00"
    client.app.state.store.put(session)
    invite = client.post("/invites", json={"caregiver_label": "Ada"})
    assert invite.status_code == 201, invite.text
    assert client.app.state.store.get(session_id) is None
    assert not (client.app.state.photo_dir / photo_id).exists()
