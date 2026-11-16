# Data Navigator

A catalog-driven, read-only text-to-SQL agent for an internal Starburst (Trino)
platform. It builds a lightweight data catalog from `information_schema`,
historical SQL and the Airflow repository's `.sql` files, and (from phase 3)
turns natural-language questions into validated, read-only SQL.

**Status:** phases 0 and 1 are implemented (catalog build and refresh).
Table cards, search, the agent and the UI follow in phases 2-4.
Everything runs locally; there is no deployment code.

## Quick start

Requirements: Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                   # creates .venv and installs everything
cp config.example.yaml config.yaml        # optional; the example points at the bundled fixtures
cp .env.example .env                      # secrets (git-ignored); not needed for fixtures

uv run data-navigator build --dry-run     # compute everything, write nothing
uv run data-navigator build               # build ./catalog.db from the fixtures
uv run data-navigator refresh             # incremental update (new history, changed files, metadata diff)
uv run data-navigator report --top 20     # Markdown report: usage, join graph, lineage
uv run data-navigator show-config         # effective configuration (never includes secrets)
```

`--config PATH` selects the YAML file (defaults to `./config.yaml` when present).
Relative paths in the configuration are resolved against the current directory.
Any key can be overridden with `DN_<SECTION>__<KEY>`, for example
`DN_STORAGE__URL=sqlite:////data/catalog.db` or
`DN_METADATA__FIXTURE_DIR=tests/fixtures/information_schema_v2`.

To run against the real platform, switch the three sources in the YAML file
(`metadata.source: starburst`, `query_history.source: starburst`,
`sql_files.source: git`), fill in the `TODO(confirm)` values and put the
credentials in `.env` (see below).

### Development

```bash
make check        # ruff, mypy (strict), import-linter, pytest with the 90 % coverage gate
make test         # pytest only
make format       # ruff --fix + ruff format
uv run alembic revision --autogenerate -m "describe change"   # new migration
```

## How the catalog is built

```
information_schema ─┐
query history ──────┼─► template/split ─► normalise (constants → ?) ─► fingerprint ─► parse (qualify)
.sql files (git) ───┘                                                                     │
                                                                                          ▼
                 usage statistics · join graph · lineage · owning team  ─►  SQLite catalog
```

1. **Metadata** – tables and columns from `information_schema.tables/columns`,
   table comments from `system.metadata.table_comments`, column comments from
   `information_schema` or `SHOW COLUMNS` as a fallback. A structure hash (ordered
   column names and types) detects changed tables.
2. **Query history** – only successful queries, read in a time window
   (`query_history.window_days` for `build`, since the last run for `refresh`).
3. **`.sql` files** – cloned from GitLab (token via a git credential helper, never
   in the URL or `.git/config`); only configured directories and extensions.
   Jinja and other placeholders are replaced by a fixed identifier before parsing.
4. **Normalise** – every constant (strings, numbers, dates, timestamps, intervals,
   hex/byte strings, JSON paths, `IN` lists, `VALUES` rows) becomes `?`; comments
   are dropped; identifiers are lower-cased. The SHA-256 of the result is the
   pattern fingerprint, so a daily job collapses into one pattern with a count.
   Statements that cannot be parsed are recorded as a hash and error type only.
5. **Parse** – sqlglot's optimizer qualifies every column against the catalog
   schema, yielding tables read/written, columns per table, join edges
   (`ON`/`USING`/`WHERE`/`MERGE ON`, with LEFT/RIGHT orientation preserved),
   filter columns, `GROUP BY` columns and aggregate expressions. Partial table
   names resolve through the session catalog/schema or a unique suffix match.
6. **Analyse** – usage per table and column (executions, distinct users and
   teams, last use, join-key and filter flags), a weighted join graph
   (committed job > repeated query > one-off query), lineage from
   `INSERT`/CTAS/`MERGE` in files, and owning teams inferred from naming rules.
7. **Store** – one transaction; `build` replaces the derived data, `refresh`
   merges. `--dry-run` never opens a database for writing. Reviewed fields
   (owner `confirmed`, card fields) are never overwritten.

`build` and `refresh` are idempotent: re-running with unchanged inputs changes
nothing, and counts are never doubled.

## Configuration reference

All keys live in `config.example.yaml`; `TODO(confirm)` marks values that still
need confirmation from the platform team (see `docs/open-questions.md`).

