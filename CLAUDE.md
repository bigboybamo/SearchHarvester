# CLAUDE.md — SearchHarvester

---

## Project Overview

**Project name:** SearchHarvester
**Language:** Python 3.12+
**Project type:** Two CLI tools in one package (no UI)
**Companion to:** [bigboybamo/JobSearchBuilder](https://github.com/bigboybamo/JobSearchBuilder), a .NET Framework 4.8 WinForms app
**Origin / spec:** [JobSearchBuilder#45](https://github.com/bigboybamo/JobSearchBuilder/issues/45). Read it at the start of a fresh session.

JobSearchBuilder builds `site:`-scoped Google queries that search the career portals of Applicant Tracking Systems (Greenhouse, Lever, Workable, Ashby, ...). It stops at the query string, so finding the actual jobs is still manual. This repo closes that loop with two tools that share one ATS client layer:

1. **Harvester** (`harvest.py`): reads saved search profiles from SQL Server, discovers company boards through a search API, pulls postings from the free public ATS JSON APIs, and writes the postings back to SQL Server.
2. **Health check** (`healthcheck.py`): a weekly GitHub Action that validates every entry in JobSearchBuilder's `AtsSourceGroups` config and opens or updates **one** issue on the JobSearchBuilder repo when entries look stale or broken.

---

## The Contract with JobSearchBuilder

These two codebases share **no code and no runtime HTTP calls**. The contract is:

| Shared thing | Owner | This repo's access |
|---|---|---|
| SQL Server database `JobSearchBuilder` | JobSearchBuilder | Reads profiles, writes postings and companies |
| `JobSearchBuilder/appsettings.json` → `AtsSourceGroups` | JobSearchBuilder | **Read-only**, fetched from GitHub raw |
| Issues on `bigboybamo/JobSearchBuilder` | JobSearchBuilder | Health check creates and updates one issue |

### Rules
- **The schema is owned by JobSearchBuilder.** This repo never runs DDL (`CREATE`, `ALTER`, `DROP`). New tables or columns are added to `JobSearchBuilder/Scripts/CreateTables.sql` in that repo, with the user's sign-off, because it has no migration system.
- **`appsettings.json` is the single source of truth for ATS domains.** Never copy the domain list into this repo. Never push edits to JobSearchBuilder; report problems as issues.
- The **user's local machine** is where the database lives (`.\SQLEXPRESS`), so the harvester runs locally. The health check needs only public data, so it runs on GitHub Actions.

### Existing tables (read-only for this repo)

```sql
SearchProfiles      (Id INT PK, Name NVARCHAR(200), Seniority NVARCHAR(50) DEFAULT 'Any',
                     CreatedAt DATETIME2, UpdatedAt DATETIME2)
ProfileKeywords     (Id INT PK, ProfileId INT FK → SearchProfiles ON DELETE CASCADE,
                     Category NVARCHAR(20), Keyword NVARCHAR(200))
ProfileSourceGroups (ProfileId INT FK, SourceGroupId INT, PK (ProfileId, SourceGroupId))
```

`ProfileKeywords.Category` values, exactly as `SqlProfileStore` writes them:
`Stack` | `Role` | `Location` | `Visa` | `Remote` | `Timezone` | `Exclude`

`ProfileSourceGroups.SourceGroupId` refers to `AtsSourceGroups[].Id` in `appsettings.json`. It is not a database FK.

### Proposed new tables (NOT yet created; need sign-off and go into JobSearchBuilder's `CreateTables.sql`)

```sql
Companies   (Id INT IDENTITY PK, AtsName NVARCHAR(50), Slug NVARCHAR(200), BoardUrl NVARCHAR(500),
             FirstSeenAt DATETIME2, LastSeenAt DATETIME2, IsActive BIT,
             UNIQUE (AtsName, Slug))
JobPostings (Id INT IDENTITY PK, CompanyId INT FK → Companies, ProfileId INT FK → SearchProfiles NULL,
             AtsJobId NVARCHAR(100), Title NVARCHAR(300), Location NVARCHAR(300), Url NVARCHAR(1000),
             PostedAt DATETIME2 NULL, FirstSeenAt DATETIME2, LastSeenAt DATETIME2,
             UNIQUE (Url))
```

Until these exist, the harvester should be built and tested against SQLite, or dry-run to stdout/JSON.

### `appsettings.json` source
```
https://raw.githubusercontent.com/bigboybamo/JobSearchBuilder/master/JobSearchBuilder/appsettings.json
```
Shape: `AtsSourceGroups: [{ "Id": int, "Name": str, "Domains": [str] }]`

Known quirks the parser **must** tolerate:
- The file may start with a UTF-8 BOM, so decode with `utf-8-sig`.
- Group names for sets #1–#11 contain a corrupted `�` where an em dash should be. Never use names as identifiers; use `Id`.
- Some "domains" are host + path + query, e.g. `worldbankgroup.csod.com/ux/ats/careersite/1/home?c=worldbankgroup`. Parse each entry as a URL, defaulting to the `https://` scheme.
- The UNDP entry contains a Google Analytics `_gl=` tracking token. Report it, but don't treat it as a failure.

---

## Architecture

```
src/harvester/
  ats/                    # SHARED: one module per ATS
    base.py               #   AtsClient protocol: matches(url), parse_url(url) -> slug,
                          #   fetch_jobs(slug) -> list[Posting], probe(slug) -> ProbeResult
    greenhouse.py
    lever.py
    ashby.py
    workable.py
    registry.py           #   finds the AtsClient for a URL
  search/                 # search provider abstraction (same idea as ILlmProvider)
    base.py               #   SearchProvider protocol: search(query, page) -> list[SearchHit]
    brave.py
    serpapi.py
    factory.py            #   picks the provider from config/env
  config.py               # loads env vars + fetches/parses appsettings.json
  db.py                   # pyodbc access; parameterised SQL only
  models.py               # dataclasses / pydantic models: Profile, Posting, Company, SearchHit, ProbeResult
  query.py                # builds the search query from a Profile (see "Query building")
  harvest.py              # entry point 1
  healthcheck.py          # entry point 2
tests/
  fixtures/               # canned JSON responses per ATS and per search provider
.github/workflows/
  ci.yml                  # lint + tests on push/PR
  healthcheck.yml         # weekly cron
pyproject.toml
```

### Key design points
- **Every ATS-specific detail lives in `ats/`.** The harvester and health check never hardcode an ATS URL pattern.
- **Search providers sit behind `SearchProvider`.** Switching between Brave and SerpAPI is a config change, not a code change.
- **Search is for discovery only.** A search hit like `jobs.lever.co/acme/...` adds `acme` to `Companies`. Postings come from polling known companies through the free ATS APIs. That keeps paid search volume low.
- `db.py` is the only module that touches the database. Everything else receives and returns models.

### Public ATS endpoints (no API key)

| ATS | Board URL pattern | Jobs API |
|---|---|---|
| Greenhouse | `job-boards.greenhouse.io/{slug}` (also legacy `boards.greenhouse.io/{slug}`) | `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs` |
| Lever | `jobs.lever.co/{slug}` | `https://api.lever.co/v0/postings/{slug}?mode=json` |
| Ashby | `jobs.ashbyhq.com/{slug}` | `https://api.ashbyhq.com/posting-api/job-board/{slug}` |
| Workable | `apply.workable.com/{slug}` | `https://apply.workable.com/api/v1/widget/accounts/{slug}` |

Verify each endpoint's response shape against a live call once, then save it as a fixture in `tests/fixtures/`. Don't trust this table blindly.

---

## Harvester Flow

1. Load profiles: join `SearchProfiles`, `ProfileKeywords` and `ProfileSourceGroups`, then group keywords by `Category`.
2. Resolve each profile's `SourceGroupId`s to domains through `appsettings.json`.
3. Build the query (see below) and run it through the active `SearchProvider`.
4. For each hit, look up the `AtsClient` in `ats/registry.py`, parse the slug, and upsert into `Companies`.
5. For every active company, `fetch_jobs(slug)` and upsert into `JobPostings` (dedupe on `Url`; refresh `LastSeenAt`).
6. Match postings to profiles locally: stack/role terms appear in the title or description, and no exclude term appears.
7. *(Later)* Send a digest of new postings per profile.

### Query building
**Open decision:** port JobSearchBuilder's `QueryBuilder` to Python, or have the C# app persist the built query per profile? Until it's decided, `query.py` ports the rules below exactly:

- Block order: site → stack → role → seniority → location → visa → remote → timezone → exclude.
- Each non-empty category becomes one `( ... )` block. Terms are **OR-ed** within a block, and blocks are **AND-ed** (space-joined).
- Site block: `site:{domain}` for each distinct domain of the selected groups, OR-ed.
- Seniority: a single value. Omit it when it is `Any`. Otherwise `(Value)`, quoted if needed.
- **Always quoted:** stack, location, visa, timezone.
- **Quoted only if needed:** role, remote, seniority. "Needed" means the term contains whitespace, one of `. + - ( ) "`, or is the word `OR`.
- Quoting strips one leading and one trailing `"`, escapes inner `"` as `\"`, then wraps the term in `"`.
- Exclude: each term becomes its own `-"term"` (never `-(A OR B)`).

Add a test that reproduces a known JobSearchBuilder output byte-for-byte.

---

## Health Check Design

For each entry in `AtsSourceGroups[].Domains`:
1. **DNS + HTTP probe:** does it resolve, and what status code and final URL does it return? A redirect to a different host is a strong "domain moved" signal.
2. **Known-board probe** (ATSes with an `AtsClient`): do the known boards for that platform still return 200 with jobs?
3. **Result-volume signal** (once harvester history exists): has the platform's `site:` query returned 0 results for 30+ days?

Output: **one** open issue on JobSearchBuilder titled `ATS domain health check: N problem(s)`, with a table of failing entries and evidence.
- If an open issue with that title prefix already exists, **edit it**. Never open duplicates.
- If everything passes and an issue is open, comment and close it.
- Label it `bug`, which already exists on that repo.
- Network flakiness is not staleness. Retry before reporting, and only flag an entry that fails on two consecutive runs. Use a workflow artifact or cache to remember the previous run.

---

## Configuration

All secrets come from **environment variables**. Never store them in a file committed to the repo.

| Variable | Used by | Notes |
|---|---|---|
| `JSB_CONNECTION_STRING` | harvester | ODBC string, e.g. `Driver={ODBC Driver 18 for SQL Server};Server=.\SQLEXPRESS;Database=JobSearchBuilder;Trusted_Connection=yes;TrustServerCertificate=yes;` |
| `SEARCH_PROVIDER` | harvester | `brave` (default) or `serpapi` |
| `BRAVE_API_KEY` | harvester | Needed only when the provider is `brave` |
| `SERPAPI_API_KEY` | harvester | Needed only when the provider is `serpapi` |
| `JSB_GITHUB_TOKEN` | health check | Fine-grained PAT: `Issues: read & write` on `bigboybamo/JobSearchBuilder` only. Stored as an Actions secret. |
| `JSB_APPSETTINGS_URL` | both | Optional override of the raw GitHub URL (e.g. a local file path for testing) |

A local `.env` file is allowed for development. It must be listed in `.gitignore`, with a committed `.env.example` that has empty values.

### Search provider costs (as of Oct 2026)
- **Brave:** $5 monthly credit (about 1,000 queries), then $5 per 1,000. This is the default.
- **SerpAPI:** 250 free per month, then $25/mo for 1,000. Returns real Google results; use it if Brave handles long `OR` queries poorly.
- **Google Custom Search JSON API:** closed to new customers and shuts down on 1 Jan 2027. **Do not implement it.**
- **Never scrape google.com HTML.**

---

## Coding Standards

- Python 3.12+, with type hints everywhere. Use `dataclasses` or `pydantic` v2 for models, not dicts passed around.
- Package layout: `src/harvester`, configured through `pyproject.toml`.
- Libraries: `httpx` (HTTP), `pyodbc` (SQL Server), `pydantic` (validation), `python-dotenv` (local dev only). Ask before adding others.
- Lint and format: `ruff check` + `ruff format`. Line length 100.
- Naming: `snake_case` for functions and variables, `PascalCase` for classes, `UPPER_SNAKE` for constants.
- Logging: use the `logging` module, never `print`. The only exception is a CLI's final summary line.
- SQL: **parameterised queries only** (`?` placeholders). Never build SQL with f-strings.
- HTTP: always set a timeout and a descriptive `User-Agent`. Retry 429/5xx responses with backoff. Be polite to the ATS APIs: concurrency ≤ 5 per host.
- No `TODO` comments in committed code.

---

## Testing

**Framework:** `pytest`, plus `respx` (or `pytest-httpx`) for HTTP mocking.

- **No real network calls in tests.** Every ATS and search response comes from `tests/fixtures/*.json`.
- **No real SQL Server in tests.** `db.py` takes a connection factory; tests use SQLite or a hand-written in-memory fake. This follows JobSearchBuilder's `InMemoryProfileStore` pattern; prefer a fake over mocking cursor calls.
- Naming: `test_{function}_{scenario}_{expected}`, e.g. `test_parse_url_legacy_greenhouse_host_returns_slug`.
- Every `AtsClient` needs tests for `matches`, `parse_url` and `fetch_jobs` (fixture → models).
- `query.py` needs a byte-for-byte parity test against a known JobSearchBuilder output.
- The `appsettings.json` parser needs tests for the BOM, the corrupted names and path-style domains.

```powershell
pytest
ruff check .
```

---

## Build & Local Commands (Windows)

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

pytest
ruff check . ; ruff format .

python -m harvester.harvest --dry-run          # search + fetch, print results, write nothing
python -m harvester.harvest --profile-id 1
python -m harvester.healthcheck --dry-run      # print the issue body, don't touch GitHub
```

Prerequisites for the harvester: **Microsoft ODBC Driver 18 for SQL Server**, and the JobSearchBuilder database set up via `sqlcmd -S .\SQLEXPRESS -i JobSearchBuilder/Scripts/CreateTables.sql` from that repo.

Scheduling locally: Windows Task Scheduler runs `.venv\Scripts\python.exe -m harvester.harvest` daily.

---

## Git Workflow

- **Main branch:** `master` (protected, matching JobSearchBuilder)
- **Branch naming:** `feature/{short-description}`
- **Commit messages:** plain English, descriptive
- PRs target `master`. CI (`ci.yml`) must pass.

---

## Roadmap

### Progress Tracker
- [ ] Phase 1 — `feature/scaffold`: pyproject, ruff, pytest, CI, `config.py` with the `appsettings.json` parser and its tests
- [ ] Phase 2 — `feature/ats-clients`: `ats/` base + registry + Greenhouse and Lever, with fixtures
- [ ] Phase 3 — `feature/ats-clients-2`: Ashby and Workable
- [ ] Phase 4 — `feature/healthcheck`: DNS/HTTP and known-board probes, issue upsert, weekly workflow
- [ ] Phase 5 — `feature/search-providers`: `SearchProvider` + Brave + SerpAPI + factory, plus a side-by-side comparison script using real generated queries
- [ ] Phase 6 — `feature/query-builder`: `query.py` port with the parity test (skip it if the persisted-query decision wins)
- [ ] Phase 7 — `feature/harvest`: end-to-end harvester. **Blocked on** the `Companies` and `JobPostings` tables existing in JobSearchBuilder
- [ ] Phase 8 — `feature/digest`: daily digest of new postings per profile

Mark phases `[x]` as they merge to master. At the start of a session, Claude reads the first unchecked phase as the current one.

### Open decisions (ask the user; don't assume)
- [ ] Brave vs SerpAPI as the default, decided after the Phase 5 side-by-side comparison
- [ ] Port `QueryBuilder`, or persist the built query in JobSearchBuilder (e.g. a `BuiltQuery` column)?
- [ ] Final `Companies` / `JobPostings` schema, applied in JobSearchBuilder's `CreateTables.sql`
- [ ] Digest channel: email, Slack, or none

---

## Claude Behaviour for This Project

### Always do
- Read issue JobSearchBuilder#45 and this file's Progress Tracker at the start of a session
- Put ATS-specific logic in `ats/` and provider-specific logic in `search/`
- Add a fixture-based test for every new ATS client or provider method
- Use parameterised SQL
- Use `--dry-run` paths when demonstrating behaviour that would write to the database or GitHub

### Never do
- Run DDL against the JobSearchBuilder database, or edit that repo's files from here
- Commit API keys, tokens or connection strings with credentials
- Scrape google.com, or implement the Google Custom Search JSON API (it shuts down 1 Jan 2027)
- Make real HTTP calls in tests
- Open more than one health-check issue at a time on JobSearchBuilder
- Create GitHub issues, post comments or merge PRs on the user's behalf without explicit approval in the session. The scheduled health-check workflow is the only automated exception.

### When unsure
- Ask before changing the proposed schema, because it lives in another repo with no migrations
- Ask before adding a dependency
- Ask before supporting a new ATS beyond the four listed. Confirm that it has a public jobs endpoint first.

---

*Created: 2026-10-16 from JobSearchBuilder#45*
*Maintained by: bigboybamo*
