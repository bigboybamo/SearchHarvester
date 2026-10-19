"""Brave Search API (web search). The default provider.

Response shape and limits are from Brave's documentation (2026-10-19) and have **not yet been
verified against a live call**; `tests/fixtures/brave_web_search.json` is hand-written to match.
"""

import logging

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from harvester.models import SearchHit
from harvester.search.base import SearchError, get_search_json, validate_request
from harvester.text import html_to_text

logger = logging.getLogger(__name__)

NAME = "brave"
ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
PAGE_SIZE = 20
"""`count` is capped at 20 and every request is billed the same, so always ask for the most."""

MAX_PAGES = 10
"""`offset` is a 0-based page index, documented maximum 9."""


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
                    # Brave wraps matched terms in <strong> and escapes entities.
                    title=html_to_text(result.title),
                    url=result.url,
                    snippet=html_to_text(result.description),
                )
            )
        return hits
