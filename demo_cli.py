#!/usr/bin/env python3
"""Guided Memory Lane v0 scripted demo.

Flow: invite -> upload (photo path + anecdote) -> trauma exclusion ->
caregiver preview -> Qloo candidates.

Modes:
    dry-run (default)  Exercises the real FastAPI app in-process against a
                       canned Qloo double. No network, no API key.
    live               Runs against a server started with the real key, e.g.
                       uvicorn app.main:app --port 8000

All printed text passes through redact(), which replaces the Qloo API key
with [REDACTED] if it ever appears.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent

DEMO: dict[str, Any] = {
    "caregiver_label": "Asha (daughter)",
    "photo_path": "/Users/pamu/Pictures/family/sunday-records-1974.jpg",
    "anecdote": (
        "Every Sunday after church, she played Nat King Cole records while "
        "making lunch for the whole family."
    ),
    "interests": ["Nat King Cole", "jazz"],
    "exclusions": [
        {
            "label": "war documentaries",
            "kind": "topic",
            "reason": "Family asked to avoid war themes.",
        }
    ],
}

_secret_values: list[str] = []


def _collect_secrets() -> None:
    """Gather candidate secret values for redaction. Never prints them."""
    env_key = os.environ.get("QLOO_API_KEY", "").strip()
    if env_key:
        _secret_values.append(env_key)
    secret_file = Path.home() / ".config" / "ax" / "secrets" / "qloo-hackathon.env"
    try:
        for line in secret_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("QLOO_API_KEY="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    _secret_values.append(value)
    except OSError:
        pass


def redact(text: str) -> str:
    for value in _secret_values:
        text = text.replace(value, "[REDACTED]")
    return text


def say(text: str = "") -> None:
    print(redact(text), flush=True)


def banner(title: str) -> None:
    say("")
    say("=" * 64)
    say(title)
    say("=" * 64)


class DemoQloo:
    """Canned Qloo double mirroring the docs-verified response shapes."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def search_tags(self, query: str) -> list[dict]:
        self.calls.append(("tags", query))
        catalog = {"jazz": [{"id": "urn:tag:genre:music:jazz", "name": "Jazz"}]}
        return catalog.get(query.strip().casefold(), [])

    def search_entities(self, query: str) -> list[dict]:
        self.calls.append(("search", query))
        catalog = {
            "nat king cole": [
                {"entity_id": "E0CD9B0B-75BB-4DAE-BCB7-79AB5DE42A0D", "name": "Nat King Cole"}
            ]
        }
        return catalog.get(query.strip().casefold(), [])

    def insights(self, entity_type, signal_tags=(), signal_entities=(), take=6):
        self.calls.append(("insights", entity_type, tuple(signal_tags), tuple(signal_entities)))
        catalog = {
            "urn:entity:artist": [
                {"entity_id": "A-1", "name": "Nat King Cole", "subtype": "urn:entity:artist", "image_url": None},
                {"entity_id": "A-2", "name": "Miles Davis", "subtype": "urn:entity:artist", "image_url": None},
            ],
            "urn:entity:movie": [
                {"entity_id": "M-1", "name": "World War II Documentary Collection", "subtype": "urn:entity:movie", "image_url": None},
                {"entity_id": "M-2", "name": "Singin' in the Rain", "subtype": "urn:entity:movie", "image_url": None},
            ],
            "urn:entity:place": [
                {"entity_id": "P-1", "name": "New Orleans Jazz Museum", "subtype": "urn:entity:place", "image_url": None},
            ],
        }
        return list(catalog.get(entity_type, []))


def _fail(step: str, response: Any) -> int:
    body = getattr(response, "text", str(response))
    say(f"FAILED at {step}: HTTP {getattr(response, 'status_code', '?')}")
    say(body[:600])
    return 1


