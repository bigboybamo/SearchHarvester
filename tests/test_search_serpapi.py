"""Fixtures are trimmed live captures (2026-10-20): `serpapi_google.json` is the first five
results for `site:jobs.lever.co "python"`; `serpapi_site_ignored.json` is a `site:` query that
matched nothing, where Google dropped the `site:` restriction and returned unrelated pages;
`serpapi_invalid_key.json` is the response to a deliberately invalid key."""

import logging

import httpx
import pytest
import respx

from conftest import load_fixture
from harvester.ats.registry import default_registry
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
    route = respx_mock.get(ENDPOINT).respond(200, content=load_fixture("serpapi_google.json"))

    serpapi.search('site:jobs.lever.co "python"', page=3)

    assert dict(route.calls.last.request.url.params) == {
        "engine": "google",
        "q": 'site:jobs.lever.co "python"',
        "start": "30",
        "api_key": KEY,
    }


def test_search_fixture_returns_hits(
    serpapi: SerpApiProvider, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(ENDPOINT).respond(200, content=load_fixture("serpapi_google.json"))

    hits = serpapi.search("q", page=1)

    assert [hit.rank for hit in hits] == [11, 12, 13, 14, 15]
    first = hits[0]
    assert first.provider == "serpapi"
    assert first.title == "Django/Python Web Developer - National Journal"
    assert first.url == (
        "https://jobs.lever.co/nationaljournal/b3f4c087-bc7a-4d8c-80a8-dc481b8d0a48"
    )
    assert first.snippet.startswith("You are an experienced web developer")


def test_search_fixture_boards_keep_canonical_lever_case(
    serpapi: SerpApiProvider, respx_mock: respx.MockRouter, http: httpx.Client
) -> None:
    # Live result #4 is jobs.lever.co/SimbeRobotics/...; Lever 404s on the lower-cased slug.
    respx_mock.get(ENDPOINT).respond(200, content=load_fixture("serpapi_google.json"))
    registry = default_registry(http)

    slugs = [resolved[1] for hit in serpapi.search("q") if (resolved := registry.resolve(hit.url))]

    assert slugs == ["nationaljournal", "artera", "egen", "SimbeRobotics", "capital"]


def test_search_site_ignored_off_site_hits_never_become_boards(
    serpapi: SerpApiProvider, respx_mock: respx.MockRouter, http: httpx.Client
) -> None:
    respx_mock.get(ENDPOINT).respond(200, content=load_fixture("serpapi_site_ignored.json"))
    registry = default_registry(http)

    hits = serpapi.search('site:jobs.lever.co "zzqx7-no-such-term-8vkq"')

    assert len(hits) == 3
    assert not any("lever.co" in hit.url for hit in hits)
    assert [registry.resolve(hit.url) for hit in hits] == [None, None, None]


def test_search_documented_no_results_message_returns_empty(
    serpapi: SerpApiProvider, respx_mock: respx.MockRouter
) -> None:
    # Documented by SerpApi but not yet observed live, hence an inline body, not a fixture.
    body = {"error": "Google hasn't returned any results for this query."}
    respx_mock.get(ENDPOINT).respond(200, json=body)

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
    respx_mock.get(ENDPOINT).respond(401, content=load_fixture("serpapi_invalid_key.json"))

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
        httpx.Response(200, content=load_fixture("serpapi_google.json")),
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


def test_search_long_query_sent_without_local_limit(
    serpapi: SerpApiProvider, respx_mock: respx.MockRouter
) -> None:
    # Live: SerpApi accepted a 643-character, 73-word query that Brave rejects.
    query = "site:jobs.lever.co " + " ".join(["python"] * 120)
    route = respx_mock.get(ENDPOINT).respond(200, content=load_fixture("serpapi_google.json"))

    serpapi.search(query)

    assert route.called
    assert (serpapi.max_query_chars, serpapi.max_query_words) == (None, None)
