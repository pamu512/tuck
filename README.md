# Guided Memory Lane (v0)

Invite-first shared listening and looking sessions for families living with dementia. A caregiver uploads a family photo path plus a short anecdote, records trauma exclusions, previews exactly what will be shared, and only then receives Qloo cultural-affinity candidates (music, film, place).

Built for the Qloo Agentic Hackathon (deadline Oct 30, 2026).

## Claim boundary (locked 2026-10-13)

This project WILL:

- Comfortable shared listening and looking sessions
- Invite-first access; the family controls every step
- Caregiver in the loop with a mandatory preview before any request
- Background only: a Cochrane 2025 review reports a small effect of preferred-music listening on depression symptoms (SMD of about -0.23); cited as background for preferred music, never as an outcome of this app
- Qloo supplies cultural-affinity candidates (music, film, place) after exclusions

This project WILL NOT claim:

- Memory restoration
- Agitation reduction
- Disease treatment
- Music plus photos outperforming either alone
- Therapy-equivalent outcomes
- Biography or life story inferred from photos

These lists are enforced in code (`app/session.py`), not just documented: the claims travel with every preview and candidates response.

## Safety invariants and where they live

1. Invite-first. `POST /invites` issues a one-time token; every session route requires it (`X-Invite-Token` header). Tokens are stored as SHA-256 hashes and compared in constant time.
2. Exclusions before any Qloo call. `POST /sessions/{id}/preview` and `POST /sessions/{id}/candidates` refuse with 409 until `POST /sessions/{id}/exclusions` has run. An empty list counts as a deliberate acknowledgment; changing exclusions later invalidates the preview so it must be re-signed. Exclusions filter both the signals sent to Qloo and the candidates returned.
3. Photos are opaque. The photo path is stored as a metadata string. It is never opened, read, or analyzed; there is no vision model anywhere. `tests/test_no_invented_bio.py` sabotages `open()` for image files and proves the full flow still works.
4. Anecdotes are display-only. Anecdote text stays in the local family record and is never sent to Qloo. Qloo signals come exclusively from caregiver-typed interests. Interests that do not resolve to a Qloo tag or entity are reported as unresolved, never guessed.
5. Preview is the sign-off. Preview shows the exact signal IDs Qloo will receive, what is withheld by exclusions, and the claim boundary, before candidates are requested.
6. No biographical text is surfaced. Qloo results are mapped down to name, ID, type, image, and a reason anchored in stated interests; Qloo's own biography/description fields are dropped in `app/qloo.py`.

## Qloo integration (docs-verified 2026-10-13)

Client: `app/qloo.py`, base URL `https://hackathon.api.qloo.com`.

- `GET /search?query=...` - entity lookup by name
- `POST /v2/tags` with `{"filter": {"query": ...}}` - taxonomy search
- `GET /v2/insights` - recommendations with query-string parameters: `filter.type`, `signal.interests.tags` (repeatable), `signal.interests.entities` (repeatable), `take`

Notes verified against the live API and the hackathon developer guide:

- `filter.type` supports `urn:entity:artist` (music), `urn:entity:movie` (film), `urn:entity:place` (place). Music is `urn:entity:artist`, not `urn:entity:music`.
- The Insights endpoint is GET-only: a POST with a JSON body fails even with correct parameters. Invalid parameters are silently ignored, so the client sends only documented ones.
- The key travels only in the `X-Api-Key` header, is loaded from `QLOO_API_KEY` (env) with a fallback to `~/.config/ax/secrets/qloo-hackathon.env`, and is never committed, logged, or printed.
- One retry on 429/5xx with backoff; errors carry no key material.

## Run

    python3.11 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    uvicorn app.main:app --port 8000

The server needs the key only when a preview or candidates call is made (it returns a clean 503 otherwise).

## Demo CLI

    python demo_cli.py               # dry-run: in-process app, canned Qloo, no key, no network
    python demo_cli.py --mode live   # against a running server that holds the key

## Tests

    pytest tests/test_exclusions.py tests/test_no_invented_bio.py

No network, no API key: tests inject a canned Qloo double and a temporary store. The real client's wire behavior (GET method, query params, biography stripping) is covered through an httpx mock transport.

## API

| Route | Body | Result |
| --- | --- | --- |
| POST /invites | `{"caregiver_label"}` | session ID plus one-time invite token |
| POST /sessions/{id}/uploads | `{"photo_path", "anecdote", "interests"}` | upload receipt; photo never opened |
| POST /sessions/{id}/exclusions | `{"exclusions": [{"label", "kind", "reason"}]}` | acknowledgment (empty list allowed) |
| POST /sessions/{id}/preview | none | exact signals Qloo will receive, withheld items, claims |
| POST /sessions/{id}/candidates | none | filtered music/film/place candidates with reasons |

State machine: invite -> uploads -> exclusions acknowledged -> preview -> candidates, enforced server-side.

## Storage and secrets

- Sessions live in a local JSON file (`data/store.json`, override with `GML_STORE_PATH`). Atomic writes, lock-guarded. This is a demo store, not a production data layer: no encryption at rest, no multi-process coordination.
- `.env.example` documents the key name only. The real key lives outside the repo at `~/.config/ax/secrets/qloo-hackathon.env`.

## Scope of v0

No LLM, no vision model, no accounts, no external storage. One job: a safe, invite-first path from family memories to culturally relevant candidates.