| Section | Key | Default | Notes |
| --- | --- | --- | --- |
| `storage` | `backend` | `sqlite` | `postgres` is a stub |
| | `url` | `sqlite:///./catalog.db` | SQLAlchemy URL |
| `starburst.connection` | `host`, `port`, `http_scheme` | example values | TODO(confirm) |
| | `auth` | `basic` | `basic` / `jwt` / `none`, TODO(confirm) |
| | `verify_ssl`, `request_timeout_s`, `source_tag` | `true`, `60`, `data-navigator` | |
| `starburst.builder` | `user` | `data_navigator_builder` | password in `DN_BUILDER_PASSWORD` |
| `samples` | `max_rows` | `10` | hard cap, not changeable by the LLM |
| | `send_to_llm` | `false` | keep `false` until security approves |
| | `query_max_execution_time` | `30s` | TODO(confirm) |
| | `session_properties` | `query_max_scan_physical_bytes: 1GB` | TODO(confirm) |
| `metadata` | `source` | `fixture` | `starburst` / `fixture` |
| | `catalogs` | `[cos]` | TODO(confirm) |
| | `excluded_schemas` | `[information_schema, sys, pg_catalog]` | |
| | `column_comments` | `auto` | `auto` / `information_schema` / `show_columns` |
| | `fixture_dir` | `tests/fixtures/information_schema` | |
| `query_history` | `source` | `jsonl` | `starburst` / `jsonl` |
| | `table` | `null` | TODO(confirm), required for `starburst` |
| | `columns.*` | `query_id, query, user, state, created, end, catalog, schema` | TODO(confirm) |
| | `success_states` | `[FINISHED]` | |
| | `window_days` | `90` | initial window for `build` |
| | `retention_days` | `null` | TODO(confirm) |
| | `jsonl_path` | `tests/fixtures/query_history/queries.jsonl` | |
| | `ignored_prefixes` | `EXPLAIN, SHOW, DESCRIBE, DESC, SET, USE, RESET` | skipped before parsing |
| `sql_files` | `source` | `local` | `git` / `local` |
| | `repo_url` | `null` | TODO(confirm); must not contain credentials |
| | `branch`, `clone_dir`, `git_username` | `main`, `.cache/airflow-repo`, `oauth2` | |
| | `include_dirs`, `extensions` | `[src, automation]`, `[.sql]` | |
| | `local_path` | `tests/fixtures/repo` | |
| | `default_catalog`, `default_schema` | `null` | TODO(confirm); used for partial names |
| `template` | `jinja` | `true` | TODO(confirm) |
| | `extra_patterns` | `${...}`, `%(name)s` | regular expressions |
| | `placeholder` | `dn_tpl_var` | identifier substituted for template expressions |
| `analysis` | `weight_repo_file / weight_repeated_query / weight_single_query` | `3 / 2 / 1` | join-edge scoring |
| | `repeated_query_threshold` | `2` | executions that make a pattern "repeated" |
| | `join_key_min_count`, `filter_min_count` | `2`, `2` | column flags |
| | `ownership_rules` | schema prefix rule | TODO(confirm) |
| `teams` | `source`, `mapping`, `default_team` | `config`, `{}`, `null` | TODO(confirm) |
| `llm` | `base_url` | **none** | required before phase 2; internal LLMaaS only |
| | `model`, `embedding_model` | `null` | TODO(confirm) |
| | `timeout_s`, `max_retries`, `max_concurrency`, `requests_per_minute` | `60`, `3`, `4`, `60` | TODO(confirm) |
| `agent` | `max_tool_steps`, `max_sql_retries`, `search_limit` | `10`, `3`, `8` | phase 3 |
| `cards` | `top_n_tables`, `max_queries_per_table`, `max_context_chars`, `max_generation_attempts` | `200`, `5`, `12000`, `3` | phase 2 |
| `audit` | `path` | `audit.jsonl` | phase 4 |

### Secrets (`.env` or environment only)

| Variable | Used by |
| --- | --- |
| `DN_BUILDER_PASSWORD` | builder connection (catalog build only) |
| `DN_END_USER_USER`, `DN_END_USER_PASSWORD`, `DN_END_USER_JWT` | end-user connection (EXPLAIN and samples) |
| `DN_GITLAB_TOKEN` | cloning the Airflow repository |
| `DN_LLM_API_KEY` | LLMaaS |

A YAML file that contains a `password`, `token` or `api_key` key is rejected
(`extra="forbid"`), so secrets cannot end up in configuration by accident.

## Security design (summary)

