"""Fixtures: `ashby_job_board.json` holds three jobs from the live `linear` board (2026-10-19),
the third chosen because it has a secondary location; `ashby_not_found.txt` is the live
plain-text 404 body."""

from datetime import UTC, datetime

import httpx
import pytest
import respx

from conftest import load_fixture
from harvester.ats.ashby import AshbyClient
from harvester.ats.base import AtsError

BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/linear"


@pytest.fixture
def client(http: httpx.Client) -> AshbyClient:
    return AshbyClient(http)


def _job(**overrides: object) -> dict[str, object]:
    job: dict[str, object] = {
        "id": "a",
        "title": "Engineer",
        "jobUrl": "https://jobs.ashbyhq.com/linear/a",
    }
    job.update(overrides)
    return job


# --- matches -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.ashbyhq.com/linear",
        "https://jobs.ashbyhq.com/linear/d3bc1ced-3ce4-4086-a050-555055dbb1ff/application",
        "jobs.ashbyhq.com/linear",
        "https://JOBS.ASHBYHQ.COM/linear",
    ],
)
def test_matches_board_host_returns_true(client: AshbyClient, url: str) -> None:
    assert client.matches(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://ashbyhq.com/linear",
        "https://api.ashbyhq.com/posting-api/job-board/linear",
        "https://jobs.ashbyhq.com.evil.example/linear",
        "https://jobs.lever.co/linear",
    ],
)
def test_matches_other_hosts_returns_false(client: AshbyClient, url: str) -> None:
    assert not client.matches(url)


# --- parse_url ---------------------------------------------------------------------------------


def test_parse_url_board_root_returns_slug(client: AshbyClient) -> None:
    assert client.parse_url("https://jobs.ashbyhq.com/linear") == "linear"


def test_parse_url_job_and_application_pages_return_slug(client: AshbyClient) -> None:
    job = "https://jobs.ashbyhq.com/linear/d3bc1ced-3ce4-4086-a050-555055dbb1ff"

    assert client.parse_url(job) == "linear"
    assert client.parse_url(f"{job}/application?utm_source=x") == "linear"


def test_parse_url_mixed_case_slug_returns_lowercase(client: AshbyClient) -> None:
    # Ashby accepts any case but echoes it into jobUrl, so case variants would duplicate URLs.
    assert client.parse_url("https://jobs.ashbyhq.com/Linear") == "linear"


@pytest.mark.parametrize(
    "url", ["https://jobs.ashbyhq.com", "https://jobs.ashbyhq.com/", "https://ashbyhq.com/linear"]
)
def test_parse_url_no_usable_slug_returns_none(client: AshbyClient, url: str) -> None:
    assert client.parse_url(url) is None


# --- fetch_jobs --------------------------------------------------------------------------------


def test_fetch_jobs_fixture_returns_postings(
    client: AshbyClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(BOARD_URL).respond(200, content=load_fixture("ashby_job_board.json"))

    postings = client.fetch_jobs("linear")

    assert route.called
    assert [p.ats_job_id[:8] for p in postings] == ["d3bc1ced", "cd5ae036", "1d652292"]
    first = postings[0]
    assert first.ats_name == "Ashby"
    assert first.company_slug == "linear"
    assert first.title == "Senior / Staff Fullstack Engineer"
    assert first.location == "Europe"
    assert first.url == "https://jobs.ashbyhq.com/linear/d3bc1ced-3ce4-4086-a050-555055dbb1ff"
    assert first.posted_at == datetime(2021, 4, 27, 20, 13, 45, 158000, tzinfo=UTC)
    assert first.description.startswith("At Linear, we're building the product development system")


def test_fetch_jobs_secondary_locations_appended(
    client: AshbyClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(BOARD_URL).respond(200, content=load_fixture("ashby_job_board.json"))

    assert client.fetch_jobs("linear")[2].location == "North America; Europe"


def test_fetch_jobs_duplicate_secondary_location_listed_once(
    client: AshbyClient, respx_mock: respx.MockRouter
) -> None:
    job = _job(location="Remote", secondaryLocations=[{"location": "Remote"}, {"location": "UK"}])
    respx_mock.get(BOARD_URL).respond(200, json={"jobs": [job]})

    assert client.fetch_jobs("linear")[0].location == "Remote; UK"


def test_fetch_jobs_unlisted_job_skipped(client: AshbyClient, respx_mock: respx.MockRouter) -> None:
    jobs = [_job(id="a", isListed=True), _job(id="b", isListed=False)]
    respx_mock.get(BOARD_URL).respond(200, json={"jobs": jobs})

    assert [p.ats_job_id for p in client.fetch_jobs("linear")] == ["a"]


def test_fetch_jobs_html_only_description_converted(
    client: AshbyClient, respx_mock: respx.MockRouter
) -> None:
    job = _job(descriptionPlain="", descriptionHtml="<p>Build <b>Python</b> things</p>")
    respx_mock.get(BOARD_URL).respond(200, json={"jobs": [job]})

    assert client.fetch_jobs("linear")[0].description == "Build Python things"


def test_fetch_jobs_malformed_job_skipped(
    client: AshbyClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(BOARD_URL).respond(200, json={"jobs": [_job(), {"id": "no-title"}]})

    postings = client.fetch_jobs("linear")

    assert len(postings) == 1
    assert postings[0].location == ""
    assert postings[0].posted_at is None


def test_fetch_jobs_unknown_board_raises_with_status(
    client: AshbyClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(BOARD_URL).respond(404, content=load_fixture("ashby_not_found.txt"))

    with pytest.raises(AtsError) as excinfo:
        client.fetch_jobs("linear")

    assert excinfo.value.status_code == 404


def test_fetch_jobs_unexpected_shape_raises(
    client: AshbyClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(BOARD_URL).respond(200, json={"apiVersion": "1"})

    with pytest.raises(AtsError, match="response shape"):
        client.fetch_jobs("linear")


# --- probe -------------------------------------------------------------------------------------


def test_probe_live_board_returns_ok_with_count(
    client: AshbyClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(BOARD_URL).respond(200, content=load_fixture("ashby_job_board.json"))

    result = client.probe("linear")

    assert result.ok
    assert result.ats_name == "Ashby"
    assert result.job_count == 3


def test_probe_unlisted_jobs_not_counted(client: AshbyClient, respx_mock: respx.MockRouter) -> None:
    jobs = [_job(id="a"), _job(id="b", isListed=False)]
    respx_mock.get(BOARD_URL).respond(200, json={"jobs": jobs})

    assert client.probe("linear").job_count == 1


def test_probe_unknown_board_returns_not_ok(
    client: AshbyClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(BOARD_URL).respond(404, content=load_fixture("ashby_not_found.txt"))

    result = client.probe("linear")

    assert not result.ok
    assert result.status_code == 404
