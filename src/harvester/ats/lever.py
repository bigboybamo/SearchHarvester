"""Lever: `jobs.lever.co/{slug}` and the public Postings API."""

import logging
from datetime import UTC, datetime

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError
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

NAME = "Lever"
HOSTS = frozenset({"jobs.lever.co"})
API_BASE = "https://api.lever.co/v0/postings"

_RESPONSE = TypeAdapter(list[dict[str, object]])


class _Categories(BaseModel):
    location: str | None = None


class _ListSection(BaseModel):
    text: str = ""
    content: str = ""


class _Posting(BaseModel):
    model_config = ConfigDict(extra="ignore", alias_generator=to_camel)

    id: str
    text: str
    hosted_url: str
    created_at: int | None = None
    categories: _Categories | None = None
    description_plain: str = ""
    lists: list[_ListSection] = []
    additional_plain: str = ""


class LeverClient:
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
        slug = require_slug(slug)
        # Without `limit` the API returns every posting in one response.
        items = self._parse_response(get_json(self._http, f"{API_BASE}/{slug}?mode=json"))
        postings: list[Posting] = []
        for item in items:
            try:
                posting = _Posting.model_validate(item)
            except ValidationError as exc:
                logger.warning("Skipping malformed Lever posting on %s: %s", slug, exc)
                continue
            postings.append(
                Posting(
                    ats_name=NAME,
                    company_slug=slug,
                    ats_job_id=posting.id,
                    title=posting.text.strip(),
                    location=(posting.categories.location or "").strip()
                    if posting.categories
                    else "",
                    url=posting.hosted_url,
                    # Lever exposes only the creation time (epoch milliseconds), not a publish time.
                    posted_at=datetime.fromtimestamp(posting.created_at / 1000, UTC)
                    if posting.created_at is not None
                    else None,
                    description=_description(posting),
                )
            )
        return postings

    def probe(self, slug: str) -> ProbeResult:
        slug = require_slug(slug)
        return probe_endpoint(
            NAME,
            slug,
            lambda: get_json(self._http, f"{API_BASE}/{slug}?mode=json"),
            lambda body: len(self._parse_response(body)),
        )

    @staticmethod
    def _parse_response(body: object) -> list[dict[str, object]]:
        try:
            return _RESPONSE.validate_python(body)
        except ValidationError as exc:
            raise AtsError(f"Unexpected Lever response shape: {exc}", status_code=200) from exc


def _description(posting: _Posting) -> str:
    """Description, requirement lists and closing text, joined as plain text."""
    sections = [posting.description_plain.strip()]
    for section in posting.lists:
        sections.append(section.text.strip())
        sections.append(html_to_text(section.content))
    sections.append(posting.additional_plain.strip())
    return "\n".join(section for section in sections if section)
