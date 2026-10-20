"""The `SearchProvider` protocol and helpers shared by every provider.

Search is for **discovery only**: a hit such as `jobs.lever.co/acme/...` tells the harvester that
Acme uses Lever. Postings themselves come from the free ATS APIs, so every paid search should be
spent finding new boards, never re-reading known ones.
"""

import json
from typing import Protocol, runtime_checkable

import httpx

from harvester.httpclient import get_with_retry
from harvester.models import SearchHit


class SearchError(Exception):
    """A search request failed. The message never contains the API key."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@runtime_checkable
class SearchProvider(Protocol):
    """A paid web search API that supports `site:` queries."""

    name: str
    """Matches the `SEARCH_PROVIDER` value that selects it."""

    page_size: int
    """Results per page, and so per paid request."""

    max_pages: int
    """How many pages the provider will serve for one query."""

    def search(self, query: str, page: int = 0) -> list[SearchHit]:
        """One page (0-based) of organic results. Raises `SearchError`; each call is billed."""
        ...


def validate_request(provider: SearchProvider, query: str, page: int) -> str:
    """Stripped `query`, after checking `query` and `page` are usable. Raises `ValueError`."""
    text = query.strip()
    if not text:
        raise ValueError("search query is empty")
    if not 0 <= page < provider.max_pages:
        raise ValueError(f"{provider.name} serves pages 0-{provider.max_pages - 1}, not {page}")
    return text


def clean_text(text: str) -> str:
    """Whitespace-normalised text. Providers return plain text, so nothing is parsed as HTML."""
    return " ".join(text.split())


def get_search_json(
    http: httpx.Client,
    provider: str,
    url: str,
    *,
    params: dict[str, str | int],
    headers: dict[str, str] | None = None,
) -> object:
    """GET a search endpoint and decode its JSON body; any secret must be in params or headers.

    Non-2xx responses raise `SearchError` with the provider's own error detail when it has one.
    """
    try:
        response = get_with_retry(http, url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise SearchError(f"{provider} request failed: {type(exc).__name__}: {exc}") from exc

    try:
        body: object = response.json()
    except json.JSONDecodeError:
        body = None

    if not response.is_success:
        detail = _error_detail(body)
        suffix = f": {detail}" if detail else ""
        raise SearchError(
            f"{provider} returned HTTP {response.status_code}{suffix}",
            status_code=response.status_code,
        )
    if body is None:
        raise SearchError(f"{provider} returned a non-JSON body", status_code=response.status_code)
    return body


def _error_detail(body: object) -> str | None:
    """A provider's readable error: `{"error": "..."}` or `{"error": {"detail": ...}}`."""
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        detail = error.get("detail") or error.get("message") or error.get("code")
        return str(detail) if detail else None
    return None
