# Plan: Guided Memory Lane v0 (Qloo Agentic Hackathon)

## Goal
Ship a live agentic demo where families add photos + short anecdotes; caregiver preview + trauma exclusions run before Qloo; Qloo supplies cultural-affinity candidates (music/film/place) after exclusions. Invite-first. No invented biography from photos.

## Deadline
Devpost Qloo Agentic Hackathon: Oct 30, 2026 @ 11:45pm EDT.

## Claims (locked 2026-10-13)
WILL: comfortable shared listening/looking; invite-first; caregiver in the loop; Cochrane 2025 depression SMD ≈ −0.23 as background for preferred music only; Qloo = cultural affinity after exclusions.
WON'T: memory restoration; agitation reduction; disease treatment; music+photo superiority; therapy-equivalent outcomes; biography inferred from photos.

## Stack
- New sibling repo: ~/Documents/GitHub/guided-memory-lane (NOT inside amd mc3-starter-kit)
- FastAPI + httpx to https://hackathon.api.qloo.com
- Local JSON store for invite, exclusions, anecdote, photo metadata
- CLI demo (`demo_cli.py`); no LLM recommender; no vision model
- Key from ~/.config/ax/secrets/qloo-hackathon.env (never commit)

## Qloo endpoints (docs-verified)
- POST /v2/insights (primary; type-filtered recommendations from entity interests)
- GET/POST /v2/tags (taxonomy)

## Local routes
- POST /invites
- POST /sessions/{id}/uploads
- POST /sessions/{id}/exclusions
- POST /sessions/{id}/preview
- POST /sessions/{id}/candidates

## File list
README.md, .env.example, requirements.txt, app/main.py, app/qloo.py, app/session.py, app/store.py, demo_cli.py, tests/test_exclusions.py, tests/test_no_invented_bio.py

## Milestone 1
Scripted demo: create invite → upload one photo path + one anecdote → set one exclusion → caregiver preview → Qloo candidates that omit excluded entity and contain no photo-derived life story.

## Evidence
pytest exclusions + no-invented-bio; CLI demo output; curl smoke of /v2/insights with key loaded from env (do not print key).
