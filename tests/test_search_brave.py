"""Fixtures are trimmed live captures (2026-10-20): `brave_web_search.json` is the first five
results for `site:jobs.lever.co "python"`, `brave_no_results.json` a query matching nothing, and
`brave_invalid_token.json` the response to a deliberately invalid token."""

import httpx
import pytest
import respx

from conftest import load_fixture
from harvester.search.base import SearchError, SearchProvider
from harvester.search.brave import ENDPOINT, BraveProvider

KEY = "brave-test-key"


@pytest.fixture
def brave(http: httpx.Client) -> BraveProvider:
    return BraveProvider(http, KEY)


def test_brave_satisfies_protocol(brave: BraveProvider) -> None:
    assert isinstance(brave, SearchProvider)
    assert (brave.name, brave.page_size, brave.max_pages) == ("brave", 20, 10)


def test_search_request_sends_token_header_and_paging(
    brave: BraveProvider, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(ENDPOINT).respond(200, content=load_fixture("brave_web_search.json"))

    brave.search('  site:jobs.lever.co "python"  ', page=2)

    request = route.calls.last.request
    assert request.headers["X-Subscription-Token"] == KEY
    assert request.headers["Accept"] == "application/json"
    assert dict(request.url.params) == {
        "q": 'site:jobs.lever.co "python"',
        "count": "20",
        "offset": "2",
    }


def test_search_fixture_returns_hits(brave: BraveProvider, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(ENDPOINT).respond(200, content=load_fixture("brave_web_search.json"))

    hits = brave.search("q")

    assert [hit.rank for hit in hits] == [1, 2, 3, 4, 5]
    assert [hit.url.split("/")[3] for hit in hits] == [
        "betr",
        "muttdata",
        "jobgether",
        "trackmind",
        "bluelightconsulting",
    ]
    first = hits[0]
    assert first.provider == "brave"
    assert first.title == "Betr - Senior Backend Engineer (PHP/Python)"
    assert first.url == "https://jobs.lever.co/betr/fc7cd746-f746-43d5-89b1-fe92bf508a68"
    assert first.snippet.startswith("The Betr team is seeking a highly skilled Senior Backend")


def test_search_text_with_angle_brackets_kept_verbatim(
    brave: BraveProvider, respx_mock: respx.MockRouter
) -> None:
    # Brave returns plain text, so `<` must not be treated as the start of an HTML tag.
    body = {
        "web": {
            "results": [{"title": "C<b>++  dev", "url": "https://x.test", "description": "a\n b"}]
        }
    }
    respx_mock.get(ENDPOINT).respond(200, json=body)

    hit = brave.search("q")[0]

    assert hit.title == "C<b>++ dev"
    assert hit.snippet == "a b"


def test_search_second_page_ranks_continue(
    brave: BraveProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(ENDPOINT).respond(200, content=load_fixture("brave_web_search.json"))

    assert brave.search("q", page=1)[0].rank == 21


def test_search_no_results_returns_empty(
    brave: BraveProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(ENDPOINT).respond(200, content=load_fixture("brave_no_results.json"))

    assert brave.search("q") == []


def test_search_malformed_result_skipped(
    brave: BraveProvider, respx_mock: respx.MockRouter
) -> None:
    body = {"web": {"results": [{"title": "No URL"}, {"title": "Kept", "url": "https://x.test"}]}}
    respx_mock.get(ENDPOINT).respond(200, json=body)

    hits = brave.search("q")

    assert [hit.title for hit in hits] == ["Kept"]
    assert hits[0].rank == 2


def test_search_invalid_token_raises_with_detail_and_no_key(
    brave: BraveProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(ENDPOINT).respond(422, content=load_fixture("brave_invalid_token.json"))

    with pytest.raises(SearchError) as excinfo:
        brave.search("q")

    assert excinfo.value.status_code == 422
    assert (
        str(excinfo.value) == "brave returned HTTP 422: The provided subscription token is invalid."
    )
    assert KEY not in str(excinfo.value)


def test_search_non_json_body_raises(brave: BraveProvider, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(ENDPOINT).respond(200, html="<html>maintenance</html>")

    with pytest.raises(SearchError, match="non-JSON"):
        brave.search("q")


def test_search_rate_limited_then_ok_retried(
    brave: BraveProvider, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(ENDPOINT)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(200, content=load_fixture("brave_web_search.json")),
    ]

    assert len(brave.search("q")) == 5
    assert route.call_count == 2


@pytest.mark.parametrize(("query", "page"), [("", 0), ("   ", 0), ("q", -1), ("q", 10)])
def test_search_invalid_request_raises_without_calling_api(
    brave: BraveProvider, query: str, page: int
) -> None:
    with pytest.raises(ValueError):
        brave.search(query, page)
