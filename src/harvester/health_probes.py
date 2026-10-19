"""Health-check probes for `AtsSourceGroups` entries.

The rules come from a survey of every live entry (2026-10-19):

- Bare hosts (`jobs.lever.co`) exist for `site:` searches, so only their DNS matters. Their root
  pages routinely redirect to the vendor's marketing site, 404 or block bots, none of which means
  the boards are gone. Hosts that only serve company subdomains (`acme.jobs.personio.com`) or only
  resolve with `www.` are fine too, because `site:` matches subdomains.
- ATS board hosts with an `AtsClient` are checked through known canary boards: the jobs API must
  list jobs on at least one, and its board page on the entry's host must not redirect elsewhere.
  That redirect is the real "domain moved" signal (`boards.greenhouse.io` -> `job-boards...`).
- Page entries (host + path) must load: a 404, a redirect to the home page or another site, a TLS
  failure or no response at all is a problem. 401/403/406/429/451 are bot blocks and cannot be
  verified either way, so they are reported separately and never counted as problems.
"""

import logging
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit

import httpx

from harvester.ats.base import AtsClient
from harvester.ats.registry import AtsRegistry
from harvester.httpclient import get_with_retry, is_certificate_error
from harvester.models import DomainEntry, DomainKind, ProbeResult

logger = logging.getLogger(__name__)

WILDCARD_PROBE_LABEL = "zz-healthcheck-probe"
BLOCKED_STATUSES = frozenset({401, 403, 406, 429, 451})
GONE_STATUSES = frozenset({404, 410})

Resolver = Callable[[str], bool]


class Verdict(StrEnum):
    OK = "ok"
    PROBLEM = "problem"
    UNVERIFIED = "unverified"
    """The site blocks automated requests, so the entry can be judged neither good nor bad."""
    SKIPPED = "skipped"


@dataclass(frozen=True)
class CheckResult:
    entry: str
    """The raw `Domains` entry. Also its identity across runs."""

    group_ids: tuple[int, ...]
    verdict: Verdict
    summary: str
    evidence: str = ""


@dataclass(frozen=True)
class PlatformHealth:
    """Canary-board probes for one ATS."""

    ats_name: str
    probes: tuple[ProbeResult, ...]

    @property
    def healthy_slug(self) -> str | None:
        """The first canary whose jobs API answered with at least one job."""
        return next((p.slug for p in self.probes if p.ok and p.job_count), None)

    def describe(self) -> str:
        return "; ".join(_describe_probe(probe) for probe in self.probes)


def resolves(host: str, *, attempts: int = 2, delay_seconds: float = 2.0) -> bool:
    """True when `host` has a DNS record. Retried once so a resolver blip isn't reported."""
    for attempt in range(1, attempts + 1):
        try:
            socket.getaddrinfo(host, 443)
        except OSError:
            if attempt < attempts:
                time.sleep(delay_seconds)
        else:
            return True
    return False


def probe_platform(client: AtsClient) -> PlatformHealth:
    return PlatformHealth(client.name, tuple(client.probe(slug) for slug in client.canary_slugs))


