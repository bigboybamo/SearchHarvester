import socket
import ssl

import httpx
import pytest
import respx

from harvester.ats.registry import AtsRegistry, default_registry
from harvester.config import parse_domain_entry
from harvester.health_probes import (
    WILDCARD_PROBE_LABEL,
    CheckResult,
    EntryChecker,
    PlatformHealth,
    Verdict,
    probe_platform,
    resolves,
)
from harvester.httpclient import is_certificate_error
from harvester.models import ProbeResult

GROUPS = (7,)


def _probe(ats: str, slug: str, *, jobs: int | None = 5, status: int = 200) -> ProbeResult:
    ok = status == 200
    return ProbeResult(
        ats_name=ats, slug=slug, ok=ok, status_code=status, job_count=jobs if ok else None
    )


HEALTHY_GREENHOUSE = PlatformHealth(
    "Greenhouse", (_probe("Greenhouse", "discord"), _probe("Greenhouse", "gitlab"))
)


class FakeDns:
    """Resolves exactly the given hosts."""

    def __init__(self, *hosts: str) -> None:
        self.hosts = set(hosts)
        self.lookups: list[str] = []

    def __call__(self, host: str) -> bool:
        self.lookups.append(host)
        return host in self.hosts


@pytest.fixture
def registry(http: httpx.Client) -> AtsRegistry:
    return default_registry(http)


def _check(
    http: httpx.Client,
    registry: AtsRegistry,
    raw: str,
    dns: FakeDns,
    platforms: dict[str, PlatformHealth] | None = None,
) -> CheckResult:
    checker = EntryChecker(http, registry, platforms or {}, dns)
    return checker.check(parse_domain_entry(raw), GROUPS)


# --- DNS ---------------------------------------------------------------------------------------


def test_check_tld_entry_skipped_without_lookups(http: httpx.Client, registry: AtsRegistry) -> None:
    dns = FakeDns()

    result = _check(http, registry, ".ai", dns)

    assert result.verdict is Verdict.SKIPPED
    assert result.entry == ".ai"
    assert dns.lookups == []


def test_check_bare_host_resolves_ok_without_http(
    http: httpx.Client, registry: AtsRegistry
) -> None:
    # respx would fail any request, so passing proves a bare host gets no HTTP check.
    result = _check(http, registry, "careers.aramco.com", FakeDns("careers.aramco.com"))

    assert result.verdict is Verdict.OK
    assert result.group_ids == GROUPS


def test_check_bare_host_only_www_resolves_ok_with_note(
    http: httpx.Client, registry: AtsRegistry
) -> None:
    result = _check(http, registry, "isdb.org", FakeDns("www.isdb.org"))

    assert result.verdict is Verdict.OK
    assert "checked www.isdb.org instead" in result.evidence


def test_check_bare_host_wildcard_dns_ok(http: httpx.Client, registry: AtsRegistry) -> None:
    dns = FakeDns(f"{WILDCARD_PROBE_LABEL}.jobs.personio.com")

    result = _check(http, registry, "jobs.personio.com", dns)

    assert result.verdict is Verdict.OK
    assert result.summary == "Serves company subdomains"


def test_check_host_without_any_dns_problem(http: httpx.Client, registry: AtsRegistry) -> None:
    result = _check(http, registry, "careers.usaid.gov", FakeDns())

    assert result.verdict is Verdict.PROBLEM
    assert result.summary == "Does not resolve"
    assert "www.careers.usaid.gov" in result.evidence


def test_check_page_entry_wildcard_dns_still_problem(
    http: httpx.Client, registry: AtsRegistry
) -> None:
    # A wildcard only helps `site:` searches on a bare host; a page needs its own host.
    dns = FakeDns(f"{WILDCARD_PROBE_LABEL}.fcdo.gov.uk")

    result = _check(http, registry, "fcdo.gov.uk/about-us/careers", dns)

    assert result.verdict is Verdict.PROBLEM
    assert result.summary == "Does not resolve"


