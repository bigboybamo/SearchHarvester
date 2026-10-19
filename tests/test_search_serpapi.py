"""Fixtures `serpapi_*_documented.json` are hand-written from SerpApi's documentation (2026-10-19),
not captured: no API key was available. Replace them with live captures on the first real call."""

import logging

import httpx
import pytest
import respx

from conftest import load_fixture
from harvester.search.base import SearchError, SearchProvider
from harvester.search.serpapi import ENDPOINT, SerpApiProvider

KEY = "serpapi-secret-key"


@pytest.fixture
def serpapi(http: httpx.Client) -> SerpApiProvider:
    return SerpApiProvider(http, KEY)


def test_serpapi_satisfies_protocol(serpapi: SerpApiProvider) -> None:
    assert isinstance(serpapi, SearchProvider)
    assert (serpapi.name, serpapi.page_size, serpapi.max_pages) == ("serpapi", 10, 10)


def test_search_request_params_include_engine_start_and_key(
    serpapi: SerpApiProvider, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(ENDPOINT).respond(
        200, content=load_fixture("serpapi_google_documented.json")
    )

    serpapi.search("site:jobs.lever.co (Python)", page=3)

    assert dict(route.calls.last.request.url.params) == {
        "engine": "google",
        "q": "site:jobs.lever.co (Python)",
        "start": "30",
        "api_key": KEY,
    }


def test_search_fixture_returns_hits(
    serpapi: SerpApiProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(ENDPOINT).respond(200, content=load_fixture("serpapi_google_documented.json"))

    hits = serpapi.search("q", page=1)

    assert [hit.rank for hit in hits] == [11, 12, 13]
    assert hits[1].provider == "serpapi"
    assert hits[1].title == "Data Engineer - Northwind Labs"
    assert hits[1].url == "https://jobs.ashbyhq.com/northwind/5a6b7c8d-9e0f-4a1b-8c2d-3e4f5a6b7c8d"
    assert hits[1].snippet == "Northwind Labs · Python, SQL · Berlin"


def test_search_no_results_message_returns_empty(
    serpapi: SerpApiProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(ENDPOINT).respond(
        200, content=load_fixture("serpapi_no_results_documented.json")
    )

    assert serpapi.search("q") == []


def test_search_other_error_on_200_raises(
    serpapi: SerpApiProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(ENDPOINT).respond(200, json={"error": "Your account has run out of searches."})

    with pytest.raises(SearchError, match="run out of searches"):
        serpapi.search("q")


def test_search_invalid_key_raises_without_leaking_key(
    serpapi: SerpApiProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(ENDPOINT).respond(
        401, content=load_fixture("serpapi_invalid_key_documented.json")
    )

    with pytest.raises(SearchError) as excinfo:
        serpapi.search("q")

    assert excinfo.value.status_code == 401
    assert "Invalid API key" in str(excinfo.value)
    assert KEY not in str(excinfo.value)


def test_search_retries_never_log_key(
    serpapi: SerpApiProvider, respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    route = respx_mock.get(ENDPOINT)
    route.side_effect = [
        httpx.Response(503),
        httpx.ConnectError("connection reset"),
        httpx.Response(200, content=load_fixture("serpapi_google_documented.json")),
    ]

    with caplog.at_level(logging.WARNING):
        serpapi.search("q")

    assert route.call_count == 3
    assert "retry" in caplog.text
    assert KEY not in caplog.text


def test_search_persistent_transport_error_raises_without_key(
    serpapi: SerpApiProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(ENDPOINT).mock(side_effect=httpx.ConnectError("dns failure"))

    with pytest.raises(SearchError) as excinfo:
        serpapi.search("q")

    assert "ConnectError" in str(excinfo.value)
    assert KEY not in str(excinfo.value)


def test_search_unexpected_shape_raises(
    serpapi: SerpApiProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(ENDPOINT).respond(200, json={"organic_results": "not a list"})

    with pytest.raises(SearchError, match="response shape"):
        serpapi.search("q")
