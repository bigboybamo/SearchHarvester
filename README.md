# SearchHarvester

Companion to [JobSearchBuilder](https://github.com/bigboybamo/JobSearchBuilder). Two CLI tools
sharing one ATS client layer:

- **Harvester**: discovers company boards via a search API, pulls postings from public ATS APIs,
  and writes them to the JobSearchBuilder SQL Server database.
- **Health check**: weekly GitHub Action that validates JobSearchBuilder's `AtsSourceGroups` and
  reports stale entries as a single issue.

Spec: [JobSearchBuilder#45](https://github.com/bigboybamo/JobSearchBuilder/issues/45).
Design and conventions: [CLAUDE.md](CLAUDE.md).

## Status

Early development. See the roadmap in [CLAUDE.md](CLAUDE.md#roadmap):

| Tool | Command | Status |
|---|---|---|
| Health check | `python -m harvester.healthcheck` | Available |
| Harvester | `python -m harvester.harvest` | Phase 7 (needs new tables in JobSearchBuilder) |

The shared ATS clients for Greenhouse, Lever, Ashby and Workable live in `harvester.ats`; the
Brave and SerpAPI search providers live in `harvester.search`. The search providers are built
from their documentation and have not yet been verified against live calls.

## Setup (Windows)

Requires Python 3.12 or newer.

```powershell
git clone https://github.com/bigboybamo/SearchHarvester.git
cd SearchHarvester
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

The harvester will also need the `db` extra (`pip install -e ".[dev,db]"`), the
[Microsoft ODBC Driver 18 for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server),
and the JobSearchBuilder database created from that repo's `Scripts/CreateTables.sql`.

## Configuration

Settings come from environment variables. For local development, copy `.env.example` to `.env`
and fill in what you need; `.env` is git-ignored.

| Variable | Used by | Notes |
|---|---|---|
| `JSB_CONNECTION_STRING` | harvester | ODBC connection string to the JobSearchBuilder database |
| `SEARCH_PROVIDER` | harvester | `brave` (default) or `serpapi` |
| `BRAVE_API_KEY` / `SERPAPI_API_KEY` | harvester | Only the active provider's key is needed |
| `JSB_GITHUB_TOKEN` | health check | Fine-grained PAT, Issues read & write on JobSearchBuilder |
| `JSB_APPSETTINGS_URL` | both | Optional; a URL or local file path overriding the default GitHub raw URL |

## Health check

Probes every `AtsSourceGroups` entry and keeps one issue on JobSearchBuilder up to date. An entry
is reported only after failing on two consecutive runs. See
[CLAUDE.md](CLAUDE.md#health-check-design) for the rules.

Try it locally without touching GitHub (makes read-only requests to every entry, takes a few
minutes):

```powershell
python -m harvester.healthcheck --dry-run
```

A second dry run fed with the first run's state shows what would be reported:

```powershell
python -m harvester.healthcheck --dry-run --state-out state.json
python -m harvester.healthcheck --dry-run --state-in state.json
```

It runs weekly through [`.github/workflows/healthcheck.yml`](.github/workflows/healthcheck.yml)
(Mondays 06:17 UTC). Manual runs from the Actions tab are dry runs unless you untick the option.
The workflow needs one repository secret:

- `JSB_GITHUB_TOKEN`: a fine-grained personal access token with **Issues: read and write** on
  `bigboybamo/JobSearchBuilder` only.

## Search provider comparison

Decides whether Brave (the default) or SerpAPI should power discovery, using queries that
JobSearchBuilder actually generated. Put one query per line in `queries.txt` (git-ignored, since
it carries your profile keywords), set `BRAVE_API_KEY` and `SERPAPI_API_KEY` in `.env`, then:

```powershell
python -m harvester.search.compare queries.txt --out comparison.md
```

Every search is billed, so the script works out the number of paid searches before making any
request and refuses to go over `--max-searches` (default 20). Use `--pages N` to compare deeper
result pages and `--providers brave` to run one provider only. The report shows, per query and
overall, how many distinct ATS boards each provider found per paid search and any errors, such
as a provider rejecting a long `OR` query.

## Development checks

Run the tests and linters:

```powershell
pytest
ruff check . ; ruff format --check .
```

Parse JobSearchBuilder's live `appsettings.json` and summarise it (one read-only HTTP GET):

```powershell
python -c "from harvester.config import load_settings, load_source_groups; groups = load_source_groups(load_settings().appsettings_url); print(len(groups), 'groups,', sum(len(g.domains) for g in groups), 'domain entries')"
```

Set `JSB_APPSETTINGS_URL` to a local path (for example `tests/fixtures/appsettings.json`) to parse
a file instead.