def test_check_page_entry_www_fallback_checks_www_url(
    http: httpx.Client, registry: AtsRegistry, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get("https://www.jica.go.jp/en/recruit").respond(404)

    result = _check(http, registry, "jica.go.jp/en/recruit", FakeDns("www.jica.go.jp"))

    assert route.called
    assert result.verdict is Verdict.PROBLEM
    assert result.summary == "Page not found (HTTP 404)"
    assert result.evidence.startswith("jica.go.jp has no DNS record; checked www.jica.go.jp")


def test_resolves_transient_failure_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def flaky(host: str, port: int) -> list[object]:
        calls.append(host)
        if len(calls) == 1:
            raise socket.gaierror("temporary failure")
        return []

    monkeypatch.setattr(socket, "getaddrinfo", flaky)

    assert resolves("example.org", delay_seconds=0)
    assert calls == ["example.org", "example.org"]


def test_resolves_persistent_failure_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(host: str, port: int) -> list[object]:
        raise socket.gaierror("nodename nor servname provided")

    monkeypatch.setattr(socket, "getaddrinfo", missing)

    assert not resolves("example.invalid", delay_seconds=0)


# --- Page entries ------------------------------------------------------------------------------

PAGE = "example.org/careers/vacancies"
PAGE_URL = "https://example.org/careers/vacancies"
PAGE_DNS = ("example.org",)


def test_check_page_loads_ok(
    http: httpx.Client, registry: AtsRegistry, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(PAGE_URL).respond(200, html="<h1>Vacancies</h1>")

    result = _check(http, registry, PAGE, FakeDns(*PAGE_DNS))

    assert result.verdict is Verdict.OK
    assert result.evidence == f"HTTP 200 at {PAGE_URL}"


@pytest.mark.parametrize("status", [404, 410])
def test_check_page_gone_problem(
    http: httpx.Client, registry: AtsRegistry, respx_mock: respx.MockRouter, status: int
) -> None:
    respx_mock.get(PAGE_URL).respond(status)

    result = _check(http, registry, PAGE, FakeDns(*PAGE_DNS))

    assert result.verdict is Verdict.PROBLEM
    assert result.summary == f"Page not found (HTTP {status})"


@pytest.mark.parametrize("status", [401, 403, 406, 429, 451])
def test_check_page_bot_block_unverified(
    http: httpx.Client, registry: AtsRegistry, respx_mock: respx.MockRouter, status: int
) -> None:
    respx_mock.get(PAGE_URL).respond(status)

    result = _check(http, registry, PAGE, FakeDns(*PAGE_DNS))

    assert result.verdict is Verdict.UNVERIFIED
    assert f"HTTP {status}" in result.summary


def test_check_page_redirect_to_home_page_problem_even_when_blocked(
    http: httpx.Client, registry: AtsRegistry, respx_mock: respx.MockRouter
) -> None:
    # Seen live: afdb.org/en/about-careers/current-vacancies -> 403 at https://www.afdb.org/
    respx_mock.get(PAGE_URL).respond(301, headers={"Location": "https://www.example.org/"})
    respx_mock.get("https://www.example.org/").respond(403)

    result = _check(http, registry, PAGE, FakeDns(*PAGE_DNS))

    assert result.verdict is Verdict.PROBLEM
    assert result.summary == "Redirects to the home page"


def test_check_page_moved_to_other_site_problem(
    http: httpx.Client, registry: AtsRegistry, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(PAGE_URL).respond(301, headers={"Location": "https://careers.example.net/jobs"})
    respx_mock.get("https://careers.example.net/jobs").respond(200)

    result = _check(http, registry, PAGE, FakeDns(*PAGE_DNS))

    assert result.verdict is Verdict.PROBLEM
    assert result.summary == "Moved to careers.example.net"


def test_check_page_redirect_to_www_same_page_ok(
    http: httpx.Client, registry: AtsRegistry, respx_mock: respx.MockRouter
) -> None:
    target = "https://www.example.org/careers/vacancies/"
    respx_mock.get(PAGE_URL).respond(301, headers={"Location": target})
    respx_mock.get(target).respond(200)

    result = _check(http, registry, PAGE, FakeDns(*PAGE_DNS))

    assert result.verdict is Verdict.OK


def test_check_page_persistent_server_error_problem(
    http: httpx.Client, registry: AtsRegistry, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(PAGE_URL).respond(503)

    result = _check(http, registry, PAGE, FakeDns(*PAGE_DNS))

    assert result.verdict is Verdict.PROBLEM
    assert result.summary == "HTTP 503"
    assert route.call_count == 3


def test_check_page_certificate_error_problem_not_retried(
    http: httpx.Client, registry: AtsRegistry, respx_mock: respx.MockRouter
) -> None:
    error = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate has expired")
    route = respx_mock.get(PAGE_URL).mock(side_effect=error)

    result = _check(http, registry, PAGE, FakeDns(*PAGE_DNS))

    assert result.verdict is Verdict.PROBLEM
    assert result.summary == "TLS certificate error"
    assert "certificate has expired" in result.evidence
    assert route.call_count == 1


def test_check_page_timeout_unreachable_after_retries(
    http: httpx.Client, registry: AtsRegistry, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(PAGE_URL).mock(side_effect=httpx.ReadTimeout("timed out"))

    result = _check(http, registry, PAGE, FakeDns(*PAGE_DNS))

    assert result.verdict is Verdict.PROBLEM
    assert result.summary == "Unreachable"
    assert "ReadTimeout" in result.evidence
    assert route.call_count == 3


def test_check_page_redirect_loop_problem(
    http: httpx.Client, registry: AtsRegistry, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(PAGE_URL).respond(302, headers={"Location": PAGE_URL})

    result = _check(http, registry, PAGE, FakeDns(*PAGE_DNS))

    assert result.verdict is Verdict.PROBLEM
    assert result.summary == "Redirect loop"


# --- ATS board hosts ---------------------------------------------------------------------------


def test_check_board_host_healthy_ok(
    http: httpx.Client, registry: AtsRegistry, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get("https://job-boards.greenhouse.io/discord").respond(200)

    result = _check(
        http,
        registry,
        "job-boards.greenhouse.io",
        FakeDns("job-boards.greenhouse.io"),
        {"Greenhouse": HEALTHY_GREENHOUSE},
    )

    assert route.called
    assert result.verdict is Verdict.OK
    assert result.evidence.startswith("discord: 5 jobs; gitlab: 5 jobs;")


def test_check_board_host_root_redirect_ignored(
    http: httpx.Client, registry: AtsRegistry, respx_mock: respx.MockRouter
) -> None:
    # The root redirects to the vendor's marketing site; only board pages matter.
    respx_mock.get("https://job-boards.greenhouse.io/").respond(
        301, headers={"Location": "https://www.greenhouse.com"}
    )
    respx_mock.get("https://job-boards.greenhouse.io/discord").respond(200)

    result = _check(
        http,
        registry,
        "job-boards.greenhouse.io",
        FakeDns("job-boards.greenhouse.io"),
        {"Greenhouse": HEALTHY_GREENHOUSE},
    )

    assert result.verdict is Verdict.OK


def test_check_board_host_boards_moved_problem(
    http: httpx.Client, registry: AtsRegistry, respx_mock: respx.MockRouter
) -> None:
    # Seen live: boards.greenhouse.io/discord -> job-boards.greenhouse.io/discord
    respx_mock.get("https://boards.greenhouse.io/discord").respond(
        301, headers={"Location": "https://job-boards.greenhouse.io/discord"}
    )
    respx_mock.get("https://job-boards.greenhouse.io/discord").respond(200)

    result = _check(
        http,
        registry,
        "boards.greenhouse.io",
        FakeDns("boards.greenhouse.io"),
        {"Greenhouse": HEALTHY_GREENHOUSE},
    )

    assert result.verdict is Verdict.PROBLEM
    assert result.summary == "Boards moved to job-boards.greenhouse.io"


def test_check_board_host_uses_first_canary_with_jobs(
    http: httpx.Client, registry: AtsRegistry, respx_mock: respx.MockRouter
) -> None:
    platforms = {
        "Greenhouse": PlatformHealth(
            "Greenhouse",
            (_probe("Greenhouse", "discord", jobs=0), _probe("Greenhouse", "gitlab")),
        )
    }
    route = respx_mock.get("https://job-boards.greenhouse.io/gitlab").respond(200)

    result = _check(
        http, registry, "job-boards.greenhouse.io", FakeDns("job-boards.greenhouse.io"), platforms
    )

    assert route.called
    assert result.verdict is Verdict.OK


def test_check_board_host_no_healthy_canary_problem(
    http: httpx.Client, registry: AtsRegistry
) -> None:
    platforms = {
        "Lever": PlatformHealth(
            "Lever", (_probe("Lever", "spotify", status=404), _probe("Lever", "palantir", jobs=0))
        )
    }

    result = _check(http, registry, "jobs.lever.co", FakeDns("jobs.lever.co"), platforms)

    assert result.verdict is Verdict.PROBLEM
    assert result.summary == "No known Lever board lists jobs"
    assert result.evidence == "spotify: HTTP 404; palantir: 0 jobs"


def test_check_board_host_board_page_404_problem(
    http: httpx.Client, registry: AtsRegistry, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("https://job-boards.greenhouse.io/discord").respond(404)

    result = _check(
        http,
        registry,
        "job-boards.greenhouse.io",
        FakeDns("job-boards.greenhouse.io"),
        {"Greenhouse": HEALTHY_GREENHOUSE},
    )

    assert result.verdict is Verdict.PROBLEM
    assert result.summary == "Board page not found on this host"


def test_check_board_host_without_dns_problem_before_board_checks(
    http: httpx.Client, registry: AtsRegistry
) -> None:
    result = _check(
        http, registry, "job-boards.greenhouse.io", FakeDns(), {"Greenhouse": HEALTHY_GREENHOUSE}
    )

    assert result.verdict is Verdict.PROBLEM
    assert result.summary == "Does not resolve"


def test_probe_platform_probes_every_canary(
    http: httpx.Client, registry: AtsRegistry, respx_mock: respx.MockRouter
) -> None:
    api = "https://api.lever.co/v0/postings"
    respx_mock.get(f"{api}/spotify").respond(404, json={"ok": False})
    respx_mock.get(f"{api}/palantir").respond(200, json=[{"id": "a"}])

    health = probe_platform(registry.get("Lever"))

    assert [p.slug for p in health.probes] == ["spotify", "palantir"]
    assert health.healthy_slug == "palantir"


def test_platform_health_no_canary_with_jobs_returns_none() -> None:
    health = PlatformHealth("Ashby", (_probe("Ashby", "linear", jobs=0),))

    assert health.healthy_slug is None


def test_is_certificate_error_only_verification_failures() -> None:
    cert = ssl.SSLCertVerificationError("certificate verify failed")
    wrapped = httpx.ConnectError("connect failed")
    wrapped.__cause__ = cert

    assert is_certificate_error(wrapped)
    assert not is_certificate_error(httpx.ConnectError(str(ssl.SSLEOFError("EOF occurred"))))
