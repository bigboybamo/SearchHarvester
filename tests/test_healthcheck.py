import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from harvester import config, healthcheck
from harvester.ats.registry import default_registry
from harvester.config import parse_domain_entry
from harvester.github import Issue
from harvester.health_probes import CheckResult, Verdict
from harvester.healthcheck import (
    ISSUE_TITLE_PREFIX,
    HealthReport,
    distinct_entries,
    issue_title,
    load_state,
    render_issue_body,
    run_checks,
    save_state,
    sync_issue,
)
from harvester.models import AtsSourceGroup

CHECKED_AT = datetime(2026, 10, 19, 6, 17, tzinfo=UTC)


def _group(group_id: int, *domains: str, name: str | None = None) -> AtsSourceGroup:
    return AtsSourceGroup(
        id=group_id,
        name=name or f"Group {group_id}",
        domains=tuple(parse_domain_entry(d) for d in domains),
    )


def _result(
    entry: str, verdict: Verdict, summary: str = "Summary", evidence: str = ""
) -> CheckResult:
    return CheckResult(entry, (1,), verdict, summary, evidence)


def _report(
    *results: CheckResult,
    previous: frozenset[str] | None = None,
    tracking: tuple[str, ...] = (),
    corrupted: tuple[int, ...] = (),
) -> HealthReport:
    return HealthReport(results, previous, tracking, corrupted)


class FakeIssues:
    """In-memory stand-in for `GitHubIssues`, recording every write."""

    def __init__(self, existing: Issue | None = None) -> None:
        self.existing = existing
        self.calls: list[tuple[object, ...]] = []

    def find_open_issue(self, title_prefix: str) -> Issue | None:
        self.calls.append(("find", title_prefix))
        return self.existing

    def create(self, title: str, body: str, labels: list[str]) -> Issue:
        self.calls.append(("create", title, labels))
        return Issue(number=99, title=title, html_url="https://example.test/99")

    def update(self, number: int, title: str, body: str) -> Issue:
        self.calls.append(("update", number, title))
        return Issue(number=number, title=title, html_url="https://example.test")

    def comment(self, number: int, body: str) -> None:
        self.calls.append(("comment", number))

    def close(self, number: int) -> None:
        self.calls.append(("close", number))


OPEN_ISSUE = Issue(number=12, title=f"{ISSUE_TITLE_PREFIX} 2 problem(s)", html_url="https://x")


# --- distinct_entries / run_checks -------------------------------------------------------------


def test_distinct_entries_shared_entry_lists_every_group() -> None:
    groups = [_group(4, "a.com", "b.com"), _group(6, "b.com"), _group(9, "c.com")]

    entries = distinct_entries(groups)

    assert [(entry.raw, ids) for entry, ids in entries] == [
        ("a.com", (4,)),
        ("b.com", (4, 6)),
        ("c.com", (9,)),
    ]


def test_run_checks_end_to_end_classifies_and_collects_notes(
    http: httpx.Client, respx_mock: respx.MockRouter
) -> None:
    api = "https://api.lever.co/v0/postings"
    respx_mock.get(f"{api}/spotify").respond(200, json=[{"id": "a"}])
    respx_mock.get(f"{api}/palantir").respond(200, json=[{"id": "b"}])
    respx_mock.get("https://jobs.lever.co/spotify").respond(200)
    respx_mock.get("https://example.org/jobs").respond(404)
    groups = [
        _group(1, "jobs.lever.co", "gone.example", name="Set #1 � Broken"),
        _group(2, "example.org/jobs?_gl=1*abc", ".ai"),
    ]

    report = run_checks(
        groups,
        http,
        default_registry(http),
        previous_failures=frozenset({"gone.example"}),
        resolve=lambda host: host in {"jobs.lever.co", "example.org"},
    )

    verdicts = {r.entry: r.verdict for r in report.results}
    assert verdicts == {
        "jobs.lever.co": Verdict.OK,
        "gone.example": Verdict.PROBLEM,
        "example.org/jobs?_gl=1*abc": Verdict.PROBLEM,
        ".ai": Verdict.SKIPPED,
    }
    assert [r.entry for r in report.confirmed] == ["gone.example"]
    assert [r.entry for r in report.pending] == ["example.org/jobs?_gl=1*abc"]
    assert report.tracking_entries == ("example.org/jobs?_gl=1*abc",)
    assert report.corrupted_group_ids == (1,)


