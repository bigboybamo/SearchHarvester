"""Environment settings and the JobSearchBuilder `appsettings.json` parser.

`appsettings.json` is owned by JobSearchBuilder and is the single source of truth for ATS
domains. This module only reads it.
"""

import json
import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from harvester.httpclient import create_client, get_with_retry
from harvester.models import AtsSourceGroup, DomainEntry, DomainKind

logger = logging.getLogger(__name__)

DEFAULT_APPSETTINGS_URL = (
    "https://raw.githubusercontent.com/bigboybamo/JobSearchBuilder/master/"
    "JobSearchBuilder/appsettings.json"
)
SEARCH_PROVIDERS = frozenset({"brave", "serpapi"})
DEFAULT_SEARCH_PROVIDER = "brave"

TRACKING_PARAMS = frozenset({"_gl", "gclid", "fbclid", "msclkid"})
TRACKING_PARAM_PREFIXES = ("_ga", "utm_")


class ConfigError(Exception):
    """Configuration is missing, malformed or unreachable."""


# --- Environment -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """Values read from environment variables. Secrets are excluded from `repr`."""

    search_provider: str = DEFAULT_SEARCH_PROVIDER
    appsettings_url: str = DEFAULT_APPSETTINGS_URL
    connection_string: str | None = field(default=None, repr=False)
    brave_api_key: str | None = field(default=None, repr=False)
    serpapi_api_key: str | None = field(default=None, repr=False)
    github_token: str | None = field(default=None, repr=False)


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Build `Settings` from `environ`, or from `os.environ` plus a local `.env` file.

    Real environment variables take precedence over `.env`. Blank values count as unset.
    """
    if environ is None:
        load_dotenv(override=False)
        environ = os.environ

    def get(name: str) -> str | None:
        value = environ.get(name, "").strip()
        return value or None

    provider = (get("SEARCH_PROVIDER") or DEFAULT_SEARCH_PROVIDER).lower()
    if provider not in SEARCH_PROVIDERS:
        raise ConfigError(
            f"SEARCH_PROVIDER must be one of {sorted(SEARCH_PROVIDERS)}, got {provider!r}"
        )

    return Settings(
        search_provider=provider,
        appsettings_url=get("JSB_APPSETTINGS_URL") or DEFAULT_APPSETTINGS_URL,
        connection_string=get("JSB_CONNECTION_STRING"),
        brave_api_key=get("BRAVE_API_KEY"),
        serpapi_api_key=get("SERPAPI_API_KEY"),
        github_token=get("JSB_GITHUB_TOKEN"),
    )


# --- appsettings.json --------------------------------------------------------------------------


class _RawGroup(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int = Field(alias="Id")
    name: str = Field(alias="Name")
    domains: list[str] = Field(alias="Domains")


class _RawAppSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ats_source_groups: list[_RawGroup] = Field(alias="AtsSourceGroups")


def parse_domain_entry(raw: str) -> DomainEntry:
    """Parse one `Domains` entry. Entries may be a bare host, host + path + query, or a TLD."""
    text = raw.strip()
    if not text:
        raise ValueError("domain entry is empty")

    if text.startswith(".") and "/" not in text:
        return DomainEntry(raw=text, kind=DomainKind.TLD, host=None, url=None)

    url = text if "://" in text else f"https://{text}"
    parts = urlsplit(url)
    if not parts.hostname:
        raise ValueError(f"domain entry {raw!r} has no host")

    has_path_or_query = parts.path not in ("", "/") or bool(parts.query)
    tracking = tuple(
        key
        for key, _ in parse_qsl(parts.query, keep_blank_values=True)
        if key in TRACKING_PARAMS or key.startswith(TRACKING_PARAM_PREFIXES)
    )
    return DomainEntry(
        raw=text,
        kind=DomainKind.URL if has_path_or_query else DomainKind.HOST,
        host=parts.hostname,
        url=url,
        tracking_params=tracking,
    )


def parse_appsettings(content: bytes | str) -> list[AtsSourceGroup]:
    """Parse the `AtsSourceGroups` section of `appsettings.json`.

    Tolerates a UTF-8 BOM. Group names are kept as-is (some contain U+FFFD); use `Id` to
    identify groups. Blank domain entries are skipped with a warning.
    """
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ConfigError(f"appsettings.json is not valid UTF-8: {exc}") from exc
    else:
        text = content.removeprefix("﻿")

    try:
        raw = _RawAppSettings.model_validate(json.loads(text))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"appsettings.json is not valid JSON: {exc}") from exc
    except ValidationError as exc:
        raise ConfigError(f"appsettings.json has an unexpected shape: {exc}") from exc

    groups: list[AtsSourceGroup] = []
    seen_ids: set[int] = set()
    for raw_group in raw.ats_source_groups:
        if raw_group.id in seen_ids:
            raise ConfigError(f"AtsSourceGroups has duplicate Id {raw_group.id}")
        seen_ids.add(raw_group.id)

        domains: list[DomainEntry] = []
        for entry in raw_group.domains:
            try:
                domains.append(parse_domain_entry(entry))
            except ValueError as exc:
                logger.warning("Skipping entry in source group %d: %s", raw_group.id, exc)
        groups.append(AtsSourceGroup(id=raw_group.id, name=raw_group.name, domains=tuple(domains)))
    return groups


def read_appsettings(source: str, client: httpx.Client | None = None) -> bytes:
    """Read raw `appsettings.json` bytes from an http(s) URL or a local file path."""
    if not source.startswith(("http://", "https://")):
        try:
            return Path(source).read_bytes()
        except OSError as exc:
            raise ConfigError(f"Cannot read appsettings.json from {source}: {exc}") from exc

    owns_client = client is None
    http = client or create_client()
    try:
        response = get_with_retry(http, source)
    except httpx.HTTPError as exc:
        raise ConfigError(f"Cannot fetch appsettings.json from {source}: {exc}") from exc
    finally:
        if owns_client:
            http.close()

    if response.status_code != httpx.codes.OK:
        raise ConfigError(
            f"Cannot fetch appsettings.json from {source}: HTTP {response.status_code}"
        )
    return response.content


def load_source_groups(source: str, client: httpx.Client | None = None) -> list[AtsSourceGroup]:
    """Read and parse `AtsSourceGroups` from `source` (URL or file path)."""
    return parse_appsettings(read_appsettings(source, client))


def domains_for_groups(
    groups: Iterable[AtsSourceGroup], group_ids: Iterable[int]
) -> list[DomainEntry]:
    """Distinct domain entries of the given group ids, ordered by `group_ids` then file order.

    Unknown ids (e.g. a `ProfileSourceGroups` row whose group was removed) are logged and skipped.
    """
    by_id = {group.id: group for group in groups}
    result: list[DomainEntry] = []
    seen_raw: set[str] = set()
    for group_id in group_ids:
        group = by_id.get(group_id)
        if group is None:
            logger.warning("Unknown AtsSourceGroups Id %d; skipping", group_id)
            continue
        for entry in group.domains:
            if entry.raw not in seen_raw:
                seen_raw.add(entry.raw)
                result.append(entry)
    return result
