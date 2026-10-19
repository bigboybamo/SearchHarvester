"""Ashby: `jobs.ashbyhq.com/{slug}` and the public Posting API."""

import logging
from datetime import datetime

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel

from harvester.ats.base import (
    AtsError,
    get_json,
    html_to_text,
    normalise_slug,
    path_segments,
    probe_endpoint,
    require_slug,
    split_url,
)
from harvester.models import Posting, ProbeResult

logger = logging.getLogger(__name__)

NAME = "Ashby"
HOSTS = frozenset({"jobs.ashbyhq.com"})
API_BASE = "https://api.ashbyhq.com/posting-api/job-board"


class _SecondaryLocation(BaseModel):
    location: str = ""


class _Job(BaseModel):
    model_config = ConfigDict(extra="ignore", alias_generator=to_camel)

    id: str
    title: str
    job_url: str
    location: str = ""
    secondary_locations: list[_SecondaryLocation] = []
    published_at: datetime | None = None
    is_listed: bool = True
    description_plain: str = ""
    description_html: str = ""


class _JobBoardResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    jobs: list[dict[str, object]]


class AshbyClient:
    name = NAME

    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def matches(self, url: str) -> bool:
        return split_url(url).hostname in HOSTS

    def parse_url(self, url: str) -> str | None:
        parts = split_url(url)
        if parts.hostname not in HOSTS:
            return None
        segments = path_segments(parts)
        return normalise_slug(segments[0]) if segments else None

    def fetch_jobs(self, slug: str) -> list[Posting]:
        # Ashby echoes the requested slug's case into jobUrl, so a lower-case slug also keeps
        # posting URLs stable for de-duplication.
        slug = require_slug(slug)
        response = self._parse_response(get_json(self._http, f"{API_BASE}/{slug}"))
        postings: list[Posting] = []
        for item in response.jobs:
            try:
                job = _Job.model_validate(item)
            except ValidationError as exc:
                logger.warning("Skipping malformed Ashby job on %s: %s", slug, exc)
                continue
            if not job.is_listed:
                continue
            postings.append(
                Posting(
                    ats_name=NAME,
                    company_slug=slug,
                    ats_job_id=job.id,
                    title=job.title.strip(),
                    location=_location(job),
                    url=job.job_url,
                    posted_at=job.published_at,
                    description=job.description_plain.strip() or html_to_text(job.description_html),
                )
            )
        return postings

    def probe(self, slug: str) -> ProbeResult:
        slug = require_slug(slug)
        return probe_endpoint(
            NAME,
            slug,
            lambda: get_json(self._http, f"{API_BASE}/{slug}"),
            lambda body: sum(
                1 for item in self._parse_response(body).jobs if item.get("isListed", True)
            ),
        )

    @staticmethod
    def _parse_response(body: object) -> _JobBoardResponse:
        try:
            return _JobBoardResponse.model_validate(body)
        except ValidationError as exc:
            raise AtsError(f"Unexpected Ashby response shape: {exc}", status_code=200) from exc


def _location(job: _Job) -> str:
    """Primary location followed by any secondary ones, de-duplicated, `; `-separated."""
    names = [job.location, *(secondary.location for secondary in job.secondary_locations)]
    distinct = dict.fromkeys(name.strip() for name in names if name.strip())
    return "; ".join(distinct)