def test_report_no_previous_run_nothing_confirmed() -> None:
    report = _report(_result("a.com", Verdict.PROBLEM), previous=None)

    assert report.confirmed == []
    assert [r.entry for r in report.pending] == ["a.com"]


def test_report_recovered_entry_neither_confirmed_nor_pending() -> None:
    report = _report(_result("a.com", Verdict.OK), previous=frozenset({"a.com"}))

    assert report.confirmed == []
    assert report.pending == []
    assert report.failing_entries == []


def test_report_unverified_never_counts_as_failing() -> None:
    report = _report(_result("a.com", Verdict.UNVERIFIED), previous=frozenset({"a.com"}))

    assert report.confirmed == []
    assert [r.entry for r in report.unverified] == ["a.com"]
    assert report.failing_entries == []


# --- State -------------------------------------------------------------------------------------


def test_save_then_load_state_round_trips_failing_entries(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    report = _report(_result("b.com", Verdict.PROBLEM), _result("a.com", Verdict.PROBLEM))

    save_state(path, report, CHECKED_AT)

    assert json.loads(path.read_text())["failing"] == ["a.com", "b.com"]
    assert load_state(path) == frozenset({"a.com", "b.com"})


def test_load_state_missing_file_returns_none(tmp_path: Path) -> None:
    assert load_state(tmp_path / "missing.json") is None
    assert load_state(None) is None


@pytest.mark.parametrize(
    "content",
    [b"{not json", b'{"version": 1}', b'{"version": 99, "checked_at": "2026-01-01T00:00:00Z", '
     b'"failing": []}'],
)  # fmt: skip
def test_load_state_unusable_file_returns_none(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(content)

    assert load_state(path) is None


# --- Issue body --------------------------------------------------------------------------------


def test_issue_title_uses_count() -> None:
    assert issue_title(3) == "ATS domain health check: 3 problem(s)"


def test_render_issue_body_confirmed_problems_in_main_table() -> None:
    report = _report(
        _result("gone.example", Verdict.PROBLEM, "Does not resolve", "No DNS record"),
        previous=frozenset({"gone.example"}),
    )

    body = render_issue_body(report, CHECKED_AT, run_url="https://github.com/o/r/actions/runs/1")

    assert "found **1 problem(s)** that failed on two consecutive runs" in body
    assert "| `gone.example` | #1 | Does not resolve | No DNS record |" in body
    assert "Checked 2026-10-19 06:17 UTC" in body
    assert "([workflow run](https://github.com/o/r/actions/runs/1))" in body
    assert "<details>" not in body


def test_render_issue_body_optional_sections_only_when_present() -> None:
    report = _report(
        _result("a.com", Verdict.PROBLEM),
        _result("new.com", Verdict.PROBLEM),
        _result("blocked.com", Verdict.UNVERIFIED, "Blocks automated requests (HTTP 403)"),
        previous=frozenset({"a.com"}),
        tracking=("t.org/x?_gl=1",),
        corrupted=(1, 2),
    )

    body = render_issue_body(report, CHECKED_AT, run_url=None)

    assert "<summary>First seen this run (1)</summary>" in body
    assert "<summary>Could not verify (1)</summary>" in body
    assert "- `t.org/x?_gl=1` contains a Google Analytics tracking token" in body
    assert "Group names for #1, #2 contain `�`" in body
    assert "workflow run" not in body


def test_render_issue_body_pipes_and_newlines_escaped() -> None:
    report = _report(
        _result("a.com/x|y", Verdict.PROBLEM, "Bad | thing", "line1\nline2"),
        previous=frozenset({"a.com/x|y"}),
    )

    body = render_issue_body(report, CHECKED_AT, run_url=None)

    assert r"| `a.com/x\|y` | #1 | Bad \| thing | line1<br>line2 |" in body


# --- sync_issue --------------------------------------------------------------------------------

CONFIRMED = _report(_result("a.com", Verdict.PROBLEM), previous=frozenset({"a.com"}))
ALL_CLEAR = _report(_result("a.com", Verdict.OK), previous=frozenset({"a.com"}))


def test_sync_issue_problems_no_open_issue_creates_with_bug_label() -> None:
    fake = FakeIssues()

    action = sync_issue(fake, CONFIRMED, "body", CHECKED_AT)  # type: ignore[arg-type]

    assert action == "opened issue #99"
    assert fake.calls[-1] == ("create", "ATS domain health check: 1 problem(s)", ["bug"])


def test_sync_issue_problems_open_issue_edited_never_duplicated() -> None:
    fake = FakeIssues(existing=OPEN_ISSUE)

    action = sync_issue(fake, CONFIRMED, "body", CHECKED_AT)  # type: ignore[arg-type]

    assert action == "updated issue #12"
    assert ("update", 12, "ATS domain health check: 1 problem(s)") in fake.calls
    assert not any(call[0] == "create" for call in fake.calls)


def test_sync_issue_all_clear_open_issue_commented_and_closed() -> None:
    fake = FakeIssues(existing=OPEN_ISSUE)

    action = sync_issue(fake, ALL_CLEAR, "body", CHECKED_AT)  # type: ignore[arg-type]

    assert action == "closed issue #12"
    assert fake.calls[1:] == [("comment", 12), ("close", 12)]


def test_sync_issue_all_clear_no_open_issue_does_nothing() -> None:
    fake = FakeIssues()

    action = sync_issue(fake, ALL_CLEAR, "body", CHECKED_AT)  # type: ignore[arg-type]

    assert action == "no issue needed"
    assert fake.calls == [("find", ISSUE_TITLE_PREFIX)]


def test_sync_issue_only_pending_problems_no_issue_opened() -> None:
    fake = FakeIssues()
    pending_only = _report(_result("a.com", Verdict.PROBLEM), previous=frozenset())

    action = sync_issue(fake, pending_only, "body", CHECKED_AT)  # type: ignore[arg-type]

    assert action == "no issue needed"


# --- main --------------------------------------------------------------------------------------


@pytest.fixture
def appsettings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "appsettings.json"
    groups = [{"Id": 1, "Name": "G", "Domains": ["gone.example", ".ai"]}]
    path.write_text(json.dumps({"AtsSourceGroups": groups}), encoding="utf-8")
    monkeypatch.setenv("JSB_APPSETTINGS_URL", str(path))
    monkeypatch.delenv("JSB_GITHUB_TOKEN", raising=False)
    # Keep a developer's local .env (which may hold a real token) out of the test.
    monkeypatch.setattr(config, "load_dotenv", lambda **_: None)
    return path


def _no_dns(host: str) -> bool:
    return False


def test_main_dry_run_prints_report_and_writes_state(
    appsettings: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "out.json"

    code = healthcheck.main(["--dry-run", "--state-out", str(state)], resolve=_no_dns)

    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("# ATS domain health check: 0 problem(s)")
    assert "First seen this run (1)" in out
    assert out.rstrip().endswith("1 new, 0 unverified; dry run, GitHub not contacted")
    assert json.loads(state.read_text())["failing"] == ["gone.example"]


def test_main_second_failing_run_opens_issue(
    appsettings: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: respx.MockRouter,
) -> None:
    previous = tmp_path / "previous.json"
    previous.write_text(
        json.dumps(
            {"version": 1, "checked_at": "2026-10-12T06:17:00Z", "failing": ["gone.example"]}
        )
    )
    monkeypatch.setenv("JSB_GITHUB_TOKEN", "tok")
    issues = "https://api.github.com/repos/bigboybamo/JobSearchBuilder/issues"
    respx_mock.get(issues).respond(200, json=[])
    create = respx_mock.post(issues).respond(
        201, json={"number": 50, "title": "t", "html_url": "https://github.com/x/50"}
    )

    code = healthcheck.main(["--state-in", str(previous)], resolve=_no_dns)

    assert code == 0
    payload = json.loads(create.calls.last.request.content)
    assert payload["title"] == "ATS domain health check: 1 problem(s)"
    assert payload["labels"] == ["bug"]
    assert "`gone.example`" in payload["body"]
    assert create.calls.last.request.headers["Authorization"] == "Bearer tok"


def test_main_without_token_and_not_dry_run_exits_2(
    appsettings: Path, respx_mock: respx.MockRouter
) -> None:
    assert healthcheck.main([], resolve=_no_dns) == 2
    assert not respx_mock.calls
