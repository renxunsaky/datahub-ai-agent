# Acceptance on the real environment

Everything in this repository was verified locally with the bundled fixtures,
the scripted replay LLM (`llm.provider: replay`) and the simulated end-user
connection (`starburst.end_user.source: fixture`). The items below cannot be
verified without the real Starburst cluster and the internal LLMaaS; run them
from a machine that has both, in this order. Each item names the command and
the expected outcome. `make check` (ruff, mypy, import-linter, pytest with the
coverage gate) is already green locally and needs no real environment.

## 0. Setup

1. `uv sync` (also regenerates `uv.lock`: the new dependencies — httpx, numpy,
   langgraph, streamlit — were added to `pyproject.toml` on a machine without
   `uv`, so the lock file is stale).
2. `cp config.example.yaml config.yaml`, then set:
   - `metadata.source: starburst`, `query_history.source: starburst` with the
     real `query_history.table` and column names, `sql_files.source: git` with
     `repo_url`;
   - `starburst.connection.*`, `starburst.builder.user`;
   - `starburst.end_user.source: starburst`;
   - `llm.provider: openai_compatible`, `llm.base_url`, `llm.model`,
     `llm.embedding_model` (leave `null` if LLMaaS has no embeddings).
3. `cp .env.example .env` and fill `DN_BUILDER_PASSWORD`, `DN_END_USER_USER`,
   `DN_END_USER_PASSWORD` (or `DN_END_USER_JWT`), `DN_GITLAB_TOKEN`,
   `DN_LLM_API_KEY`.
4. `uv run data-navigator show-config` — no secret appears in the output.

## 1. Catalog (phases 0-1, real sources)

| # | Step | Expected |
| --- | --- | --- |
| 1.1 | `uv run data-navigator build --dry-run --skip-cards` | Metadata, history and files are read; summary shows table/query/file counts; nothing written. Builder statements all pass the whitelist (no `BuilderPolicyError`). |
| 1.2 | `uv run data-navigator build --skip-cards` | `catalog.db` created; parse-failure count is acceptable; `report` shows usage, join graph and lineage that look right to a domain expert. |
| 1.3 | `uv run data-navigator refresh --skip-cards` right after | `0 added, 0 changed`; counts not doubled (idempotence). |
| 1.4 | Add a table on the platform, then `refresh` | The new table is listed under `added`. |

Open questions that this step answers: `docs/open-questions.md` items 1-7, 10, 11.

## 2. Table cards and search (phase 2, real LLMaaS)

| # | Step | Expected |
| --- | --- | --- |
| 2.1 | `uv run data-navigator cards --tables <one table>` | One card generated; `review show <table>` prints a sensible purpose and key columns that exist. If LLMaaS rejects `response_format`, set `llm.json_mode: false` and retry. |
| 2.2 | `uv run data-navigator cards` | Top-N cards generated within the rate limit; failures (if any) listed with the error type; logs show token counts only, never prompt text. |
| 2.3 | `review edit <table> --purpose "..." --by <you>` then `cards --force --tables <table>` | The edited purpose survives; the other fields are regenerated. |
| 2.4 | `uv run data-navigator index` | With an embedding model: `text + vector`, all documents embedded. Without: `text only` and a single warning in the log. |
| 2.5 | Ask a colleague's wording in `chat` | `search_tables` returns the expected tables in the top 5 (compare with the fixture behaviour). |

Open questions answered: items 8 and 9 (OpenAI compatibility, embeddings, rate limits, data classification).

## 3. Agent (phase 3, real cluster + LLMaaS)

| # | Step | Expected |
| --- | --- | --- |
| 3.1 | `chat`: a question whose tables you may read | Mode `sql`; the shown statement passed `EXPLAIN` with *your* identity (Starburst query log shows source `data-navigator-end-user` and your user, never the builder). |
| 3.2 | `chat`: a question about a table you may **not** read | Candidates mode with `access: denied` for that table; column details are not shown. |
| 3.3 | `chat`: an ambiguous question | Candidates mode with a clarifying question. |
| 3.4 | Stop the cluster / break the password, ask again | `validate_sql` returns `unknown`/`timeout`; the reply never claims the SQL is executable; `check_access` is `unknown`, never `allowed`. |
| 3.5 | `uv run python eval/run_eval.py --golden <real golden set>` | Report with mode accuracy, recall@5, EXPLAIN first-pass rate and **SQL execution correctness** (this last metric is only meaningful here). |

## 4. Samples, UI and audit (phase 4)

| # | Step | Expected |
| --- | --- | --- |
| 4.1 | `chat`: ask for a sample, answer `n` | Nothing runs (no new query in the Starburst log); audit shows `sample confirmed=False executed=False`. |
| 4.2 | Ask again, answer `y` | At most `samples.max_rows` rows; the executed statement carries `LIMIT <= max_rows` and the session properties `query_max_execution_time` / `query_max_scan_physical_bytes` (visible in the query log). |
| 4.3 | Ask for a sample of SQL that was never validated | Refused without a confirmation prompt. |
| 4.4 | `uv run data-navigator ui` | Streamlit page: SQL block, "允许读取样例" / "拒绝" buttons, sample table; identity shown comes from `.env`. |
| 4.5 | `uv run data-navigator audit` and `grep` the JSONL file | Entries contain de-identified SQL (`?` placeholders), counts, no sample values; `catalog.db` contains no sample rows (`tests/unit/test_architecture.py` logic applied to the live DB). |
| 4.6 | Security sign-off | Decide whether `samples.send_to_llm` may ever be `true` (open question 9). Until then it stays `false`. |

## 5. Optional: Trino container integration test

Not implemented (listed as optional in the task). If wanted: a `docker compose`
file with Trino, file-based access control for two users, and a pytest marker
that runs `validate_sql`, `check_access` and `run_sample` end to end.
