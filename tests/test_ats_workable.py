"""Fixtures: `workable_account.json` holds the first three jobs of the live `huggingface`
account (?details=true, 2026-10-19); `workable_not_found.txt` is the live plain-text 404 body."""

from datetime import UTC, datetime

import httpx
import pytest
import respx

from conftest import load_fixture
from harvester.ats.base import AtsError
from harvester.ats.workable import WorkableClient

ACCOUNT_URL = "https://apply.workable.com/api/v1/widget/accounts/huggingface"


@pytest.fixture
def client(http: httpx.Client) -> WorkableClient:
    return WorkableClient(http)


def _job(**overrides: object) -> dict[str, object]:
    job: dict[str, object] = {
        "shortcode": "ABC123",
        "title": "Engineer",
        "url": "https://apply.workable.com/j/ABC123",
    }
    job.update(overrides)
    return job


# --- matches -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://apply.workable.com/huggingface/",
        "https://apply.workable.com/huggingface/j/81B46579FE/",
        "https://apply.workable.com/j/81B46579FE",
        "apply.workable.com/huggingface",
    ],
)
def test_matches_board_host_returns_true(client: WorkableClient, url: str) -> None:
    assert client.matches(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://workable.com/huggingface",
        "https://huggingface.workable.com/",
        "https://apply.workable.com.evil.example/huggingface",
        "https://jobs.ashbyhq.com/huggingface",
    ],
)
def test_matches_other_hosts_returns_false(client: WorkableClient, url: str) -> None:
    assert not client.matches(url)


# --- parse_url ---------------------------------------------------------------------------------


def test_parse_url_board_root_returns_slug(client: WorkableClient) -> None:
    assert client.parse_url("https://apply.workable.com/huggingface/") == "huggingface"


def test_parse_url_job_page_returns_slug(client: WorkableClient) -> None:
    url = "https://apply.workable.com/huggingface/j/81B46579FE/apply/?utm_source=x"

    assert client.parse_url(url) == "huggingface"


def test_parse_url_mixed_case_slug_keeps_case(client: WorkableClient) -> None:
    # Workable slugs are case-sensitive (/HuggingFace is a 404 on the live API), so the
    # spelling found in a search-result URL is kept rather than guessed.
    assert client.parse_url("apply.workable.com/HuggingFace") == "HuggingFace"


@pytest.mark.parametrize(
    "url",
    [
        "https://apply.workable.com/j/81B46579FE",
        "https://apply.workable.com/J/81B46579FE",
        "https://apply.workable.com/api/v1/widget/accounts/huggingface",
        "https://apply.workable.com/",
        "https://workable.com/huggingface",
    ],
)
def test_parse_url_no_usable_slug_returns_none(client: WorkableClient, url: str) -> None:
    assert client.parse_url(url) is None


# --- fetch_jobs --------------------------------------------------------------------------------


def test_fetch_jobs_fixture_returns_postings(
    client: WorkableClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(ACCOUNT_URL, params={"details": "true"}).respond(
        200, content=load_fixture("workable_account.json")
    )

    postings = client.fetch_jobs("huggingface")

    assert route.called
    assert [p.ats_job_id for p in postings] == ["81B46579FE", "19A136F8E2", "9E2A4C02C7"]
    first = postings[0]
    assert first.ats_name == "Workable"
    assert first.company_slug == "huggingface"
    assert first.title == "Open-Source Machine Learning Engineer - EMEA Remote"
    assert first.location == "Paris, Île-de-France, France (Remote)"
    assert first.url == "https://apply.workable.com/j/81B46579FE"
    assert first.posted_at == datetime(2026, 5, 29, tzinfo=UTC)


def test_fetch_jobs_html_description_returns_plain_text(
    client: WorkableClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(ACCOUNT_URL).respond(200, content=load_fixture("workable_account.json"))

    description = client.fetch_jobs("huggingface")[0].description

    assert description.startswith("At Hugging Face, we're on a journey to democratize good AI.")
    assert "<" not in description


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"city": "Berlin", "country": "Germany"}, "Berlin, Germany"),
        ({"city": " ", "state": "", "country": "Spain", "telecommuting": True}, "Spain (Remote)"),
        ({"telecommuting": True}, "Remote"),
        ({}, ""),
    ],
)
def test_fetch_jobs_location_variants(
    client: WorkableClient,
    respx_mock: respx.MockRouter,
    fields: dict[str, object],
    expected: str,
) -> None:
    respx_mock.get(ACCOUNT_URL).respond(200, json={"jobs": [_job(**fields)]})

    assert client.fetch_jobs("huggingface")[0].location == expected


def test_fetch_jobs_minimal_job_defaults_optional_fields(
    client: WorkableClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(ACCOUNT_URL).respond(200, json={"jobs": [_job(), {"title": "no shortcode"}]})

    postings = client.fetch_jobs("huggingface")

    assert len(postings) == 1
    assert postings[0].posted_at is None
    assert postings[0].description == ""


def test_fetch_jobs_unknown_board_raises_with_status(
    client: WorkableClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(ACCOUNT_URL).respond(404, content=load_fixture("workable_not_found.txt"))

    with pytest.raises(AtsError) as excinfo:
        client.fetch_jobs("huggingface")

    assert excinfo.value.status_code == 404


def test_fetch_jobs_unexpected_shape_raises(
    client: WorkableClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(ACCOUNT_URL).respond(200, json={"name": "Hugging Face"})

    with pytest.raises(AtsError, match="response shape"):
        client.fetch_jobs("huggingface")


# --- probe -------------------------------------------------------------------------------------


def test_probe_live_board_returns_ok_without_details(
    client: WorkableClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(ACCOUNT_URL).respond(200, content=load_fixture("workable_account.json"))

    result = client.probe("huggingface")

    assert result.ok
    assert result.ats_name == "Workable"
    assert result.job_count == 3
    assert "details" not in route.calls.last.request.url.params


def test_probe_unknown_board_returns_not_ok(
    client: WorkableClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(ACCOUNT_URL).respond(404, content=load_fixture("workable_not_found.txt"))

    result = client.probe("huggingface")

    assert not result.ok
    assert result.status_code == 404
