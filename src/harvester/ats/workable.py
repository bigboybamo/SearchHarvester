"""Workable: `apply.workable.com/{slug}` and the public widget API."""

import logging
from datetime import UTC, date, datetime, time

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from harvester.ats.base import (
    AtsError,
    get_json,
    normalise_slug,
    path_segments,
    probe_endpoint,
    require_slug,
    split_url,
)
from harvester.models import Posting, ProbeResult
from harvester.text import html_to_text

logger = logging.getLogger(__name__)

NAME = "Workable"
HOSTS = frozenset({"apply.workable.com"})
API_BASE = "https://apply.workable.com/api/v1/widget/accounts"

# First path segments on apply.workable.com that are not company slugs. `/j/{shortcode}` is a
# short link that only redirects to `/{slug}/j/{shortcode}`, so it carries no slug.
RESERVED_SEGMENTS = frozenset({"api", "j"})


class _Job(BaseModel):
    model_config = ConfigDict(extra="ignore")

    shortcode: str
    title: str
    url: str
    city: str = ""
    state: str = ""
    country: str = ""
    telecommuting: bool = False
    published_on: date | None = None
    description: str = ""


class _AccountResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    jobs: list[dict[str, object]]


class WorkableClient:
    name = NAME
    # Three, because Workable boards are small and can briefly drop to zero openings.
    canary_slugs = ("huggingface", "epignosis", "blueground")

    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def matches(self, url: str) -> bool:
        return split_url(url).hostname in HOSTS

    def parse_url(self, url: str) -> str | None:
        parts = split_url(url)
        if parts.hostname not in HOSTS:
            return None
        segments = path_segments(parts)
        if not segments or segments[0].lower() in RESERVED_SEGMENTS:
            return None
        return normalise_slug(segments[0])

    def board_url(self, slug: str) -> str:
        return f"https://apply.workable.com/{require_slug(slug)}/"

    def fetch_jobs(self, slug: str) -> list[Posting]:
        slug = require_slug(slug)
        # details=true adds the HTML description, which the harvester needs for keyword matching.
        response = self._parse_response(get_json(self._http, f"{API_BASE}/{slug}?details=true"))
        postings: list[Posting] = []
        for item in response.jobs:
            try:
                job = _Job.model_validate(item)
            except ValidationError as exc:
                logger.warning("Skipping malformed Workable job on %s: %s", slug, exc)
                continue
            postings.append(
                Posting(
                    ats_name=NAME,
                    company_slug=slug,
                    ats_job_id=job.shortcode,
                    title=job.title.strip(),
                    location=_location(job),
                    url=job.url,
                    # Workable gives a date only; midnight UTC keeps posted_at timezone-aware.
                    posted_at=datetime.combine(job.published_on, time(), UTC)
                    if job.published_on
                    else None,
                    description=html_to_text(job.description),
                )
            )
        return postings

    def probe(self, slug: str) -> ProbeResult:
        slug = require_slug(slug)
        return probe_endpoint(
            NAME,
            slug,
            lambda: get_json(self._http, f"{API_BASE}/{slug}"),
            lambda body: len(self._parse_response(body).jobs),
        )

    @staticmethod
    def _parse_response(body: object) -> _AccountResponse:
        try:
            return _AccountResponse.model_validate(body)
        except ValidationError as exc:
            raise AtsError(f"Unexpected Workable response shape: {exc}", status_code=200) from exc


def _location(job: _Job) -> str:
    """`City, State, Country`, with ` (Remote)` appended for remote-friendly roles."""
    place = ", ".join(part.strip() for part in (job.city, job.state, job.country) if part.strip())
    if not job.telecommuting:
        return place
    return f"{place} (Remote)" if place else "Remote"