| Constraint | Where it is enforced |
| --- | --- |
| Two separate Starburst identities | `starburst/builder_client.py` (dedicated user) and `starburst/end_user_client.py` (the person); separate config and factories; import-linter forbids `agent/` and `ui/` from importing the builder client, the sources or the pipeline (`pyproject.toml`, `tests/unit/test_architecture.py`) |
| Builder statement whitelist | `starburst/whitelist.py`: AST-based; only `SHOW`, `DESCRIBE`, and `SELECT`s over `information_schema.*`, `system.metadata.*` and the configured history table; everything else raises locally |
| Generated SQL is read-only | `sqlproc/readonly_guard.py`: exactly one `SELECT` (CTEs, set operations, subqueries, `UNNEST`, windows allowed); DDL/DML/`CALL`/`SET`/`USE`/`EXECUTE`/multi-statement/`SELECT INTO`/table-valued functions rejected; no LLM involved |
| Sample limits | `EndUserClient.sample` applies `min(requested, samples.max_rows)` with sqlglot, re-checks the statement before sending, truncates results, and sets `query_max_execution_time` plus configured session properties |
| Deterministic access decisions | `starburst/errors.py`: only `PERMISSION_DENIED` → `denied`, only success → `allowed`, everything else → `unknown` (property-tested) |
| SQL de-identification | `sqlproc/normalize.py` replaces every constant before storage; unparseable SQL keeps a hash and error type only; `tests/unit/test_architecture.py` scans the whole SQLite file for raw SQL and fixture constants |
| Secrets | environment / `.env` only (`config.Secrets`, `SecretStr`); git token passed through an inline credential helper, URLs with user info rejected, error messages scrubbed |
| Logging | clients log statement digests, statuses and row counts only; sample rows never reach the logs |
| Identity injected by the harness | `build_end_user_client(settings, secrets, user=...)`: the identity comes from `DN_END_USER_USER`, never from tool arguments |
| Internal LLM only | `llm.base_url` has no default |

Phase 3/4 items (interrupt before samples, `samples.send_to_llm`, prompt
data blocks, audit log) have their configuration and storage model in place
but no runtime yet.

## Switching the storage to PostgreSQL

The ORM models (`catalog/models.py`) and the Alembic migrations
(`catalog/migrations/`) use portable constructs only: `JSON` columns, a
`UTCDateTime` decorator that stores naive UTC on SQLite and aware values on
PostgreSQL, and batch-mode migrations. The SQLAlchemy implementation
(`catalog/sqlalchemy_repo.py`) is dialect-neutral; `SqliteCatalogRepository`
only adds the engine (foreign keys on) and `PostgresCatalogRepository` is a
stub that currently raises.

To finish the PostgreSQL backend:

1. implement `PostgresCatalogRepository.__init__` with `create_engine(url)` and
   pool settings, and remove the `NotImplementedError`;
2. run `uv run alembic upgrade head` against PostgreSQL and fix anything the
   dialect rejects (none expected); consider `JSONB` for the JSON columns;
3. implement `TextIndex` with PostgreSQL full-text search and `VectorIndex`
   with pgvector (phase 2 interfaces in `search/`);
4. set `storage.backend: postgres` and `storage.url: postgresql+psycopg://...`.

## Project layout

```
src/data_navigator/
  config.py              settings (YAML + env) and secrets (env only)
  cli.py                 build / refresh / report / show-config
  starburst/             builder client (whitelist), end-user client (EXPLAIN, samples), error mapping
  sources/               metadata, query history, .sql files, team mapping (protocols + implementations)
  sqlproc/               template, statements, normalize, fingerprint, parse, readonly_guard
  analysis/              usage, join_graph, lineage, ownership
  catalog/               ORM models, records, repository interface, SQLite/SQLAlchemy repo, migrations
  pipeline/              build/refresh orchestration and the Markdown report
  search/                TextIndex / VectorIndex protocols (implementations in phase 2)
  agent/, ui/            placeholders with import rules (phases 3-4)
tests/
  unit/                  one module per package; architecture and persistence-safety tests
  fixtures/              fake bank platform: information_schema JSON (v1, v2), query history JSONL, Airflow-style repo
docs/open-questions.md   everything marked TODO(confirm)
```

## Design decisions worth knowing

- **Derived data is recomputed from stored patterns.** Usage, the join graph
  and lineage are recalculated from `query_templates` + `sql_files` on every
  run, which is what makes `build` and `refresh` idempotent and consistent.
- **`build` replaces, `refresh` merges.** A full build clears patterns and
  files first (tables, owners and reviewed fields are kept); refresh adds the
  history since the last run and re-processes only files whose content changed.
- **Join edges are oriented and canonical.** Endpoints are ordered so the same
  key pair always maps to one row; `LEFT`/`RIGHT` are flipped when the order
  flips, so the outer side keeps its meaning.
- **Scores count patterns, occurrences count executions.** A job that runs
  daily adds its weight once but contributes all its executions to the
  occurrence count.
- **Timestamp precision inferred from literals is dropped** (`TIMESTAMP
  '… .123'` and `'…'` yield the same pattern); explicit type parameters such as
  `DECIMAL(18, 2)` are structure and are kept.
- **Additional tables** `sql_files` and `parse_failures` were added to the data
  model to support incremental file processing and failure accounting.
