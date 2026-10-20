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


class QueryTooLongError(SearchError):
    """The query breaks the provider's limits. Raised before any request, so nothing is billed."""


@runtime_checkable
class SearchProvider(Protocol):
    """A paid web search API that supports `site:` queries."""

    name: str
    """Matches the `SEARCH_PROVIDER` value that selects it."""

    page_size: int
    """Results per page, and so per paid request."""

    max_pages: int
    """How many pages the provider will serve for one query."""

    max_query_chars: int | None
    """Longest query accepted, in characters (not bytes); `None` if the provider sets no limit."""

    max_query_words: int | None
    """Most whitespace-separated words accepted (`site:x`, `OR`, `-"a"` each count); or `None`."""

    def search(self, query: str, page: int = 0) -> list[SearchHit]:
        """One page (0-based) of organic results. Raises `SearchError`; each call is billed."""
        ...


def validate_request(provider: SearchProvider, query: str, page: int) -> str:
    """Stripped `query`, after checking it and `page` are usable.

    Raises `ValueError` for an empty query or a page out of range, and `QueryTooLongError` when
    the query breaks the provider's limits, so an over-long query never costs a request.
    """
    text = query.strip()
    if not text:
        raise ValueError("search query is empty")
    if not 0 <= page < provider.max_pages:
        raise ValueError(f"{provider.name} serves pages 0-{provider.max_pages - 1}, not {page}")
    chars, words = len(text), len(text.split())
    if provider.max_query_chars is not None and chars > provider.max_query_chars:
        raise QueryTooLongError(
            f"{provider.name} accepts at most {provider.max_query_chars} characters; "
            f"this query has {chars}"
        )
    if provider.max_query_words is not None and words > provider.max_query_words:
        raise QueryTooLongError(
            f"{provider.name} accepts at most {provider.max_query_words} words; "
            f"this query has {words}"
        )
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
        # Brave puts the specific reason in meta.errors[].msg ("... at most 600 characters ...")
        # and only a generic sentence in detail.
        meta = error.get("meta")
        reasons = meta.get("errors") if isinstance(meta, dict) else None
        if isinstance(reasons, list):
            messages = [str(r["msg"]) for r in reasons if isinstance(r, dict) and r.get("msg")]
            if messages:
                return "; ".join(messages)
        detail = error.get("detail") or error.get("message") or error.get("code")
        return str(detail) if detail else None
    return None
