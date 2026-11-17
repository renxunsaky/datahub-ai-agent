# Data Navigator

A catalog-driven, read-only text-to-SQL agent for an internal Starburst (Trino)
platform. It builds a lightweight data catalog from `information_schema`,
historical SQL and the Airflow repository's `.sql` files, documents the tables
with LLM-generated cards, and turns natural-language questions into validated,
read-only SQL (or a list of candidate tables when it is not sure).

**Status:** all four phases are implemented and tested locally against the
bundled fixtures, a scripted ("replay") LLM and a simulated end-user
connection. The checks that need the real Starburst cluster and the internal
LLMaaS are listed in `docs/acceptance-real-environment.md`. Everything runs
locally; there is no deployment code.

## Quick start

Requirements: Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                   # creates .venv and installs everything
cp config.example.yaml config.yaml        # optional; the example points at the bundled fixtures
cp .env.example .env                      # secrets (git-ignored); not needed for fixtures

uv run data-navigator build --dry-run     # compute everything, write nothing
uv run data-navigator build               # build ./catalog.db: tables, patterns, cards, search index
uv run data-navigator refresh             # incremental update (new history, changed files, metadata diff)
uv run data-navigator report --top 20     # Markdown report: usage, join graph, lineage
uv run data-navigator cards --tables cos.retail_ods.account --force   # (re)generate cards
uv run data-navigator review list         # cards waiting for review; show / confirm / reject / edit
uv run data-navigator index               # rebuild the search index only
uv run data-navigator chat                # interactive agent (identity from DN_END_USER_USER)
uv run data-navigator ui                  # the same as a local Streamlit page
uv run data-navigator audit --limit 20    # recent audit entries
uv run python eval/run_eval.py --golden eval/golden_set.example.yaml --out eval/report.md
uv run data-navigator show-config         # effective configuration (never includes secrets)
```

The example configuration runs the whole flow offline: `llm.provider: replay`
answers from `eval/replay.example.yaml` and `starburst.end_user.source: fixture`
simulates `EXPLAIN`/samples against the catalog with a static access list.

`--config PATH` selects the YAML file (defaults to `./config.yaml` when present).
Relative paths in the configuration are resolved against the current directory.
Any key can be overridden with `DN_<SECTION>__<KEY>`, for example
`DN_STORAGE__URL=sqlite:////data/catalog.db` or
`DN_METADATA__FIXTURE_DIR=tests/fixtures/information_schema_v2`.

To run against the real platform, switch the sources in the YAML file
(`metadata.source: starburst`, `query_history.source: starburst`,
`sql_files.source: git`, `starburst.end_user.source: starburst`,
`llm.provider: openai_compatible` with `llm.base_url`/`llm.model`), fill in the
`TODO(confirm)` values and put the credentials in `.env` (see below). The
acceptance steps for that environment are in `docs/acceptance-real-environment.md`.

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

## Table cards, search and the agent

**Cards (phase 2).** After every `build`/`refresh` (and with `cards`), the top
`cards.top_n_tables` tables by usage get a *context pack*: structure, usage
statistics, the strongest join edges, up to `cards.max_queries_per_table`
de-identified query patterns and the statements that populate the table, all
inside tagged data blocks and capped at `cards.max_context_chars`. The pack's
SHA-256 is the card's `input_hash`; a card is regenerated only when it changes
(or with `--force`). The model must return a JSON object matching
`cards/schemas.py` (key columns must exist in the table); invalid replies are
retried up to `cards.max_generation_attempts` times. `review` lets a person
confirm, reject or edit a card; edited fields and confirmed/rejected cards are
never overwritten by regeneration.

**Search.** Tables (name, comment, card text, columns) and query patterns are
indexed in SQLite FTS5; with `llm.embedding_model` set, embeddings are stored
as BLOBs and compared with numpy cosine similarity, and both rankings are fused
with reciprocal rank fusion. Without an embedding model the search is
full-text only (logged once).

**Agent (phases 3-4).** A LangGraph state machine (`agent/graph.py`) drives an
OpenAI-style tool loop:

| Tool | What it does | Identity |
| --- | --- | --- |
| `search_tables` | hybrid search over table documents; returns name, owner, one-line purpose | public |
| `get_table_card` | columns, key-column meanings, usage, join edges — only if `check_access` allows | end user |
| `get_join_path` | shortest paths in the observed join graph; never guesses keys | public |
| `similar_queries` | de-identified patterns whose tables the user may all read | end user |
| `validate_sql` | read-only guard, then `EXPLAIN` with the user's credentials | end user |
| `check_access` | `EXPLAIN SELECT * FROM t LIMIT 0`: allowed / denied / unknown | end user |
| `run_sample` | only SQL that passed `validate_sql` (hash check); pauses with `interrupt` until the person confirms; row cap enforced in code | end user |
| `final_answer` | structured answer: mode `sql` or `candidates` | - |

Deterministic rules in `finalize`: mode `sql` is only possible when that exact
statement passed `validate_sql` in this session; after
`agent.max_sql_retries` validation failures or `agent.max_tool_steps` tool
calls the model may only call `final_answer` and the reply is forced into
candidates mode; candidates are annotated with `check_access` results and the
source of their description (`card (ai_inferred)` is shown as "AI inferred").
Sample rows are returned to the UI through the graph state only; the model
sees row counts and column names unless `samples.send_to_llm` is true.