def run_flow(http: Any) -> int:
    banner("STEP 1 of 5: invite")
    response = http.post("/invites", json={"caregiver_label": DEMO["caregiver_label"]})
    if response.status_code != 201:
        return _fail("invite", response)
    invite = response.json()
    headers = {"X-Invite-Token": invite["invite_token"]}
    say(f"session      {invite['session_id']}")
    say(f"caregiver    {invite['caregiver_label']}")
    say("invite token issued once; the demo keeps it in memory only")

    banner("STEP 2 of 5: upload (photo path + short anecdote)")
    response = http.post(
        f"/sessions/{invite['session_id']}/uploads",
        json={
            "photo_path": DEMO["photo_path"],
            "anecdote": DEMO["anecdote"],
            "interests": DEMO["interests"],
        },
        headers=headers,
    )
    if response.status_code != 201:
        return _fail("upload", response)
    upload = response.json()
    say(f"photo        {DEMO['photo_path']}")
    say(f"             ({upload['photo_note']})")
    say(f"anecdote     {DEMO['anecdote']}")
    say(f"interests    {', '.join(DEMO['interests'])} (caregiver-typed, explicit)")

    banner("STEP 3 of 5: trauma exclusions (before any Qloo call)")
    response = http.post(
        f"/sessions/{invite['session_id']}/exclusions",
        json={"exclusions": DEMO["exclusions"]},
        headers=headers,
    )
    if response.status_code != 200:
        return _fail("exclusions", response)
    for exclusion in DEMO["exclusions"]:
        say(f"excluded     {exclusion['label']} ({exclusion['kind']}) - {exclusion['reason']}")

    banner("STEP 4 of 5: caregiver preview (first Qloo touch)")
    response = http.post(f"/sessions/{invite['session_id']}/preview", headers=headers)
    if response.status_code != 200:
        return _fail("preview", response)
    preview = response.json()
    for signal in preview["signals"]:
        say(f"signal       {signal['name']}  [{signal['kind']}]  {signal['id']}")
    if preview["unresolved_interests"]:
        say(f"unresolved   {preview['unresolved_interests']} (never guessed)")
    say(f"qloo gets    only these IDs: {preview['what_qloo_receives']['signal_ids']}")
    say(f"             types: {', '.join(preview['what_qloo_receives']['insight_types'])}")
    say(f"note         {preview['what_qloo_receives']['note']}")

    banner("STEP 5 of 5: Qloo candidates (after exclusions)")
    response = http.post(f"/sessions/{invite['session_id']}/candidates", headers=headers)
    if response.status_code != 200:
        return _fail("candidates", response)
    body = response.json()
    for candidate in body["candidates"]:
        say(f"[{candidate['type']:>5}] {candidate['name']}")
        say(f"        {candidate['why']}")
    if body["removed_by_exclusions"]:
        say("")
        say("filtered out by exclusions:")
        for item in body["removed_by_exclusions"]:
            say(f"        {item['name']} (matched: {item['matched_exclusion']})")
    if body.get("types_failed"):
        say("")
        say("degraded (upstream trouble, other categories still served):")
        for item in body["types_failed"]:
            say(f"        {item['type']}: {item['error']}")

    banner("Boundary")
    say(body["boundary"])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Guided Memory Lane v0 scripted demo")
    parser.add_argument("--mode", choices=("dry-run", "live"), default="dry-run")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=90.0, help="per-request timeout for live mode")
    args = parser.parse_args()
    _collect_secrets()

    if args.mode == "dry-run":
        import sys
        import warnings

        warnings.filterwarnings(
            "ignore", message="Using `httpx` with `starlette.testclient`"
        )
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from fastapi.testclient import TestClient

        from app.main import create_app

        say("mode: dry-run (in-process app, canned Qloo double, no network, no key)")
        with tempfile.TemporaryDirectory(prefix="gml-demo-") as tmp:
            client = TestClient(
                create_app(store_path=Path(tmp) / "demo-store.json", qloo=DemoQloo())
            )
            return run_flow(client)

    import httpx

    say(f"mode: live (server at {args.base_url})")
    try:
        with httpx.Client(base_url=args.base_url, timeout=args.timeout) as client:
            return run_flow(client)
    except httpx.HTTPError as exc:
        say(f"cannot reach server: {exc.__class__.__name__}. Start it with: uvicorn app.main:app")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
