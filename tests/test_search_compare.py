from pathlib import Path

import httpx
import pytest

from harvester import config
from harvester.ats.registry import AtsRegistry, default_registry
from harvester.models import SearchHit
from harvester.search import compare
from harvester.search.base import SearchError
from harvester.search.compare import (
    ProviderRun,
    QueryComparison,
    planned_searches,
    read_queries,
    render_report,
    run_provider,
)


class FakeProvider:
    """Serves canned pages of URLs; a page may instead be an exception to raise."""

    def __init__(
        self, name: str, pages: list[list[str] | Exception], page_size: int = 2, max_pages: int = 10
    ) -> None:
        self.name = name
        self.page_size = page_size
        self.max_pages = max_pages
        self._pages = pages
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, page: int = 0) -> list[SearchHit]:
        self.calls.append((query, page))
        served = self._pages[page] if page < len(self._pages) else []
        if isinstance(served, Exception):
            raise served
        return [
            SearchHit(provider=self.name, rank=i + 1, title="t", url=url)
            for i, url in enumerate(served)
        ]


@pytest.fixture
def registry(http: httpx.Client) -> AtsRegistry:
    return default_registry(http)


LEVER_ACME = "https://jobs.lever.co/acme/123"
LEVER_ACME_BOARD = "https://jobs.lever.co/acme"
GREENHOUSE_BETA = "https://job-boards.greenhouse.io/beta/jobs/9"
ASHBY_GAMMA = "https://jobs.ashbyhq.com/gamma"
NOT_ATS = "https://blog.example.org/post"


# --- read_queries / planned_searches -----------------------------------------------------------


def test_read_queries_skips_blank_and_comment_lines(tmp_path: Path) -> None:
    path = tmp_path / "queries.txt"
    path.write_bytes(
        b'\xef\xbb\xbf# from JobSearchBuilder\n(site:jobs.lever.co) ("Python")\n\n  q2  \n'
    )

    assert read_queries(path) == ['(site:jobs.lever.co) ("Python")', "q2"]


def test_planned_searches_counts_pages_capped_by_provider() -> None:
    providers = [FakeProvider("a", [], max_pages=10), FakeProvider("b", [], max_pages=1)]

    assert planned_searches(["q1", "q2"], providers, pages=3) == 2 * (3 + 1)


# --- run_provider ------------------------------------------------------------------------------


def test_run_provider_counts_boards_once_and_ats_hits(registry: AtsRegistry) -> None:
    provider = FakeProvider("a", [[LEVER_ACME, LEVER_ACME_BOARD], [GREENHOUSE_BETA, NOT_ATS], []])

    run = run_provider(provider, "q", pages=3, registry=registry)

    assert run.searches == 3
    assert len(run.hits) == 4
    assert run.ats_hits == 3
    assert run.boards == {("Lever", "acme"), ("Greenhouse", "beta")}
    assert run.boards_per_search == pytest.approx(2 / 3)


def test_run_provider_short_page_stops_paging(registry: AtsRegistry) -> None:
    provider = FakeProvider("a", [[LEVER_ACME], [GREENHOUSE_BETA, ASHBY_GAMMA]])

    run = run_provider(provider, "q", pages=5, registry=registry)

    assert provider.calls == [("q", 0)]
    assert run.searches == 1


def test_run_provider_error_recorded_and_paging_stops(registry: AtsRegistry) -> None:
    provider = FakeProvider(
        "a", [[LEVER_ACME, ASHBY_GAMMA], SearchError("HTTP 422: query too long")]
    )

    run = run_provider(provider, "q", pages=3, registry=registry)

    assert run.searches == 2
    assert run.error == "HTTP 422: query too long"
    assert run.boards == {("Lever", "acme"), ("Ashby", "gamma")}


def test_run_provider_ats_host_without_slug_counts_as_ats_hit_only(
    registry: AtsRegistry,
) -> None:
    provider = FakeProvider("a", [["https://apply.workable.com/j/ABC123"]])

    run = run_provider(provider, "q", pages=1, registry=registry)

    assert run.ats_hits == 1
    assert run.boards == set()


