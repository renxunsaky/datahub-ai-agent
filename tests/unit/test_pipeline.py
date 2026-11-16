from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from data_navigator.catalog.sqlite_repo import SqliteCatalogRepository
from data_navigator.config import Secrets, Settings
from data_navigator.pipeline.build import CatalogBuilder
from data_navigator.pipeline.report import render_report
from data_navigator.sources.factory import Sources, build_sources

FIXTURES = Path("tests/fixtures")
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
TEAMS = {
    "alice": "retail",
    "bob": "retail",
    "carol": "risk",
    "dave": "cards",
    "eve": "data_platform",
}


def make_settings(**overrides: object) -> Settings:
    settings = Settings()
    settings.teams.mapping = dict(TEAMS)
    for key, value in overrides.items():
        section, _, field = key.partition("__")
        setattr(getattr(settings, section), field, value)
    return settings


def make_sources(settings: Settings) -> Sources:
    return build_sources(settings, Secrets(_env_file=None))  # type: ignore[call-arg]


@pytest.fixture
def repo(tmp_path: Path) -> SqliteCatalogRepository:
    repository = SqliteCatalogRepository(f"sqlite:///{tmp_path}/catalog.db")
    repository.initialize()
    return repository


def builder(
    settings: Settings, repo: SqliteCatalogRepository, now: datetime = NOW
) -> CatalogBuilder:
    return CatalogBuilder(settings, repo, make_sources(settings), now=lambda: now)


def test_build_from_fixtures(repo: SqliteCatalogRepository) -> None:
    summary = builder(make_settings(), repo).build()
    assert not summary.dry_run and summary.run_id is not None
    assert summary.tables_seen == 12 and len(summary.tables_added) == 12
    assert (summary.queries_processed, summary.queries_skipped, summary.query_parse_failures) == (
        22,
        2,
        1,
    )
    assert (summary.files_processed, summary.statements_processed, summary.file_parse_failures) == (
        7,
        8,
        1,
    )
    assert summary.templates_new == 23 == summary.templates_total
    assert summary.join_edges == 11 and summary.lineage_edges == 12
    assert summary.owners_inferred == 10
    assert "dry run" not in summary.render()

    tables = {t.full_name: t for t in repo.list_tables()}
    account = tables["cos.retail_ods.account"]
    assert (account.owner_team, account.owner_source) == ("retail", "inferred")
    assert tables["cos.ref.branch"].owner_team is None
    assert (account.use_count, account.user_count, account.team_count) == (12, 3, 3)
    assert account.last_queried_at == datetime(2026, 10, 12, 8, 15, tzinfo=UTC)
    columns = {c.name: c for c in account.columns}
    assert columns["customer_id"].is_join_key and columns["status"].is_filter
    assert not columns["balance"].is_join_key

    templates = {t.fingerprint: t for t in repo.list_templates()}
    daily = max(templates.values(), key=lambda t: t.execution_count)
    assert daily.execution_count == 6 and daily.users == ["alice"] and daily.teams == ["retail"]
    assert daily.tables_read == ["cos.retail_ods.account", "cos.retail_ods.customer"]
    assert "?" in daily.normalized_sql and "2026" not in daily.normalized_sql
    file_patterns = [t for t in templates.values() if t.file_paths]
    assert len(file_patterns) == 8

    lineage = repo.list_lineage_edges()
    downstreams = {e.downstream_table for e in lineage}
    assert downstreams == {
        "cos.cards_dm.card_spend_monthly",
        "cos.retail_dm.customer_daily_balance",
        "cos.retail_dm.customer_monthly_summary",
        "cos.risk_dm.customer_risk_score",
        "cos.risk_dm.daily_exposure",
    }
    top_edge = repo.list_join_edges()[0]
    assert (top_edge.left_table, top_edge.right_table) == (
        "cos.retail_ods.account",
        "cos.retail_ods.customer",
    )
    assert top_edge.sources == {"repeated_query": 1, "repo_file": 2}
    assert repo.count_parse_failures() == 2
    run = repo.last_successful_run()
    assert (
        run is not None
        and run.history_processed_until == NOW
        and run.summary["templates_new"] == 23
    )


