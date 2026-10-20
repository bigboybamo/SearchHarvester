"""Data models shared across the harvester and the health check."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

REPLACEMENT_CHAR = "�"


class DomainKind(StrEnum):
    """How an `AtsSourceGroups[].Domains` entry is shaped."""

    HOST = "host"
    """A bare host such as `jobs.lever.co`."""

    URL = "url"
    """A host plus a path and/or query, such as `jobs.itu.int/go/View-all-categories/8942455`."""

    TLD = "tld"
    """A top-level-domain search term such as `.ai`. It has no host to probe."""


class DomainEntry(BaseModel):
    """One parsed entry from an ATS source group's `Domains` list."""

    model_config = ConfigDict(frozen=True)

    raw: str
    """The entry exactly as written in `appsettings.json`, minus surrounding whitespace."""

    kind: DomainKind
    host: str | None
    """Lower-cased host name; `None` for TLD entries."""

    url: str | None
    """The entry as an absolute URL (scheme defaults to `https://`); `None` for TLD entries."""

    tracking_params: tuple[str, ...] = ()
    """Analytics query parameters (e.g. `_gl`) found in the entry. Worth reporting, not fatal."""


class AtsSourceGroup(BaseModel):
    """One `AtsSourceGroups` item. Always identify groups by `id`, never by `name`."""

    model_config = ConfigDict(frozen=True)

    id: int
    name: str
    domains: tuple[DomainEntry, ...]

    @property
    def has_corrupted_name(self) -> bool:
        """True when the name contains U+FFFD, i.e. it was saved with the wrong encoding."""
        return REPLACEMENT_CHAR in self.name


class Posting(BaseModel):
    """One job posting as returned by an ATS jobs API."""

    model_config = ConfigDict(frozen=True)

    ats_name: str
    company_slug: str
    ats_job_id: str
    title: str
    location: str
    """Free-text location as the ATS reports it; empty when the ATS gives none."""

    url: str
    """Public posting URL. This is the de-duplication key in `JobPostings`."""

    posted_at: datetime | None
    """Timezone-aware publish time, when the ATS exposes one."""

    description: str = ""
    """Plain-text description, used to match stack and role terms locally."""


class ProbeResult(BaseModel):
    """Outcome of checking whether a known board is still live."""

    model_config = ConfigDict(frozen=True)

    ats_name: str
    slug: str
    ok: bool
    """True when the jobs API answered 200 with a parseable body, even if the board has no jobs."""

    status_code: int | None = None
    """HTTP status of the final response; `None` when no response arrived (e.g. DNS failure)."""

    job_count: int | None = None
    error: str | None = None


class SearchHit(BaseModel):
    """One organic result from a search provider. Used only to discover company boards."""

    model_config = ConfigDict(frozen=True)

    provider: str
    rank: int
    """1-based position across pages, as the provider ranked it."""

    title: str
    url: str
    snippet: str = ""
