"""Fixtures: `greenhouse_jobs.json` is the first three jobs of the live `discord` board
(?content=true, 2026-10-19); `greenhouse_not_found.json` is the live 404 body."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from conftest import load_fixture
from harvester.ats.base import AtsError
from harvester.ats.greenhouse import GreenhouseClient

JOBS_URL = "https://boards-api.greenhouse.io/v1/boards/discord/jobs"


@pytest.fixture
def client(http: httpx.Client) -> GreenhouseClient:
    return GreenhouseClient(http)


# --- matches -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://job-boards.greenhouse.io/discord",
        "https://boards.greenhouse.io/discord/jobs/8806482002",
        "job-boards.greenhouse.io/discord",
        "HTTPS://Job-Boards.Greenhouse.IO/discord",
    ],
)
def test_matches_board_hosts_returns_true(client: GreenhouseClient, url: str) -> None:
    assert client.matches(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.lever.co/discord",
        "https://greenhouse.io/discord",
        "https://job-boards.greenhouse.io.evil.example/discord",
        "https://evil.example/job-boards.greenhouse.io/discord",
    ],
)
def test_matches_other_hosts_returns_false(client: GreenhouseClient, url: str) -> None:
    assert not client.matches(url)


# --- parse_url ---------------------------------------------------------------------------------


def test_parse_url_board_root_returns_slug(client: GreenhouseClient) -> None:
    assert client.parse_url("https://job-boards.greenhouse.io/discord") == "discord"


def test_parse_url_job_posting_returns_slug(client: GreenhouseClient) -> None:
    url = "https://job-boards.greenhouse.io/discord/jobs/8806482002?gh_src=abc"

    assert client.parse_url(url) == "discord"


def test_parse_url_legacy_greenhouse_host_returns_slug(client: GreenhouseClient) -> None:
    assert client.parse_url("https://boards.greenhouse.io/discord/jobs/123") == "discord"


def test_parse_url_embed_board_returns_slug_from_query(client: GreenhouseClient) -> None:
    url = "https://boards.greenhouse.io/embed/job_board?for=Discord&b=https://discord.com"

    assert client.parse_url(url) == "discord"


def test_parse_url_mixed_case_slug_returns_lowercase(client: GreenhouseClient) -> None:
    assert client.parse_url("job-boards.greenhouse.io/Discord/") == "discord"


@pytest.mark.parametrize(
    "url",
    [
        "https://job-boards.greenhouse.io",
        "https://job-boards.greenhouse.io/",
        "https://boards.greenhouse.io/embed/job_board",
        "https://job-boards.greenhouse.io/%20bad%20slug",
        "https://jobs.lever.co/discord",
    ],
)
def test_parse_url_no_usable_slug_returns_none(client: GreenhouseClient, url: str) -> None:
    assert client.parse_url(url) is None


# --- fetch_jobs --------------------------------------------------------------------------------


def test_fetch_jobs_fixture_returns_postings(
    client: GreenhouseClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(JOBS_URL, params={"content": "true"}).respond(
        200, content=load_fixture("greenhouse_jobs.json")
    )

    postings = client.fetch_jobs("discord")

    assert route.called
    assert [p.ats_job_id for p in postings] == ["8806482002", "8840756002", "8722538002"]
    first = postings[0]
    assert first.ats_name == "Greenhouse"
    assert first.company_slug == "discord"
    assert first.title == "Commercial Policy Lead"
    assert first.location == "San Francisco Bay Area"
    assert first.url == "https://job-boards.greenhouse.io/discord/jobs/8806482002"
    assert first.posted_at == datetime(
        2026, 9, 15, 12, 52, 11, tzinfo=timezone(timedelta(hours=-4))
    )
    assert postings[2].location == "San Francisco Bay Area (or Remote U.S.)"


def test_fetch_jobs_escaped_html_content_returns_plain_text(
    client: GreenhouseClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(JOBS_URL).respond(200, content=load_fixture("greenhouse_jobs.json"))

    description = client.fetch_jobs("discord")[0].description

    assert description.startswith("Discord has a highly engaged community")
    assert "there’s one thing" in description
    assert "<" not in description
    assert "&lt;" not in description
    assert "&quot;" not in description


def test_fetch_jobs_slug_normalised_in_request_and_postings(
    client: GreenhouseClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(JOBS_URL).respond(200, content=load_fixture("greenhouse_jobs.json"))

    postings = client.fetch_jobs("Discord")

    assert route.called
    assert {p.company_slug for p in postings} == {"discord"}


def test_fetch_jobs_malformed_job_skipped(
    client: GreenhouseClient, respx_mock: respx.MockRouter
) -> None:
    body = {
        "jobs": [
            {"id": 1, "title": "Kept", "absolute_url": "https://x.test/1", "location": None},
            {"id": 2, "absolute_url": "https://x.test/2"},
        ],
        "meta": {"total": 2},
    }
    respx_mock.get(JOBS_URL).respond(200, json=body)

    postings = client.fetch_jobs("discord")

    assert [p.title for p in postings] == ["Kept"]
    assert postings[0].location == ""
    assert postings[0].posted_at is None
    assert postings[0].description == ""


def test_fetch_jobs_unknown_board_raises_with_status(
    client: GreenhouseClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(JOBS_URL).respond(404, content=load_fixture("greenhouse_not_found.json"))

    with pytest.raises(AtsError) as excinfo:
        client.fetch_jobs("discord")

    assert excinfo.value.status_code == 404


def test_fetch_jobs_unexpected_shape_raises(
    client: GreenhouseClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(JOBS_URL).respond(200, json=[{"id": 1}])

    with pytest.raises(AtsError, match="response shape"):
        client.fetch_jobs("discord")


def test_fetch_jobs_unsafe_slug_raises_without_request(client: GreenhouseClient) -> None:
    with pytest.raises(ValueError, match="invalid board slug"):
        client.fetch_jobs("../admin")


# --- probe -------------------------------------------------------------------------------------


def test_probe_live_board_returns_ok_with_count(
    client: GreenhouseClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(JOBS_URL).respond(200, content=load_fixture("greenhouse_jobs.json"))

    result = client.probe("discord")

    assert result.ok
    assert result.status_code == 200
    assert result.job_count == 3
    assert "content" not in route.calls.last.request.url.params


def test_probe_empty_board_returns_ok_with_zero(
    client: GreenhouseClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(JOBS_URL).respond(200, json={"jobs": [], "meta": {"total": 0}})

    result = client.probe("discord")

    assert result.ok
    assert result.job_count == 0


def test_probe_unknown_board_returns_not_ok(
    client: GreenhouseClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(JOBS_URL).respond(404, content=load_fixture("greenhouse_not_found.json"))

    result = client.probe("discord")

    assert not result.ok
    assert result.status_code == 404
    assert result.job_count is None
    assert "404" in (result.error or "")


def test_probe_connection_error_returns_not_ok_without_status(
    client: GreenhouseClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(JOBS_URL).mock(side_effect=httpx.ConnectError("dns failure"))

    result = client.probe("discord")

    assert not result.ok
    assert result.status_code is None
    assert "dns failure" in (result.error or "")
