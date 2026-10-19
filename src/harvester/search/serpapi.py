"""SerpApi (Google results). Use it if Brave handles long `OR` queries poorly.

Response shape is from SerpApi's documentation (2026-10-19) and has **not yet been verified
against a live call**; `tests/fixtures/serpapi_google.json` is hand-written to match.

SerpApi only accepts the key as the `api_key` query parameter, so it is passed through
`get_with_retry(params=...)`, which never logs parameters.
"""

import logging

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from harvester.models import SearchHit
from harvester.search.base import SearchError, get_search_json, validate_request
from harvester.text import html_to_text

logger = logging.getLogger(__name__)

NAME = "serpapi"
ENDPOINT = "https://serpapi.com/search"
PAGE_SIZE = 10
"""Google's page size; `start` advances in steps of 10."""

MAX_PAGES = 10

# Unverified: SerpApi reports an empty result set as HTTP 200 with this message in `error`,
# rather than as an empty `organic_results` list. Confirm on the first live run.
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
                    title=result.title.strip(),
                    url=result.link,
                    snippet=html_to_text(result.snippet),
                )
            )
        return hits
