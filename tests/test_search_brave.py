"""Fixtures `brave_*_documented.json` are hand-written from Brave's documentation (2026-10-19), not
captured: no API key was available. Replace them with live captures on the first real call."""

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
    route = respx_mock.get(ENDPOINT).respond(
        200, content=load_fixture("brave_web_search_documented.json")
    )

    brave.search("  site:jobs.lever.co (Python)  ", page=2)

    request = route.calls.last.request
    assert request.headers["X-Subscription-Token"] == KEY
    assert request.headers["Accept"] == "application/json"
    assert dict(request.url.params) == {
        "q": "site:jobs.lever.co (Python)",
        "count": "20",
        "offset": "2",
    }


def test_search_fixture_returns_hits_with_html_stripped(
    brave: BraveProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(ENDPOINT).respond(200, content=load_fixture("brave_web_search_documented.json"))

    hits = brave.search("q")

    assert [hit.rank for hit in hits] == [1, 2, 3, 4]
    first = hits[0]
    assert first.provider == "brave"
    assert first.title == "Acme Robotics - Senior Python Engineer"
    assert first.url == "https://jobs.lever.co/acme-robotics/0b1f2c3d-4e5f-6a7b-8c9d-0e1f2a3b4c5d"
    assert (
        first.snippet == "Acme Robotics is hiring a Senior Python Engineer & mentor. Remote (EU)."
    )
    assert hits[2].snippet == ""


def test_search_second_page_ranks_continue(
    brave: BraveProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(ENDPOINT).respond(200, content=load_fixture("brave_web_search_documented.json"))

    assert brave.search("q", page=1)[0].rank == 21


def test_search_no_web_section_returns_empty(
    brave: BraveProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(ENDPOINT).respond(200, json={"type": "search", "query": {"original": "q"}})

    assert brave.search("q") == []


def test_search_malformed_result_skipped(
    brave: BraveProvider, respx_mock: respx.MockRouter
) -> None:
    body = {"web": {"results": [{"title": "No URL"}, {"title": "Kept", "url": "https://x.test"}]}}
    respx_mock.get(ENDPOINT).respond(200, json=body)

    hits = brave.search("q")

    assert [hit.title for hit in hits] == ["Kept"]
    assert hits[0].rank == 2


def test_search_validation_error_raises_with_detail(
    brave: BraveProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(ENDPOINT).respond(422, content=load_fixture("brave_error_documented.json"))

    with pytest.raises(SearchError) as excinfo:
        brave.search("q")

    assert excinfo.value.status_code == 422
    assert str(excinfo.value) == (
        "brave returned HTTP 422: Unable to validate request parameter(s)"
    )


def test_search_unauthorised_error_never_contains_key(
    brave: BraveProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(ENDPOINT).respond(401, json={"type": "ErrorResponse", "error": {"code": "AUTH"}})

    with pytest.raises(SearchError) as excinfo:
        brave.search("q")

    assert excinfo.value.status_code == 401
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
        httpx.Response(200, content=load_fixture("brave_web_search_documented.json")),
    ]

    assert len(brave.search("q")) == 4
    assert route.call_count == 2


@pytest.mark.parametrize(("query", "page"), [("", 0), ("   ", 0), ("q", -1), ("q", 10)])
def test_search_invalid_request_raises_without_calling_api(
    brave: BraveProvider, query: str, page: int
) -> None:
    with pytest.raises(ValueError):
        brave.search(query, page)