def test_build_is_idempotent(repo: SqliteCatalogRepository) -> None:
    first = builder(make_settings(), repo).build()
    second = builder(make_settings(), repo, now=NOW + timedelta(hours=1)).build()
    assert second.tables_unchanged == 12 and not second.tables_added
    assert second.templates_total == first.templates_total == 23
    daily = max(repo.list_templates(), key=lambda t: t.execution_count)
    assert daily.execution_count == 6  # not doubled
    assert len(repo.list_join_edges()) == 11
    assert [r.status for r in repo.list_build_runs()] == ["succeeded", "succeeded"]


def test_dry_run_writes_nothing(repo: SqliteCatalogRepository) -> None:
    summary = builder(make_settings(), repo).build(dry_run=True)
    assert summary.dry_run and summary.run_id is None
    assert summary.templates_new == 23 and len(summary.tables_added) == 12
    assert "dry run" in summary.render()
    assert repo.list_tables() == [] and repo.list_templates() == [] and repo.list_build_runs() == []


def test_refresh_detects_new_changed_and_removed_tables(repo: SqliteCatalogRepository) -> None:
    builder(make_settings(), repo).build()
    settings = make_settings(metadata__fixture_dir=FIXTURES / "information_schema_v2")
    summary = builder(settings, repo, now=NOW + timedelta(days=1)).refresh()
    assert summary.tables_added == ["cos.cards_dm.card_spend_monthly"]
    assert summary.tables_changed == ["cos.retail_ods.customer"]
    assert summary.tables_removed == ["cos.retail_dm.customer_monthly_summary"]
    assert (
        summary.queries_processed == 0
        and summary.files_processed == 0
        and summary.files_unchanged == 7
    )
    customer = repo.get_table("cos.retail_ods.customer")
    assert customer is not None and "email" in [c.name for c in customer.columns]
    assert repo.get_table("cos.retail_dm.customer_monthly_summary") is not None
    assert "cos.retail_dm.customer_monthly_summary" not in {t.full_name for t in repo.list_tables()}
    assert summary.templates_total == 23  # patterns survive a metadata refresh

    # A second refresh with the same inputs changes nothing.
    again = builder(settings, repo, now=NOW + timedelta(days=2)).refresh()
    assert not again.tables_added and not again.tables_changed and not again.tables_removed


def test_refresh_with_table_filter(repo: SqliteCatalogRepository) -> None:
    builder(make_settings(), repo).build()
    settings = make_settings(metadata__fixture_dir=FIXTURES / "information_schema_v2")
    summary = builder(settings, repo, now=NOW + timedelta(days=1)).refresh(
        tables=["COS.retail_ods.customer", "cos.ref.branch"]
    )
    assert summary.tables_seen == 2
    assert summary.tables_changed == ["cos.retail_ods.customer"]
    assert summary.tables_added == [] and summary.tables_removed == []


