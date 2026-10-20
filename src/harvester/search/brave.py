"""Brave Search API (web search). The default provider.

Verified against live calls on 2026-10-20; the fixtures in `tests/fixtures/brave_*.json` are
trimmed captures. Observed: titles and descriptions are plain text (no `<strong>` highlighting
or HTML entities, despite the docs), a query with no matches omits `web` entirely, and an
invalid token is HTTP 422 `SUBSCRIPTION_TOKEN_INVALID`, not 401.
"""

import logging

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from harvester.models import SearchHit
from harvester.search.base import SearchError, clean_text, get_search_json, validate_request

logger = logging.getLogger(__name__)

NAME = "brave"
ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
PAGE_SIZE = 20
"""`count` is capped at 20 and every request is billed the same, so always ask for the most."""

MAX_PAGES = 10
"""`offset` is a 0-based page index, documented maximum 9."""

# Undocumented; found by probing on 2026-10-20. Over either limit Brave answers HTTP 422 with
# the reason in error.meta.errors[].msg. Characters are code points (600 characters taking 689
# UTF-8 bytes was accepted) and exactly 600 is allowed; words are whitespace-separated.
MAX_QUERY_CHARS = 600
MAX_QUERY_WORDS = 75


class _Result(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    url: str
    description: str = ""


class _Web(BaseModel):
    model_config = ConfigDict(extra="ignore")

    results: list[dict[str, object]] = []


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")

    web: _Web | None = None
    """Absent when the query matched nothing."""


class BraveProvider:
    name = NAME
    page_size = PAGE_SIZE
    max_pages = MAX_PAGES
    max_query_chars = MAX_QUERY_CHARS
    max_query_words = MAX_QUERY_WORDS

    def __init__(self, http: httpx.Client, api_key: str) -> None:
        self._http = http
        self._headers = {"Accept": "application/json", "X-Subscription-Token": api_key}

    def search(self, query: str, page: int = 0) -> list[SearchHit]:
        text = validate_request(self, query, page)
        body = get_search_json(
            self._http,
            NAME,
            ENDPOINT,
            params={"q": text, "count": PAGE_SIZE, "offset": page},
            headers=self._headers,
        )
        try:
            response = _Response.model_validate(body)
        except ValidationError as exc:
            raise SearchError(f"Unexpected Brave response shape: {exc}", status_code=200) from exc

        hits: list[SearchHit] = []
        for index, item in enumerate(response.web.results if response.web else []):
            try:
                result = _Result.model_validate(item)
            except ValidationError as exc:
                logger.warning("Skipping malformed Brave result: %s", exc)
                continue
            hits.append(
                SearchHit(
                    provider=NAME,
                    rank=page * PAGE_SIZE + index + 1,
                    title=clean_text(result.title),
                    url=result.url,
                    snippet=clean_text(result.description),
                )
            )
        return hits
