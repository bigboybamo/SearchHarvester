"""Side-by-side comparison of search providers on real JobSearchBuilder queries.

Decides the open "Brave vs SerpAPI" question. What matters is discovery: how many distinct ATS
company boards each provider finds per paid search, and whether it copes with JobSearchBuilder's
long `site:` / `OR` queries at all.

    python -m harvester.search.compare queries.txt
    python -m harvester.search.compare queries.txt --pages 2 --max-searches 30 --out report.md

`queries.txt` holds one query per line, as JobSearchBuilder generated it. Blank lines and lines
starting with `#` are ignored. The total number of paid searches is checked against
`--max-searches` before any request is made.
"""

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from harvester.ats.registry import AtsRegistry, default_registry
from harvester.config import ConfigError, load_settings
from harvester.httpclient import create_client
from harvester.models import SearchHit
from harvester.search.base import SearchError, SearchProvider
from harvester.search.factory import create_provider

logger = logging.getLogger(__name__)

DEFAULT_PROVIDERS = ("brave", "serpapi")
DEFAULT_MAX_SEARCHES = 20
MAX_LISTED_BOARDS = 10

Board = tuple[str, str]
"""(ATS name, company slug)."""


@dataclass
class ProviderRun:
    provider: str
    searches: int = 0
    hits: list[SearchHit] = field(default_factory=list)
    boards: set[Board] = field(default_factory=set)
    ats_hits: int = 0
    """Hits on a supported ATS host, whether or not a slug could be read from them."""

    error: str | None = None

    @property
    def boards_per_search(self) -> float:
        return len(self.boards) / self.searches if self.searches else 0.0


@dataclass
class QueryComparison:
    query: str
    runs: list[ProviderRun]

    @property
    def characters(self) -> int:
        return len(self.query)

    @property
    def words(self) -> int:
        return len(self.query.split())