`chat` (CLI, `[y/N]` confirmation) and `ui` (Streamlit, confirmation buttons)
share the same `AgentSession`; every turn and every sample decision is written
to the audit log (`audit_logs` table plus `audit.path` JSONL) with the SQL
de-identified.

**Evaluation.** `eval/run_eval.py` runs `eval/golden_set.example.yaml`
(question, user, `expected_sql` or `expected_mode: candidates`,
`expected_tables`) and reports mode accuracy, candidate recall@5, EXPLAIN
first-pass rate and SQL execution correctness (the last one is only meaningful
against the real cluster; the fixture connection returns synthetic rows).

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
| `starburst.end_user` | `source` | `fixture` | `starburst` / `fixture` (simulated EXPLAIN and samples) |
| | `fixture_access` | `{}` | fixture only: user -> glob patterns of readable tables |
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
| `llm` | `provider` | `none` | `openai_compatible` / `replay` / `none` |
| | `base_url` | **none** | required for `openai_compatible`; public endpoints are rejected |
| | `model`, `embedding_model` | `null` | TODO(confirm); no embedding model = text-only search |
| | `timeout_s`, `max_retries`, `max_concurrency`, `requests_per_minute` | `60`, `3`, `4`, `60` | TODO(confirm) |
| | `temperature`, `json_mode` | `0.0`, `true` | `json_mode` sends `response_format: json_object`, TODO(confirm) |
| | `replay_path` | `eval/replay.example.yaml` | replay provider script |
| `agent` | `max_tool_steps`, `max_sql_retries`, `search_limit` | `10`, `3`, `8` | per-turn limits |
| | `similar_queries_limit`, `history_window` | `5`, `40` | |
| `search` | `rrf_k`, `candidate_multiplier` | `60`, `3` | fusion parameters |
| `cards` | `top_n_tables`, `max_queries_per_table`, `max_context_chars`, `max_generation_attempts` | `200`, `5`, `12000`, `3` | |
| `audit` | `path` | `audit.jsonl` | JSONL copy of the audit trail; `null` keeps the database only |

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
| Identity injected by the harness | `agent/factory.py` binds the end-user client to `DN_END_USER_USER` (or `chat --user` for local runs) when the session is created; no tool takes a user argument (`tests/unit/test_agent_tools.py`) |
| Confirmation before samples | `agent/graph.py` sample node: hash check against the validated-SQL registry, then LangGraph `interrupt`; nothing runs on decline or on a new question |
| Sample data never persisted or logged | rows live in the graph state / UI only; the audit trail stores counts; `samples.send_to_llm` defaults to `false` |
| Visibility tiers | `agent/tools.py`: names, owners and one-line purposes for everyone; columns, card details and similar patterns only when `check_access` is `allowed` |
| Prompt-injection defence | catalog content reaches the model only inside `<tool_result>` / `<table>`… data blocks; the system prompts say those are data, not instructions |
| Deterministic mode decision | `finalize`: mode `sql` requires a validated hash; forced candidates after the retry/step limits; access labels from `check_access` |
| Internal LLM only | `llm.base_url` has no default and rejects well-known public endpoints; the client logs tokens and latency only |
| Audit | `audit/log.py`: user, question, de-identified SQL, validation status, sample confirmed/executed/row count |

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
3. implement `search/postgres_index.py` (`PostgresTextIndex` with
   `tsvector`, `PostgresVectorIndex` with pgvector) and wire them in
   `search/factory.py`; the SQLite FTS5 and vector tables are created outside
   Alembic because they are SQLite-specific;
4. set `storage.backend: postgres` and `storage.url: postgresql+psycopg://...`.

## Project layout

```
src/data_navigator/
  config.py              settings (YAML + env) and secrets (env only)
  cli.py                 build / refresh / cards / index / review / chat / ui / audit / report / show-config
  starburst/             builder client (whitelist), end-user client (EXPLAIN, samples), fixture connection, errors
  sources/               metadata, query history, .sql files, team mapping (protocols + implementations)
  sqlproc/               template, statements, normalize, fingerprint, parse, readonly_guard
  analysis/              usage, join_graph, lineage, ownership
  catalog/               ORM models, records, repository interface, SQLite/SQLAlchemy repo, migrations
  pipeline/              build/refresh orchestration, enrichment (cards + index), Markdown report
  llm/                   OpenAI-compatible client, replay client, factory
  cards/                 context packs, output schema, prompts, generator
  search/                documents, SQLite FTS5 / vector indexes, hybrid RRF search, PostgreSQL stubs
  agent/                 state, tools, prompts, LangGraph graph, session, factory
  audit/                 audit logger
  ui/                    Streamlit page
  evaluation.py          golden-set runner (eval/run_eval.py is the entry point)
tests/
  unit/                  one module per package; architecture and persistence-safety tests
  fixtures/              fake bank platform: information_schema JSON (v1, v2), query history JSONL, Airflow-style repo
eval/                    golden_set.example.yaml, replay.example.yaml, run_eval.py
docs/open-questions.md   everything marked TODO(confirm)
docs/acceptance-real-environment.md   what can only be verified on the real platform
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