# --- render_report -----------------------------------------------------------------------------


def _comparison(query: str, *runs: ProviderRun) -> QueryComparison:
    return QueryComparison(query, list(runs))


def test_render_report_summary_and_overlap() -> None:
    brave = ProviderRun("brave", searches=1, ats_hits=2, boards={("Lever", "acme"), ("Ashby", "g")})
    serp = ProviderRun(
        "serpapi", searches=2, ats_hits=3, boards={("Lever", "acme"), ("Greenhouse", "b")}
    )
    report = render_report([_comparison('(site:jobs.lever.co) ("Go")', brave, serp)])

    assert report.startswith("# Search provider comparison\n")
    assert "| brave | 1 | 0 | 2 | 2 | 2.00 | 0 of 1 |" in report
    assert "| serpapi | 2 | 0 | 3 | 2 | 1.00 | 0 of 1 |" in report
    assert '27 characters, 2 words\n\n```\n(site:jobs.lever.co) ("Go")\n```' in report
    assert "Boards found by both: 1" in report
    assert "Only brave: 1: Ashby/g" in report
    assert "Only serpapi: 1: Greenhouse/b" in report


def test_render_report_error_shown_and_counted() -> None:
    failed = ProviderRun("brave", searches=1, error="brave returned HTTP 422 | detail")
    ok = ProviderRun("serpapi", searches=1)

    report = render_report([_comparison("q", failed, ok)])

    assert r"| brave returned HTTP 422 \| detail |" in report
    assert "| brave | 1 | 0 | 0 | 0 | 0.00 | 1 of 1 |" in report


def test_render_report_many_unique_boards_truncated() -> None:
    many = ProviderRun("brave", searches=1, boards={("Lever", f"c{i:02}") for i in range(13)})

    report = render_report([_comparison("q", many, ProviderRun("serpapi", searches=1))])

    assert "Only brave: 13: Lever/c00," in report
    assert "(+3 more)" in report


# --- main --------------------------------------------------------------------------------------


@pytest.fixture
def queries_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("BRAVE_API_KEY", "SERPAPI_API_KEY", "SEARCH_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "load_dotenv", lambda **_: None)
    path = tmp_path / "queries.txt"
    path.write_text("q1\nq2\n", encoding="utf-8")
    return path


def test_main_over_budget_refuses_before_any_search(
    queries_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = {"brave": FakeProvider("brave", [[LEVER_ACME]]), "serpapi": FakeProvider("serpapi", [])}
    monkeypatch.setattr(compare, "create_provider", lambda settings, http, name: fakes[name])

    code = compare.main([str(queries_file), "--pages", "3", "--max-searches", "11"])

    assert code == 2
    assert fakes["brave"].calls == []
    assert fakes["serpapi"].calls == []


def test_main_runs_and_writes_report(
    queries_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fakes = {
        "brave": FakeProvider("brave", [[LEVER_ACME]]),
        "serpapi": FakeProvider("serpapi", [[LEVER_ACME, GREENHOUSE_BETA], []]),
    }
    monkeypatch.setattr(compare, "create_provider", lambda settings, http, name: fakes[name])
    out = tmp_path / "report.md"

    code = compare.main([str(queries_file), "--out", str(out)])

    assert code == 0
    assert fakes["brave"].calls == [("q1", 0), ("q2", 0)]
    printed = capsys.readouterr().out
    assert printed == out.read_text(encoding="utf-8")
    assert "## Query 2" in printed


def test_main_missing_key_exits_2(queries_file: Path) -> None:
    assert compare.main([str(queries_file)]) == 2


def test_main_empty_queries_file_exits_2(tmp_path: Path) -> None:
    path = tmp_path / "empty.txt"
    path.write_text("# nothing yet\n", encoding="utf-8")

    assert compare.main([str(path)]) == 2
