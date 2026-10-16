# SearchHarvester

Companion to [JobSearchBuilder](https://github.com/bigboybamo/JobSearchBuilder). Two CLI tools
sharing one ATS client layer:

- **Harvester**: discovers company boards via a search API, pulls postings from public ATS APIs,
  and writes them to the JobSearchBuilder SQL Server database.
- **Health check**: weekly GitHub Action that validates JobSearchBuilder's `AtsSourceGroups` and
  reports stale entries as a single issue.

Spec: [JobSearchBuilder#45](https://github.com/bigboybamo/JobSearchBuilder/issues/45).
Design and conventions: [CLAUDE.md](CLAUDE.md).

## Development (Windows)

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"        # add ",db" for the harvester's SQL Server access

pytest
ruff check . ; ruff format .
```

Configuration comes from environment variables; see [.env.example](.env.example).
