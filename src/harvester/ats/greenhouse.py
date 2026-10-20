"""Greenhouse: `job-boards.greenhouse.io/{slug}` and the public Job Board API."""

import html
import logging
from datetime import datetime
from urllib.parse import parse_qs

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

NAME = "Greenhouse"
HOSTS = frozenset({"job-boards.greenhouse.io", "boards.greenhouse.io"})
API_BASE = "https://boards-api.greenhouse.io/v1/boards"
# Greenhouse ignores slug case, so lower-case to avoid case-only duplicates.
LOWERCASE_SLUGS = True


class _Location(BaseModel):
    name: str | None = None


class _Job(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    title: str
    absolute_url: str
    location: _Location | None = None
    first_published: datetime | None = None
    content: str | None = None


class _JobsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    jobs: list[dict[str, object]]


class GreenhouseClient:
    name = NAME
    canary_slugs = ("discord", "gitlab")

    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def matches(self, url: str) -> bool:
        return split_url(url).hostname in HOSTS

    def parse_url(self, url: str) -> str | None:
        parts = split_url(url)
        if parts.hostname not in HOSTS:
            return None
        segments = path_segments(parts)
        if not segments:
            return None
        if segments[0] == "embed":
            # Embedded boards: boards.greenhouse.io/embed/job_board?for={slug}
            return normalise_slug(
                parse_qs(parts.query).get("for", [None])[0], lowercase=LOWERCASE_SLUGS
            )
        return normalise_slug(segments[0], lowercase=LOWERCASE_SLUGS)

    def board_url(self, slug: str) -> str:
        return f"https://job-boards.greenhouse.io/{require_slug(slug, lowercase=LOWERCASE_SLUGS)}"

    def fetch_jobs(self, slug: str) -> list[Posting]:
        slug = require_slug(slug, lowercase=LOWERCASE_SLUGS)
        # content=true adds the description, which the harvester needs for keyword matching.
        response = self._parse_response(
            get_json(self._http, f"{API_BASE}/{slug}/jobs?content=true")
        )
        postings: list[Posting] = []
        for item in response.jobs:
            try:
                job = _Job.model_validate(item)
            except ValidationError as exc:
                logger.warning("Skipping malformed Greenhouse job on %s: %s", slug, exc)
                continue
            postings.append(
                Posting(
                    ats_name=NAME,
                    company_slug=slug,
                    ats_job_id=str(job.id),
                    title=job.title.strip(),
                    location=(job.location.name or "").strip() if job.location else "",
                    url=job.absolute_url,
                    posted_at=job.first_published,
                    # Greenhouse HTML-escapes the HTML in `content`, so unescape before stripping.
                    description=html_to_text(html.unescape(job.content or "")),
                )
            )
        return postings

    def probe(self, slug: str) -> ProbeResult:
        slug = require_slug(slug, lowercase=LOWERCASE_SLUGS)
        return probe_endpoint(
            NAME,
            slug,
            lambda: get_json(self._http, f"{API_BASE}/{slug}/jobs"),
            lambda body: len(self._parse_response(body).jobs),
        )

    @staticmethod
    def _parse_response(body: object) -> _JobsResponse:
        try:
            return _JobsResponse.model_validate(body)
        except ValidationError as exc:
            raise AtsError(f"Unexpected Greenhouse response shape: {exc}", status_code=200) from exc
