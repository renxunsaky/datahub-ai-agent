# Open questions (TODO(confirm))

Everything below is a configuration item with a placeholder default; nothing
in the code assumes the real values. Grep for `TODO(confirm)` to find the
exact spots.

## Starburst

1. **Connection** – host, port, HTTP scheme, and the authentication method for
   the builder user and for end users (`starburst.connection.*`). Basic auth
   and JWT are wired; Kerberos/OAuth would need a small addition in
   `starburst/connection.py`.
2. **Dedicated builder user** – its name (`starburst.builder.user`), which
   catalogs it can see (`metadata.catalogs`), and whether it is restricted to
   metadata plus the history table (the client enforces this locally anyway).
3. **Query history** – the fully qualified table name (`query_history.table`),
   its column names (`query_history.columns.*`), the timezone of its timestamps
   (UTC assumed), its retention (`query_history.retention_days`), whether rows
   appear only once a query has finished (assumed; the time window uses the
   end timestamp), and the expected volume (a 90-day window is fetched in one
   statement for now).
4. **Sample limits** – sensible values for `samples.query_max_execution_time`
   and `samples.session_properties` (e.g. `query_max_scan_physical_bytes`).

## GitLab / Airflow repository

5. **Repository** – clone URL (`sql_files.repo_url`), branch, and a token with
   `read_repository` scope (`DN_GITLAB_TOKEN`). The token is passed through a
   git credential helper and never written to disk.
6. **Template syntax** – which forms appear in `.sql` files besides Jinja
   (`template.extra_patterns` currently handles `${var}` and `%(name)s`).
7. **Default catalog/schema** for two-part or bare table names in files
   (`sql_files.default_catalog/default_schema`); unique suffix matching against
   the catalog is used when they are not set.

## LLMaaS (needed from phase 2)

8. **Endpoint** – `llm.base_url` (no default by design), whether it is
   OpenAI-compatible, available model names, whether an embedding model exists
   (`llm.embedding_model`; without it search falls back to full-text only), and
   rate limits (`llm.requests_per_minute`, `llm.max_concurrency`).
9. **Data classification** – which data may be sent to LLMaaS. This decides
   whether `samples.send_to_llm` may ever be switched on, and whether table
   comments / column names of all catalogs may be used in prompts.

## Organisation

10. **Owning teams** – whether teams follow schema names, COS bucket names or
    a registry (`analysis.ownership_rules`); bucket-based rules need the
    bucket to be discoverable (e.g. via `SHOW CREATE TABLE`).
11. **User → team mapping** – the authoritative source (`teams.*`); a static
    mapping in the configuration is used for now.
12. **CI platform** – GitLab CI is assumed but not configured; `make check`
    runs the full gate locally.

## Noted deviations from the task description

- Added `sql_files` and `parse_failures` tables (incremental file processing
  and failure accounting need them).
- `cli.py` only wires commands; the orchestration lives in `pipeline/` so it is
  testable without Typer.
- The SQLAlchemy implementation is shared (`catalog/sqlalchemy_repo.py`);
  `sqlite_repo.py` and `postgres_repo.py` are thin subclasses.
- `build` replaces the derived data (patterns, files, join graph, lineage) so
  that repeated builds stay idempotent; confirmed owners and reviewed card
  fields are preserved.
- Phase 0 did not implement the `TextIndex` / `VectorIndex` SQLite classes,
  only their protocols; they belong to phase 2 together with the cards.
