"""Weekly health check of JobSearchBuilder's `AtsSourceGroups` entries.

Probes every entry, then keeps **one** issue on JobSearchBuilder in step with the result. An entry
is only reported once it has failed on two consecutive runs, so a one-off network blip never
reaches the issue. The previous run's failures are read from, and this run's written to, a small
JSON state file that the workflow carries between runs as an artifact.

    python -m harvester.healthcheck --dry-run      # print the issue body, don't touch GitHub
"""

import argparse
import logging
import os
import sys
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, ValidationError

from harvester.ats.registry import AtsRegistry, default_registry
from harvester.config import ConfigError, load_settings, load_source_groups
from harvester.github import GitHubIssues, create_github_client
from harvester.health_probes import (
    CheckResult,
    EntryChecker,
    PlatformHealth,
    Resolver,
    Verdict,
    probe_platform,
    resolves,
)
from harvester.httpclient import create_client
from harvester.models import AtsSourceGroup, DomainEntry, DomainKind

logger = logging.getLogger(__name__)

ISSUE_REPO = "bigboybamo/JobSearchBuilder"
ISSUE_TITLE_PREFIX = "ATS domain health check:"
ISSUE_LABELS = ["bug"]
APPSETTINGS_BLOB_URL = (
    "https://github.com/bigboybamo/JobSearchBuilder/blob/master/JobSearchBuilder/appsettings.json"
)
MAX_CONCURRENCY = 5
STATE_VERSION = 1


# --- Running the checks ------------------------------------------------------------------------


@dataclass(frozen=True)
class HealthReport:
    results: tuple[CheckResult, ...]
    previous_failures: frozenset[str] | None
    """Entries that failed on the previous run; `None` when there is no previous run."""

    tracking_entries: tuple[str, ...]
    corrupted_group_ids: tuple[int, ...]

    def _problems(self) -> list[CheckResult]:
        return [r for r in self.results if r.verdict is Verdict.PROBLEM]

    @property
    def confirmed(self) -> list[CheckResult]:
        """Problems that also failed last run. Only these are reported."""
        previous = self.previous_failures or frozenset()
        return [r for r in self._problems() if r.entry in previous]

    @property
    def pending(self) -> list[CheckResult]:
        """Problems seen for the first time; reported if they fail again next run."""
        previous = self.previous_failures or frozenset()
        return [r for r in self._problems() if r.entry not in previous]

    @property
    def unverified(self) -> list[CheckResult]:
        return [r for r in self.results if r.verdict is Verdict.UNVERIFIED]

    @property
    def failing_entries(self) -> list[str]:
        return sorted(r.entry for r in self._problems())


def distinct_entries(
    groups: Iterable[AtsSourceGroup],
) -> list[tuple[DomainEntry, tuple[int, ...]]]:
    """Each distinct entry once, with every group id that lists it, in file order."""
    by_raw: dict[str, tuple[DomainEntry, list[int]]] = {}
    for group in groups:
        for entry in group.domains:
            by_raw.setdefault(entry.raw, (entry, []))[1].append(group.id)
    return [(entry, tuple(ids)) for entry, ids in by_raw.values()]


def run_checks(
    groups: Sequence[AtsSourceGroup],
    http: httpx.Client,
    registry: AtsRegistry,
    previous_failures: frozenset[str] | None,
    resolve: Resolver = resolves,
) -> HealthReport:
    entries = distinct_entries(groups)
    board_clients = {
        client.name: client
        for entry, _ in entries
        if entry.kind is DomainKind.HOST
        and entry.url is not None
        and (client := registry.client_for(entry.url)) is not None
    }
    platforms: dict[str, PlatformHealth] = {
        name: probe_platform(client) for name, client in board_clients.items()
    }
    checker = EntryChecker(http, registry, platforms, resolve)

    logger.info("Checking %d distinct entries from %d groups", len(entries), len(groups))
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as pool:
        results = tuple(pool.map(lambda item: checker.check(*item), entries))

    return HealthReport(
        results=results,
        previous_failures=previous_failures,
        tracking_entries=tuple(entry.raw for entry, _ in entries if entry.tracking_params),
        corrupted_group_ids=tuple(group.id for group in groups if group.has_corrupted_name),
    )


# --- State between runs ------------------------------------------------------------------------


class _State(BaseModel):
    version: int
    checked_at: datetime
    failing: list[str]


def load_state(path: Path | None) -> frozenset[str] | None:
    """Entries that failed last run, or `None` if there is no usable previous state."""
    if path is None or not path.is_file():
        logger.info("No previous state; new failures will wait a run before being reported")
        return None
    try:
        state = _State.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as exc:
        logger.warning("Ignoring unreadable state file %s: %s", path, exc)
        return None
    if state.version != STATE_VERSION:
        logger.warning("Ignoring state file %s with version %d", path, state.version)
        return None
    return frozenset(state.failing)


def save_state(path: Path, report: HealthReport, checked_at: datetime) -> None:
    state = _State(version=STATE_VERSION, checked_at=checked_at, failing=report.failing_entries)
    path.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")


# --- Issue text --------------------------------------------------------------------------------


def issue_title(problem_count: int) -> str:
    return f"{ISSUE_TITLE_PREFIX} {problem_count} problem(s)"