class EntryChecker:
    """Checks single entries. Thread-safe: it only reads its inputs and the shared HTTP client."""

    def __init__(
        self,
        http: httpx.Client,
        registry: AtsRegistry,
        platforms: Mapping[str, PlatformHealth],
        resolve: Resolver = resolves,
    ) -> None:
        self._http = http
        self._registry = registry
        self._platforms = platforms
        self._resolve = resolve

    def check(self, entry: DomainEntry, group_ids: tuple[int, ...]) -> CheckResult:
        def result(verdict: Verdict, summary: str, evidence: str = "") -> CheckResult:
            return CheckResult(entry.raw, group_ids, verdict, summary, evidence)

        if entry.kind is DomainKind.TLD or entry.host is None or entry.url is None:
            return result(Verdict.SKIPPED, "TLD search term; nothing to probe")

        host, url = entry.host, entry.url
        note = ""
        if not self._resolve(host):
            www = f"www.{host}"
            if not host.startswith("www.") and self._resolve(www):
                note = f"{host} has no DNS record; checked {www} instead. "
                host, url = www, _with_host(url, www)
            elif entry.kind is DomainKind.HOST and self._resolve(f"{WILDCARD_PROBE_LABEL}.{host}"):
                return result(
                    Verdict.OK,
                    "Serves company subdomains",
                    f"{host} has no DNS record of its own, but *.{host} resolves",
                )
            else:
                return result(
                    Verdict.PROBLEM,
                    "Does not resolve",
                    f"No DNS record for {host}, www.{host} or *.{host}",
                )

        client = self._registry.client_for(entry.url)
        if entry.kind is DomainKind.HOST and client is not None:
            verdict, summary, evidence = self._check_board_host(client, host)
        elif entry.kind is DomainKind.HOST:
            verdict, summary, evidence = Verdict.OK, "Resolves", ""
        else:
            verdict, summary, evidence = self._check_page(url, host)
        return result(verdict, summary, (note + evidence).strip())

    def _check_board_host(self, client: AtsClient, host: str) -> tuple[Verdict, str, str]:
        health = self._platforms[client.name]
        slug = health.healthy_slug
        if slug is None:
            return Verdict.PROBLEM, f"No known {client.name} board lists jobs", health.describe()

        board = _with_host(client.board_url(slug), host)
        try:
            response = get_with_retry(self._http, board)
        except httpx.HTTPError as exc:
            return Verdict.PROBLEM, "Board page unreachable", f"GET {board}: {_describe_error(exc)}"

        final = response.url
        if final.host != host:
            return Verdict.PROBLEM, f"Boards moved to {final.host}", f"{board} redirects to {final}"
        if response.status_code in GONE_STATUSES:
            return (
                Verdict.PROBLEM,
                "Board page not found on this host",
                f"GET {board} returned HTTP {response.status_code}, but the {client.name} API "
                f"lists jobs for {slug}",
            )
        if response.status_code in BLOCKED_STATUSES:
            return (
                Verdict.UNVERIFIED,
                f"Blocks automated requests (HTTP {response.status_code})",
                f"GET {board}",
            )
        return (
            Verdict.OK,
            "Known boards healthy",
            f"{health.describe()}; {board} returned HTTP {response.status_code}",
        )

    def _check_page(self, url: str, host: str) -> tuple[Verdict, str, str]:
        try:
            response = get_with_retry(self._http, url)
        except httpx.TooManyRedirects:
            return Verdict.PROBLEM, "Redirect loop", f"GET {url} never settled on a page"
        except httpx.HTTPError as exc:
            summary = "TLS certificate error" if is_certificate_error(exc) else "Unreachable"
            return Verdict.PROBLEM, summary, f"GET {url}: {_describe_error(exc)}"

        status, final = response.status_code, response.url
        evidence = f"HTTP {status} at {final}"
        if status in GONE_STATUSES:
            return Verdict.PROBLEM, f"Page not found (HTTP {status})", evidence
        if not _same_site(host, final.host):
            return Verdict.PROBLEM, f"Moved to {final.host}", evidence
        if urlsplit(url).path not in ("", "/") and final.path in ("", "/"):
            return Verdict.PROBLEM, "Redirects to the home page", evidence
        if status in BLOCKED_STATUSES:
            return Verdict.UNVERIFIED, f"Blocks automated requests (HTTP {status})", evidence
        if status >= 400:
            return Verdict.PROBLEM, f"HTTP {status}", evidence
        return Verdict.OK, "Page loads", evidence


def _with_host(url: str, host: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=host))


def _same_site(a: str, b: str) -> bool:
    return a.removeprefix("www.") == b.removeprefix("www.")


def _describe_error(exc: httpx.HTTPError) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _describe_probe(probe: ProbeResult) -> str:
    if probe.ok:
        return f"{probe.slug}: {probe.job_count} jobs"
    if probe.status_code is not None:
        return f"{probe.slug}: HTTP {probe.status_code}"
    return f"{probe.slug}: {probe.error or 'no response'}"