def read_queries(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def planned_searches(
    queries: Sequence[str], providers: Sequence[SearchProvider], pages: int
) -> int:
    """The most paid searches a comparison can make (paging stops early on short pages)."""
    return len(queries) * sum(min(pages, provider.max_pages) for provider in providers)


def run_provider(
    provider: SearchProvider, query: str, pages: int, registry: AtsRegistry
) -> ProviderRun:
    run = ProviderRun(provider.name)
    for page in range(min(pages, provider.max_pages)):
        run.searches += 1
        try:
            hits = provider.search(query, page)
        except SearchError as exc:
            run.error = str(exc)
            logger.warning("%s failed on page %d: %s", provider.name, page, exc)
            break
        run.hits.extend(hits)
        if len(hits) < provider.page_size:
            break

    for hit in run.hits:
        resolved = registry.resolve(hit.url)
        if registry.client_for(hit.url) is not None:
            run.ats_hits += 1
        if resolved is not None:
            client, slug = resolved
            run.boards.add((client.name, slug))
    return run


def compare(
    queries: Sequence[str],
    providers: Sequence[SearchProvider],
    registry: AtsRegistry,
    pages: int = 1,
) -> list[QueryComparison]:
    return [
        QueryComparison(query, [run_provider(p, query, pages, registry) for p in providers])
        for query in queries
    ]


# --- Report ------------------------------------------------------------------------------------


def render_report(comparisons: Sequence[QueryComparison]) -> str:
    lines = ["# Search provider comparison", "", *_summary(comparisons)]
    for number, comparison in enumerate(comparisons, start=1):
        lines += [
            "",
            f"## Query {number}",
            "",
            f"{comparison.characters} characters, {comparison.words} words",
            "",
            "```",
            comparison.query,
            "```",
            "",
            *_run_table(comparison.runs),
            *_overlap(comparison.runs),
        ]
    return "\n".join(lines) + "\n"


def _summary(comparisons: Sequence[QueryComparison]) -> list[str]:
    totals: dict[str, ProviderRun] = {}
    failures: dict[str, int] = {}
    for comparison in comparisons:
        for run in comparison.runs:
            total = totals.setdefault(run.provider, ProviderRun(run.provider))
            total.searches += run.searches
            total.hits += run.hits
            total.ats_hits += run.ats_hits
            total.boards |= run.boards
            failures[run.provider] = failures.get(run.provider, 0) + (run.error is not None)
    rows = [
        f"| {name} | {total.searches} | {len(total.hits)} | {total.ats_hits} | {len(total.boards)} "
        f"| {total.boards_per_search:.2f} | {failures[name]} of {len(comparisons)} |"
        for name, total in totals.items()
    ]
    return [
        f"Across {len(comparisons)} queries (distinct boards counted once overall):",
        "",
        "| Provider | Searches | Hits | ATS hits | Distinct boards | Boards per search "
        "| Queries failed |",
        "|---|---|---|---|---|---|---|",
        *rows,
    ]


def _run_table(runs: Sequence[ProviderRun]) -> list[str]:
    rows = [
        f"| {run.provider} | {run.searches} | {len(run.hits)} | {run.ats_hits} | {len(run.boards)} "
        f"| {run.boards_per_search:.2f} | {_cell(run.error or '')} |"
        for run in runs
    ]
    return [
        "| Provider | Searches | Hits | ATS hits | Boards | Boards per search | Error |",
        "|---|---|---|---|---|---|---|",
        *rows,
    ]


def _overlap(runs: Sequence[ProviderRun]) -> list[str]:
    if len(runs) != 2:
        return []
    first, second = runs
    shared = first.boards & second.boards
    lines = ["", f"Boards found by both: {len(shared)}"]
    for run, other in ((first, second), (second, first)):
        only = sorted(run.boards - other.boards)
        listed = ", ".join(f"{ats}/{slug}" for ats, slug in only[:MAX_LISTED_BOARDS])
        more = f" (+{len(only) - MAX_LISTED_BOARDS} more)" if len(only) > MAX_LISTED_BOARDS else ""
        lines.append(f"Only {run.provider}: {len(only)}" + (f": {listed}{more}" if only else ""))
    return lines


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


# --- CLI ---------------------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m harvester.search.compare", description=__doc__)
    parser.add_argument("queries", type=Path, help="file with one query per line")
    parser.add_argument(
        "--providers",
        default=",".join(DEFAULT_PROVIDERS),
        help="comma-separated providers to compare (default: brave,serpapi)",
    )
    parser.add_argument("--pages", type=int, default=1, help="pages per query and provider")
    parser.add_argument(
        "--max-searches",
        type=int,
        default=DEFAULT_MAX_SEARCHES,
        help=f"refuse to run if more paid searches are needed (default {DEFAULT_MAX_SEARCHES})",
    )
    parser.add_argument("--out", type=Path, help="also write the report to this file")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)
    try:
        queries = read_queries(args.queries)
    except OSError as exc:
        logger.error("Cannot read %s: %s", args.queries, exc)
        return 2
    if not queries:
        logger.error("%s contains no queries", args.queries)
        return 2
    if args.pages < 1:
        logger.error("--pages must be at least 1")
        return 2

    settings = load_settings()
    with create_client() as http:
        try:
            providers = [
                create_provider(settings, http, name.strip())
                for name in args.providers.split(",")
                if name.strip()
            ]
        except ConfigError as exc:
            logger.error("%s", exc)
            return 2

        planned = planned_searches(queries, providers, args.pages)
        if planned > args.max_searches:
            logger.error(
                "This comparison needs up to %d paid searches, more than --max-searches %d",
                planned,
                args.max_searches,
            )
            return 2
        logger.info("Running %d queries; up to %d paid searches", len(queries), planned)
        comparisons = compare(queries, providers, default_registry(http), args.pages)

    report = render_report(comparisons)
    if args.out is not None:
        args.out.write_text(report, encoding="utf-8")
    sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