def render_issue_body(report: HealthReport, checked_at: datetime, run_url: str | None) -> str:
    confirmed = report.confirmed
    lines = [
        f"The weekly check of every `AtsSourceGroups` entry in [`appsettings.json`]"
        f"({APPSETTINGS_BLOB_URL}) found **{len(confirmed)} problem(s)** that failed on two "
        "consecutive runs.",
        "",
        *_table(confirmed),
    ]
    if report.pending:
        lines += _details(
            f"First seen this run ({len(report.pending)})",
            "Reported here if they fail again next run.",
            _table(report.pending),
        )
    if report.unverified:
        lines += _details(
            f"Could not verify ({len(report.unverified)})",
            "These sites block automated requests, so they were neither passed nor failed.",
            _table(report.unverified),
        )
    notes = _notes(report)
    if notes:
        lines += _details("Notes", "Worth fixing, but not failures.", notes)

    run = f" ([workflow run]({run_url}))" if run_url else ""
    lines += [
        "",
        "---",
        f"Checked {checked_at:%Y-%m-%d %H:%M} UTC by the "
        f"[SearchHarvester](https://github.com/bigboybamo/SearchHarvester) health check{run}. "
        "This issue is edited in place on every run and closed when all entries pass, so manual "
        "edits to this description will be overwritten. Comments are kept.",
    ]
    return "\n".join(lines) + "\n"


def render_dry_run(report: HealthReport, checked_at: datetime) -> str:
    """Issue body plus a summary of everything that passed, for local inspection."""
    passed = [r for r in report.results if r.verdict in (Verdict.OK, Verdict.SKIPPED)]
    return "\n".join(
        [
            f"# {issue_title(len(report.confirmed))}",
            "",
            render_issue_body(report, checked_at, run_url=None),
            f"## Passed ({len(passed)})",
            "",
            *_table(passed),
            "",
        ]
    )


def _table(results: Sequence[CheckResult]) -> list[str]:
    rows = [
        f"| {_code(r.entry)} | {', '.join(f'#{i}' for i in r.group_ids)} | {_cell(r.summary)} "
        f"| {_cell(r.evidence)} |"
        for r in sorted(results, key=lambda r: (r.group_ids, r.entry))
    ]
    return ["| Entry | Groups | Result | Evidence |", "|---|---|---|---|", *rows]


def _details(summary: str, intro: str, body: list[str]) -> list[str]:
    return [
        "",
        "<details>",
        f"<summary>{summary}</summary>",
        "",
        intro,
        "",
        *body,
        "",
        "</details>",
    ]


def _notes(report: HealthReport) -> list[str]:
    notes = [
        f"- {_code(entry)} contains a Google Analytics tracking token (`_gl` / `_ga`)."
        for entry in report.tracking_entries
    ]
    if report.corrupted_group_ids:
        ids = ", ".join(f"#{group_id}" for group_id in report.corrupted_group_ids)
        notes.append(
            f"- Group names for {ids} contain `\N{REPLACEMENT CHARACTER}` where an em dash "
            "should be; the file was probably saved with the wrong encoding."
        )
    return notes


def _code(text: str) -> str:
    return f"`{text.replace('`', "'")}`".replace("|", "\\|")


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", "<br>")


# --- GitHub ------------------------------------------------------------------------------------


def sync_issue(issues: GitHubIssues, report: HealthReport, body: str, checked_at: datetime) -> str:
    """Create, edit or close the single health-check issue. Returns what was done."""
    existing = issues.find_open_issue(ISSUE_TITLE_PREFIX)
    confirmed = len(report.confirmed)
    if confirmed:
        if existing is not None:
            issues.update(existing.number, issue_title(confirmed), body)
            return f"updated issue #{existing.number}"
        created = issues.create(issue_title(confirmed), body, ISSUE_LABELS)
        return f"opened issue #{created.number}"
    if existing is not None:
        issues.comment(
            existing.number,
            f"Every entry passed the health check on {checked_at:%Y-%m-%d}. Closing.",
        )
        issues.close(existing.number)
        return f"closed issue #{existing.number}"
    return "no issue needed"


# --- CLI ---------------------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m harvester.healthcheck", description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="print the report; don't touch GitHub"
    )
    parser.add_argument("--state-in", type=Path, help="state file from the previous run")
    parser.add_argument("--state-out", type=Path, help="where to write this run's state")
    parser.add_argument(
        "--repo", default=ISSUE_REPO, help=f"issue repository (default {ISSUE_REPO})"
    )
    return parser.parse_args(argv)


def _run_url() -> str | None:
    server, repo, run_id = (
        os.environ.get(name) for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")
    )
    return f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else None


def main(argv: Sequence[str] | None = None, *, resolve: Resolver = resolves) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)
    try:
        return _run(args, resolve)
    except ConfigError as exc:
        logger.error("%s", exc)
        return 2


def _run(args: argparse.Namespace, resolve: Resolver) -> int:
    settings = load_settings()
    # No token means dry run; checked before probing so a misconfigured run fails fast.
    token = None if args.dry_run else settings.github_token
    if not args.dry_run and token is None:
        raise ConfigError("JSB_GITHUB_TOKEN is required unless --dry-run is given")

    checked_at = datetime.now(UTC)
    with create_client() as http:
        groups = load_source_groups(settings.appsettings_url, http)
        report = run_checks(
            groups, http, default_registry(http), load_state(args.state_in), resolve
        )

    if args.state_out is not None:
        save_state(args.state_out, report, checked_at)

    if token is None:
        sys.stdout.write(render_dry_run(report, checked_at))
        action = "dry run, GitHub not contacted"
    else:
        body = render_issue_body(report, checked_at, _run_url())
        with create_github_client(token) as github_http:
            action = sync_issue(GitHubIssues(github_http, args.repo), report, body, checked_at)

    sys.stdout.write(
        f"Health check: {len(report.results)} entries, {len(report.confirmed)} confirmed "
        f"problem(s), {len(report.pending)} new, {len(report.unverified)} unverified; {action}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
