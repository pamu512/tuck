"""Qloo hackathon API client (docs-verified 2026-10-13).

Endpoints used by Guided Memory Lane:
    GET  /search       entity lookup by name
    POST /v2/tags      taxonomy search, body {"filter": {"query": ...}}
    GET  /v2/insights  recommendations, parameters in the query string

The Insights endpoint is GET-only: per the hackathon developer guide, POST
with a JSON body fails even when the parameters are correct. filter.type
must be a single supported URN. signal.interests.tags must be a single
comma-joined query parameter (repeated tags params 500 on the hackathon
API); signal.interests.entities may be repeated.

The API key travels only in the X-Api-Key header. It is never logged,
embedded in error messages, or returned to callers.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://hackathon.api.qloo.com"
DEFAULT_SECRET_FILE = Path.home() / ".config" / "ax" / "secrets" / "qloo-hackathon.env"

#: Demo-facing category labels mapped to supported filter.type URNs.
INSIGHT_TYPES: dict[str, str] = {
    "music": "urn:entity:artist",
    "film": "urn:entity:movie",
    "place": "urn:entity:place",
}

#: Reverse mapping used for display labels.
TYPE_LABELS: dict[str, str] = {
    "urn:entity:artist": "music",
    "urn:entity:movie": "film",
    "urn:entity:place": "place",
}


class QlooError(RuntimeError):
    """Base error for Qloo client failures. Never contains the API key."""


class QlooAuthError(QlooError):
    """The API key is missing or was rejected."""


class QlooUpstreamError(QlooError):
    """Qloo returned an unexpected or failed response."""


def load_api_key(secret_file: str | Path | None = None) -> str | None:
    """Return QLOO_API_KEY from the environment, or from a KEY=VALUE file.

    The fallback file (default ~/.config/ax/secrets/qloo-hackathon.env) is
    only read; its value is returned to the caller and never printed.
    """
    key = os.environ.get("QLOO_API_KEY", "").strip()
    if key:
        return key
    path = Path(secret_file) if secret_file else DEFAULT_SECRET_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.strip().startswith("QLOO_API_KEY="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            return value or None
    return None


class QlooClient:
    """Thin httpx wrapper for the three endpoints used by the demo."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 40.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        resolved_key = (api_key or "").strip() or load_api_key()
        if not resolved_key:
            raise QlooAuthError(
                "QLOO_API_KEY is not set. Export it or place it in "
                f"{DEFAULT_SECRET_FILE} (see README)."
            )
        self.api_key: str = resolved_key
        self.base_url: str = (
            base_url or os.environ.get("QLOO_API_BASE") or DEFAULT_BASE_URL
        ).rstrip("/")
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"X-Api-Key": self.api_key},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: list[tuple[str, str]] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """One request with a single retry on timeout/429/5xx. Key-safe errors."""
        last_error = "no attempt made"
        for attempt in (1, 2):
            try:
                response = self._http.request(
                    method,
                    path,
                    params=httpx.QueryParams(tuple(params)) if params else None,
                    json=json_body,
                )
            except httpx.TimeoutException as exc:
                last_error = f"network error: {exc.__class__.__name__}"
                if attempt == 1:
                    time.sleep(1.2)
                continue
            except httpx.HTTPError as exc:
                last_error = f"network error: {exc.__class__.__name__}"
                if attempt == 1:
                    time.sleep(0.5)
                continue
            if response.status_code in (429, 502, 503, 504) and attempt == 1:
                last_error = f"HTTP {response.status_code}"
                time.sleep(0.8)
                continue
            if response.status_code == 401:
                raise QlooAuthError("Qloo rejected the API key (HTTP 401).")
            if response.status_code >= 400:
                detail = response.text[:200].replace(self.api_key, "[REDACTED]")
                raise QlooUpstreamError(
                    f"{method} {path} failed: HTTP {response.status_code}: {detail}"
                )
            return response.json()
        raise QlooUpstreamError(f"{method} {path} failed after retry: {last_error}")

    def search_tags(self, query: str) -> list[dict[str, Any]]:
        """POST /v2/tags: search the taxonomy. Returns [{"id", "name"}, ...]."""
        data = self._request("POST", "/v2/tags", json_body={"filter": {"query": query}})
        tags = (data.get("results") or {}).get("tags") or []
        return [{"id": t.get("id"), "name": t.get("name")} for t in tags if t.get("id")]

    def search_entities(self, query: str) -> list[dict[str, Any]]:
        """GET /search: look up entity IDs by name. Returns [{"entity_id", "name"}, ...]."""
        data = self._request("GET", "/search", params=[("query", query)])
        results = data.get("results") or []
        return [
            {"entity_id": r.get("entity_id"), "name": r.get("name")}
            for r in results
            if r.get("entity_id")
        ]

    def insights(
        self,
        entity_type: str,
        signal_tags: list[str] | tuple[str, ...] = (),
        signal_entities: list[str] | tuple[str, ...] = (),
        take: int = 6,
    ) -> list[dict[str, Any]]:
        """GET /v2/insights: type-filtered cultural-affinity candidates.

        Returns mapped entities with safe display fields only. Qloo's
        biography/description properties are deliberately dropped: Guided
        Memory Lane never surfaces biographical text.
        """
        if entity_type not in TYPE_LABELS:
            raise QlooError(f"unsupported insight type: {entity_type}")
        params: list[tuple[str, str]] = [("filter.type", entity_type), ("take", str(take))]
        # Hackathon API: repeated signal.interests.tags params 500 with
        # TypeError on 'in' for 'tag'; one comma-joined value is required.
        if signal_tags:
            params.append(("signal.interests.tags", ",".join(signal_tags)))
        params += [("signal.interests.entities", entity) for entity in signal_entities]
        data = self._request("GET", "/v2/insights", params=params)
        entities = (data.get("results") or {}).get("entities") or []
        candidates = []
        for entity in entities:
            if not entity.get("entity_id") or not entity.get("name"):
                continue
            image = (entity.get("properties") or {}).get("image") or {}
            candidates.append(
                {
                    "entity_id": entity["entity_id"],
                    "name": entity["name"],
                    "subtype": entity.get("subtype") or entity_type,
                    "image_url": image.get("url"),
                }
            )
        return candidates
