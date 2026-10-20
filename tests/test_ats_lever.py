"""Fixtures: `lever_postings.json` is the first three postings of the live `spotify` board
(2026-10-19); `lever_not_found.json` is the live 404 body."""

from datetime import UTC, datetime

import httpx
import pytest
import respx

from conftest import load_fixture
from harvester.ats.base import AtsError
from harvester.ats.lever import LeverClient

POSTINGS_URL = "https://api.lever.co/v0/postings/spotify"


@pytest.fixture
def client(http: httpx.Client) -> LeverClient:
    return LeverClient(http)


# --- matches -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.lever.co/spotify",
        "https://jobs.lever.co/spotify/223f9722-3579-4ff2-b123-e4cc7b0c1a64/apply",
        "jobs.lever.co/spotify",
        "https://JOBS.LEVER.CO/spotify",
    ],
)
def test_matches_board_host_returns_true(client: LeverClient, url: str) -> None:
    assert client.matches(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://lever.co/spotify",
        "https://api.lever.co/v0/postings/spotify",
        "https://jobs.lever.co.evil.example/spotify",
        "https://job-boards.greenhouse.io/spotify",
    ],
)
def test_matches_other_hosts_returns_false(client: LeverClient, url: str) -> None:
    assert not client.matches(url)


# --- parse_url ---------------------------------------------------------------------------------


def test_parse_url_board_root_returns_slug(client: LeverClient) -> None:
    assert client.parse_url("https://jobs.lever.co/spotify") == "spotify"


def test_parse_url_posting_and_apply_pages_return_slug(client: LeverClient) -> None:
    posting = "https://jobs.lever.co/spotify/223f9722-3579-4ff2-b123-e4cc7b0c1a64"

    assert client.parse_url(posting) == "spotify"
    assert client.parse_url(f"{posting}/apply?lever-source=x") == "spotify"


def test_parse_url_hyphenated_slug_returns_slug(client: LeverClient) -> None:
    assert client.parse_url("jobs.lever.co/acme-corp/") == "acme-corp"


def test_parse_url_mixed_case_slug_keeps_canonical_case(client: LeverClient) -> None:
    # Lever slugs are case-sensitive with one canonical spelling: on the live API
    # /SimbeRobotics is a 200 while /simberobotics is a 404 (and /Spotify a 404).
    assert client.parse_url("https://jobs.lever.co/SimbeRobotics/bffe760d") == "SimbeRobotics"


def test_fetch_jobs_mixed_case_slug_requested_verbatim(
    client: LeverClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get("https://api.lever.co/v0/postings/SimbeRobotics").respond(200, json=[])

    assert client.fetch_jobs("SimbeRobotics") == []
    assert route.called


@pytest.mark.parametrize(
    "url",
    ["https://jobs.lever.co", "https://jobs.lever.co/", "https://lever.co/spotify"],
)
def test_parse_url_no_usable_slug_returns_none(client: LeverClient, url: str) -> None:
    assert client.parse_url(url) is None


# --- fetch_jobs --------------------------------------------------------------------------------


def test_fetch_jobs_fixture_returns_postings(
    client: LeverClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(POSTINGS_URL, params={"mode": "json"}).respond(
        200, content=load_fixture("lever_postings.json")
    )

    postings = client.fetch_jobs("spotify")

    assert route.called
    assert [p.ats_job_id for p in postings] == [
        "223f9722-3579-4ff2-b123-e4cc7b0c1a64",
        "d87833d0-fb78-4794-b45d-3fe5c8274bc8",
        "77526d53-3811-4336-9c0d-72f6ac0242c7",
    ]
    first = postings[0]
    assert first.ats_name == "Lever"
    assert first.company_slug == "spotify"
    assert first.title == "Art Director - Mexico"
    assert first.location == "Mexico City"
    assert first.url == "https://jobs.lever.co/spotify/223f9722-3579-4ff2-b123-e4cc7b0c1a64"
    assert first.posted_at == datetime(2026, 10, 9, 17, 15, 8, 174000, tzinfo=UTC)


def test_fetch_jobs_description_includes_lists_and_closing_text(
    client: LeverClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(POSTINGS_URL).respond(200, content=load_fixture("lever_postings.json"))

    description = client.fetch_jobs("spotify")[0].description

    assert description.startswith("The Marketing team in Latin America")
    assert "What You'll Do" in description
    assert "Bring creative concepts to life" in description
    assert "Spotify is an equal opportunity employer" in description
    assert "<" not in description


def test_fetch_jobs_minimal_posting_defaults_optional_fields(
    client: LeverClient, respx_mock: respx.MockRouter
) -> None:
    body = [
        {"id": "a", "text": " Kept ", "hostedUrl": "https://jobs.lever.co/spotify/a"},
        {"id": "b", "hostedUrl": "https://jobs.lever.co/spotify/b"},
    ]
    respx_mock.get(POSTINGS_URL).respond(200, json=body)

    postings = client.fetch_jobs("spotify")

    assert [p.title for p in postings] == ["Kept"]
    assert postings[0].location == ""
    assert postings[0].posted_at is None
    assert postings[0].description == ""


def test_fetch_jobs_unknown_board_raises_with_status(
    client: LeverClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(POSTINGS_URL).respond(404, content=load_fixture("lever_not_found.json"))

    with pytest.raises(AtsError) as excinfo:
        client.fetch_jobs("spotify")

    assert excinfo.value.status_code == 404


def test_fetch_jobs_transient_html_500_retried_then_ok(
    client: LeverClient, respx_mock: respx.MockRouter
) -> None:
    # Seen live: api.lever.co briefly returned an HTML "Internal server error" page for a board.
    route = respx_mock.get(POSTINGS_URL)
    route.side_effect = [
        httpx.Response(500, html="<!DOCTYPE html><title>Internal server error</title>"),
        httpx.Response(200, content=load_fixture("lever_postings.json")),
    ]

    assert len(client.fetch_jobs("spotify")) == 3
    assert route.call_count == 2


def test_fetch_jobs_non_json_200_raises(client: LeverClient, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(POSTINGS_URL).respond(200, html="<html>maintenance</html>")

    with pytest.raises(AtsError, match="non-JSON"):
        client.fetch_jobs("spotify")


def test_fetch_jobs_unexpected_shape_raises(
    client: LeverClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(POSTINGS_URL).respond(200, json={"ok": False})

    with pytest.raises(AtsError, match="response shape"):
        client.fetch_jobs("spotify")


# --- probe -------------------------------------------------------------------------------------


def test_probe_live_board_returns_ok_with_count(
    client: LeverClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(POSTINGS_URL).respond(200, content=load_fixture("lever_postings.json"))

    result = client.probe("spotify")

    assert result.ok
    assert result.ats_name == "Lever"
    assert result.job_count == 3


def test_probe_unknown_board_returns_not_ok(
    client: LeverClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(POSTINGS_URL).respond(404, content=load_fixture("lever_not_found.json"))

    result = client.probe("spotify")

    assert not result.ok
    assert result.status_code == 404


def test_probe_persistent_server_error_returns_not_ok(
    client: LeverClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(POSTINGS_URL).respond(503)

    result = client.probe("spotify")

    assert not result.ok
    assert result.status_code == 503
    assert route.call_count == 3