def test_refresh_processes_only_new_history_and_changed_files(
    repo: SqliteCatalogRepository, tmp_path: Path
) -> None:
    history = tmp_path / "queries.jsonl"
    shutil.copy(FIXTURES / "query_history/queries.jsonl", history)
    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURES / "repo", repo_dir)
    settings = make_settings(query_history__jsonl_path=history, sql_files__local_path=repo_dir)
    first = builder(settings, repo).build()
    assert first.templates_total == 23

    # New history after the first run, one changed file, one removed file.
    later = NOW + timedelta(days=5)
    new_record = {
        "query_id": "q_new",
        "query": "SELECT c.customer_id, c.segment_code, SUM(a.balance) AS total_balance FROM cos.retail_ods.account a JOIN cos.retail_ods.customer c ON a.customer_id = c.customer_id WHERE a.dt = DATE '2026-11-19' AND a.status = 'OPEN' GROUP BY c.customer_id, c.segment_code",
        "user": "frank",
        "state": "FINISHED",
        "created": "2026-11-19T08:15:00Z",
        "end": "2026-11-19T08:15:30Z",
        "catalog": "cos",
        "schema": "retail_ods",
    }
    with history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(new_record) + "\n")
    (repo_dir / "src/retail/adhoc_check.sql").write_text(
        "SELECT COUNT(*) AS n FROM cos.ref.branch WHERE region = 'x';\n"
    )
    (repo_dir / "src/risk/daily_exposure.sql").unlink()

    summary = builder(settings, repo, now=later).refresh()
    assert summary.history_since == NOW and summary.history_until == later
    assert summary.queries_processed == 1 and summary.queries_skipped == 0
    assert (
        summary.files_processed == 1 and summary.files_removed == 1 and summary.files_unchanged == 5
    )
    assert summary.templates_new == 2  # the daily pattern (merged) and the rewritten check
    assert summary.templates_total == 22  # +1 new check, -1 old check, -1 removed job, daily merged

    daily = max(repo.list_templates(), key=lambda t: t.execution_count)
    assert daily.execution_count == 7 and "frank" in daily.users
    assert daily.last_executed_at == datetime(2026, 11, 19, 8, 15, 30, tzinfo=UTC)
    lineage = repo.list_lineage_edges()
    assert not any(e.downstream_table == "cos.risk_dm.daily_exposure" for e in lineage)
    assert {f.path for f in repo.list_sql_files()} == {
        "automation/cards/card_spend_report.sql",
        "automation/ops/broken.sql",
        "src/retail/adhoc_check.sql",
        "src/retail/customer_daily_balance.sql",
        "src/retail/customer_monthly_summary.sql",
        "src/risk/customer_risk_score.sql",
    }
    branch = repo.get_table("cos.ref.branch")
    assert branch is not None and branch.use_count == 3  # eve, the rewritten check, broken.sql#2

    # Nothing new: a further refresh is a no-op.
    again = builder(settings, repo, now=later + timedelta(days=1)).refresh()
    assert (
        again.queries_processed == 0 and again.files_processed == 0 and again.templates_total == 23
    )


def test_refresh_without_previous_run_warns(repo: SqliteCatalogRepository) -> None:
    summary = builder(make_settings(), repo).refresh()
    assert any("no previous successful run" in w for w in summary.warnings)
    assert summary.templates_total == 23


def test_failed_run_is_recorded_and_rolled_back(
    repo: SqliteCatalogRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(edges: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(repo, "replace_join_edges", boom)
    with pytest.raises(RuntimeError, match="disk full"):
        builder(make_settings(), repo).build()
    assert repo.list_tables() == []
    runs = repo.list_build_runs()
    assert [r.status for r in runs] == ["failed"]
    assert runs[0].summary == {"error": "RuntimeError"}


def test_unavailable_file_source_is_a_warning(repo: SqliteCatalogRepository) -> None:
    settings = make_settings(sql_files__local_path=Path("does/not/exist"))
    summary = builder(settings, repo).build()
    assert any("sql files unavailable" in w for w in summary.warnings)
    assert summary.files_processed == 0 and summary.queries_processed == 22
    assert repo.list_lineage_edges() == []


def test_report_renders_sections(repo: SqliteCatalogRepository) -> None:
    builder(make_settings(), repo).build()
    text = render_report(repo, top=5)
    for heading in (
        "## Last run",
        "## Tables by usage",
        "## Join graph",
        "## Lineage",
        "## Query patterns",
    ):
        assert heading in text
    assert "cos.retail_ods.account" in text and "retail (inferred)" in text
    assert "cos.risk_dm.daily_exposure <-" in text
    empty = SqliteCatalogRepository("sqlite://")
    empty.initialize()
    assert "- none" in render_report(empty)
