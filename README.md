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

Early development. **Neither CLI exists yet.** What works today is the shared foundation: settings,
the `appsettings.json` parser and the HTTP client. See the roadmap in [CLAUDE.md](CLAUDE.md#roadmap):

| Tool | Command | Arrives in |
|---|---|---|
| Health check | `python -m harvester.healthcheck` | Phase 4 |
| Harvester | `python -m harvester.harvest` | Phase 7 (needs new tables in JobSearchBuilder) |

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

## Running what exists today

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
