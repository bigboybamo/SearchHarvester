"""Data models shared across the harvester and the health check."""

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
