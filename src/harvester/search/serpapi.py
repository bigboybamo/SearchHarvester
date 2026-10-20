"""SerpApi (Google results). Use it if Brave handles long `OR` queries poorly.

Verified against live calls on 2026-10-20; the fixtures in `tests/fixtures/serpapi_*.json` are
trimmed captures. Observed: snippets are plain text, and an invalid key is HTTP 401 with an
`error` string. When a `site:` query matches nothing, Google silently drops the `site:`
restriction and returns unrelated pages (`serpapi_site_ignored.json`), so hits must never be
trusted to be on the requested site; discovery keeps only URLs an `AtsClient` recognises.

SerpApi only accepts the key as the `api_key` query parameter, so it is passed through
`get_with_retry(params=...)`, which never logs parameters.
"""

import logging

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from harvester.models import SearchHit
from harvester.search.base import SearchError, clean_text, get_search_json, validate_request

logger = logging.getLogger(__name__)

NAME = "serpapi"
ENDPOINT = "https://serpapi.com/search"
PAGE_SIZE = 10
"""Google's page size; `start` advances in steps of 10."""

MAX_PAGES = 10

# SerpApi documents an empty result set as HTTP 200 with this message in `error`. Not seen live
# yet: a no-match `site:` query returned off-site results instead (see module docstring).
NO_RESULTS_MESSAGE = "hasn't returned any results"


class _Result(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    link: str
    snippet: str = ""


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")

    organic_results: list[dict[str, object]] = []
    error: str | None = None


class SerpApiProvider:
    name = NAME
    page_size = PAGE_SIZE
    max_pages = MAX_PAGES

    def __init__(self, http: httpx.Client, api_key: str) -> None:
        self._http = http
        self._api_key = api_key

    def search(self, query: str, page: int = 0) -> list[SearchHit]:
        text = validate_request(self, query, page)
        body = get_search_json(
            self._http,
            NAME,
            ENDPOINT,
            params={
                "engine": "google",
                "q": text,
                "start": page * PAGE_SIZE,
                "api_key": self._api_key,
            },
        )
        try:
            response = _Response.model_validate(body)
        except ValidationError as exc:
            raise SearchError(f"Unexpected SerpApi response shape: {exc}", status_code=200) from exc

        if response.error is not None:
            if NO_RESULTS_MESSAGE in response.error:
                return []
            raise SearchError(f"SerpApi error: {response.error}", status_code=200)

        hits: list[SearchHit] = []
        for index, item in enumerate(response.organic_results):
            try:
                result = _Result.model_validate(item)
            except ValidationError as exc:
                logger.warning("Skipping malformed SerpApi result: %s", exc)
                continue
            hits.append(
                SearchHit(
                    provider=NAME,
                    rank=page * PAGE_SIZE + index + 1,
                    title=clean_text(result.title),
                    url=result.link,
                    snippet=clean_text(result.snippet),
                )
            )
        return hits
