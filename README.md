# Tuck

Tuck asks one question at a time, then makes a private listening gift.

You make an account (email and a password), say who you are helping, and answer Tuck. Tuck keeps those answers as you typed them. Exclusions and a preview come before any request, and again before a private Spotify playlist. If Spotify is not connected, the gift is a listening file and search links.

Built for the Qloo Agentic Hackathon (deadline Oct 30, 2026). The live guide is the site root: [guided-memory-lane.onrender.com](https://guided-memory-lane.onrender.com).

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

1. Account first, then the invite token. `POST /accounts` and `POST /login` set an HttpOnly cookie signed with `SESSION_SECRET` (environment or the local secrets file; the value is never committed or printed). `POST /invites` requires that cookie. Session routes still require the one-time `X-Invite-Token`. `POST /dev/sample` is the only session without an account, and it only loads the canned sample family. The sample button sits under Advanced.
2. Exclusions before any recommendation. `POST /sessions/{id}/preview` and `POST /sessions/{id}/candidates` refuse with 409 until `POST /sessions/{id}/exclusions` has run. An empty list counts as a deliberate acknowledgment; changing exclusions later invalidates the preview so it must be re-signed. Acknowledgment tries to resolve each label to a Qloo tag or entity id (`search_tags` / `search_entities`) and filters on that id when it hits. Labels that do not resolve are returned in `unresolved_exclusions` and still filter by name, which can miss paraphrases. Exclusions filter both the signals sent to Qloo and the candidates returned.
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

Open `http://127.0.0.1:8000/`. Tuck asks for an email, then one question at a time. The sample family is under Advanced, and it stops at the preview until you ask for the list.

Preview and candidates need the key (503 if it is missing). Exclusion acknowledgment also tries Qloo; if that lookup fails, the labels are still saved and listed in `unresolved_exclusions`.

On the first screen, **Open the sample family** runs one invite, one memory, one exclusion, the preview, the list, and the playlist export. With a canned or healthy Qloo response it finishes well under a minute. Use the step rail to reopen Preview: that sheet is the claim boundary.

## Spotify

A private playlist is created only after exclusions are acknowledged and the preview is signed. Music candidates are resolved with Spotify search. Before any track is added, the artist, title, and album are checked again with the same exclusion matcher. The playlist is private (`playlist-modify-private` only; the public scope is not requested). The description is `Shared listening for {family label}.` It does not retell the anecdote or make an outcome claim.

The browser step is Authorization Code with PKCE. No client secret is used or stored. Set `SPOTIFY_CLIENT_ID` and `SPOTIFY_REDIRECT_URI` (see `.env.example`). Register that exact redirect URI in the Spotify app. On Render, set:

`SPOTIFY_REDIRECT_URI=https://guided-memory-lane.onrender.com/spotify/callback`

If Spotify is not connected, the guide does not dead-end. It downloads a listening file and offers Spotify search links for the music names that cleared the exclusions. YouTube Music, Apple Music, and Spotify Connect are not in this build.

`SPOTIFY_CLIENT_ID` is set in the server environment when a private playlist should be available. Do not commit it.

## Demo CLI

    python demo_cli.py               # dry-run: in-process app, canned Qloo, no key, no network
    python demo_cli.py --mode live   # against a running server that holds the key

Dry-run leaves Spotify unset, so the listening file is the gift. Live mode prints whichever export the server is configured for. The private playlist itself is finished in the browser, because Spotify has to send the caregiver back to `/spotify/callback`.

## Tests

    pytest tests/

No network, no API key: tests inject a canned Qloo double and a temporary store. The real client's wire behavior (GET method, query params, biography stripping) is covered through an httpx mock transport.

## API

| Route | Body | Result |
| --- | --- | --- |
| POST /accounts | `{"email", "password"}` | account cookie; password is stored as a hash |
| POST /login | `{"email", "password"}` | account cookie |
| POST /me/profile | `{"person_name"}` | durable name of the person you are helping |
| POST /invites | `{"caregiver_label"}` | session ID plus one-time invite token; requires the account cookie |
| POST /sessions/{id}/uploads | `{"photo_path", "anecdote", "interests"}` | upload receipt; photo never opened |
| POST /sessions/{id}/exclusions | `{"exclusions": [{"label", "kind", "reason"}]}` | acknowledgment (empty list allowed); unresolved labels are listed |
| POST /sessions/{id}/preview | none | exact signals Qloo will receive, withheld items, claims |
| GET / | none | caregiver desk |
| GET /sample-family | none | the judge-demo family (not a session) |
| POST /sessions/{id}/guide | `{"intent": "status" or "candidates"}` | next step; candidates are refused until the preview is signed |
| POST /sessions/{id}/candidates | none | filtered music/film/place candidates; anecdote and photo path are then cleared |
| GET /sessions/{id}/export | none | playlist plan: tracks, withheld names, M3U-or-Spotify mode |
| GET /sessions/{id}/playlist.m3u | none | M3U download (invite header, not a query token) |
| POST /sessions/{id}/spotify/start | none | Spotify authorize URL, or 409 with `fallback: m3u` when Spotify is not connected |
| GET /spotify/callback | Spotify redirect | exchanges the code and creates the private playlist |
| DELETE /sessions/{id} | none | delete the session and its anecdotes before completion |

State machine: invite, uploads, exclusions acknowledged, preview, candidates, playlist export. The guide (`app/agent.py`) is the order check: it resolves caregiver-typed interests and exclusion labels through Qloo, and it refuses candidates until the preview is signed. Anecdote text and photo paths are never put in a tool payload.

A successful candidates response seals the session. The list, the exclusions, and the family label stay so the playlist can re-check them. The anecdote and the photo path are cleared. `DELETE /sessions/{id}` removes the rest. Both require `X-Invite-Token`.

## Public demo retention

Sessions are anecdotes and photo paths, so the public demo does not keep them indefinitely.

- TTL is 24 hours (`GML_SESSION_TTL_SECONDS`, positive integer). An expired session is deleted on the next read or when a new invite is created, and then answers 404.
- At most 200 sessions are stored (`GML_MAX_SESSIONS`). A new invite past that cap returns 429. Expired rows are dropped first and do not consume the cap.
- `POST /sessions/{id}/candidates` clears the anecdote and photo path after a successful response and keeps a sealed export for the playlist. A 409 or 502 leaves the family text in place so the caregiver can fix the request. `DELETE /sessions/{id}` removes the session sooner.

There is no per-IP rate limit on `POST /invites`. The cap and TTL bound how much the file can grow.

## Storage and secrets

- Sessions live in a local JSON file (`data/store.json`, override with `GML_STORE_PATH`). Writes are atomic. Each session's load → mutate → put runs under a stripe lock, so overlapping uploads or exclusion updates on that session cannot drop a write. This is a demo store, not a production data layer: no encryption at rest, no multi-process coordination.
- `.env.example` documents the key name only. The real key lives outside the repo at `~/.config/ax/secrets/qloo-hackathon.env`.

## Scope

No vision model and no invented biography. One job: a safe, invite-first path from a family memory to a list, then to a private playlist or an M3U. YouTube Music and Apple Music are deferred.

## Deploy on Render

Blueprint: `render.yaml`.

- Build: `pip install -r requirements.txt`
- Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

Set `QLOO_API_KEY` in the Render Dashboard. The blueprint names the variable only; never commit the value.

`SESSION_SECRET`, `SPOTIFY_CLIENT_ID`, and `SPOTIFY_REDIRECT_URI` come from the environment. Without Spotify configured, `/` still serves Tuck and the listening file is the gift. The redirect URI must match the Spotify app exactly, including the path `/spotify/callback`.
