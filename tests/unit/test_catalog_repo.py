from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.exc import StatementError

from data_navigator.catalog.factory import create_repository
from data_navigator.catalog.models import Base
from data_navigator.catalog.records import (
    ColumnRecord,
    ColumnUsage,
    JoinEdgeRecord,
    LineageEdgeRecord,
    ParseFailureRecord,
    SqlFileRecord,
    TableRecord,
    TableUsage,
    TemplateRecord,
)
from data_navigator.catalog.sqlite_repo import SqliteCatalogRepository
from data_navigator.config import StorageSettings

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> SqliteCatalogRepository:
    repository = SqliteCatalogRepository(f"sqlite:///{tmp_path}/catalog.db")
    repository.initialize()
    return repository


def table(name: str, *columns: tuple[str, str], comment: str | None = None) -> TableRecord:
    catalog, schema, table_name = name.split(".")
    return TableRecord(
        full_name=name,
        catalog=catalog,
        schema_name=schema,
        table_name=table_name,
        table_type="BASE TABLE",
        comment=comment,
        columns=tuple(
            ColumnRecord(name=col, ordinal_position=i + 1, data_type=typ)
            for i, (col, typ) in enumerate(columns)
        ),
    )


def test_migrations_match_models(repo: SqliteCatalogRepository) -> None:
    repo.initialize()  # idempotent
    with repo.engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "render_as_batch": True}
        )
        diff = compare_metadata(context, Base.metadata)
    assert diff == []


def test_upsert_tables_tracks_added_changed_unchanged(repo: SqliteCatalogRepository) -> None:
    first = repo.upsert_tables([table("cos.a.t", ("id", "bigint"))], seen_at=NOW)
    assert first.added == ["cos.a.t"] and not first.changed

    again = repo.upsert_tables([table("cos.a.t", ("id", "bigint"))], seen_at=NOW)
    assert again.unchanged == ["cos.a.t"]

    changed = repo.upsert_tables(
        [table("cos.a.t", ("id", "bigint"), ("v", "varchar"), comment="c")], seen_at=NOW
    )
    assert changed.changed == ["cos.a.t"]
    state = repo.get_table("COS.A.T")
    assert state is not None
    assert [c.name for c in state.columns] == ["id", "v"]
    assert state.comment == "c"

    removed_col = repo.upsert_tables([table("cos.a.t", ("v", "varchar"))], seen_at=NOW)
    assert removed_col.changed == ["cos.a.t"]
    state = repo.get_table("cos.a.t")
    assert state is not None and [c.name for c in state.columns] == ["v"]


def test_soft_delete_and_readd(repo: SqliteCatalogRepository) -> None:
    repo.upsert_tables([table("cos.a.t", ("id", "bigint"))], seen_at=NOW)
    assert repo.mark_tables_deleted(["cos.a.t", "cos.a.missing"], at=NOW) == ["cos.a.t"]
    assert repo.mark_tables_deleted(["cos.a.t"], at=NOW) == []
    assert repo.list_tables() == []
    deleted = repo.list_tables(include_deleted=True)
    assert len(deleted) == 1 and deleted[0].is_deleted

    diff = repo.upsert_tables([table("cos.a.t", ("id", "bigint"))], seen_at=NOW)
    assert diff.added == ["cos.a.t"]
    assert not repo.list_tables()[0].is_deleted


def test_confirmed_owner_is_never_overwritten(repo: SqliteCatalogRepository) -> None:
    repo.upsert_tables([table("cos.retail_ods.t", ("id", "bigint"))], seen_at=NOW)
    assert repo.set_owner("cos.retail_ods.t", "retail", source="inferred")
    assert not repo.set_owner("cos.retail_ods.t", "retail", source="inferred")
    assert repo.set_owner("cos.retail_ods.t", "cards", source="confirmed")
    assert not repo.set_owner("cos.retail_ods.t", "retail", source="inferred")
    state = repo.get_table("cos.retail_ods.t")
    assert state is not None and (state.owner_team, state.owner_source) == ("cards", "confirmed")
    assert repo.set_owner("cos.retail_ods.t", "risk", source="confirmed")
    assert not repo.set_owner("cos.a.unknown", "x", source="inferred")


def test_schema_map(repo: SqliteCatalogRepository) -> None:
    repo.upsert_tables(
        [table("cos.a.t", ("id", "bigint"), ("v", "varchar")), table("cos.b.u", ("k", "date"))],
        seen_at=NOW,
    )
    repo.mark_tables_deleted(["cos.b.u"], at=NOW)
    assert repo.schema_map() == {"cos": {"a": {"t": {"id": "bigint", "v": "varchar"}}}}


def test_templates_merge_on_upsert(repo: SqliteCatalogRepository) -> None:
    base = TemplateRecord(
        fingerprint="f1",
        normalized_sql="SELECT ?",
        dialect="trino",
        statement_type="select",
        tables_read=["cos.a.t"],
        execution_count=2,
        users=["alice"],
        teams=["retail"],
        first_seen_at=NOW,
        last_executed_at=NOW,
    )
    repo.upsert_templates([base])
    later = TemplateRecord(
        fingerprint="f1",
        normalized_sql="SELECT ?",
        dialect="trino",
        statement_type="select",
        tables_read=["cos.a.t"],
        execution_count=3,
        users=["bob"],
        teams=["retail", "cards"],
        file_paths=["src/x.sql"],
        first_seen_at=NOW - timedelta(days=1),
        last_executed_at=NOW + timedelta(days=1),
    )
    repo.upsert_templates([later])
    (merged,) = repo.list_templates()
    assert merged.execution_count == 5
    assert merged.users == ["alice", "bob"]
    assert merged.teams == ["cards", "retail"]
    assert merged.file_paths == ["src/x.sql"]
    assert merged.source_kinds == ["history", "file"]
    assert merged.first_seen_at == NOW - timedelta(days=1)
    assert merged.last_executed_at == NOW + timedelta(days=1)

    repo.clear_templates()
    assert repo.list_templates() == []


def test_detach_file_removes_file_only_templates(repo: SqliteCatalogRepository) -> None:
    repo.upsert_templates(
        [
            TemplateRecord("file-only", "SELECT ?", "trino", "select", file_paths=["src/a.sql"]),
            TemplateRecord(
                "shared", "SELECT ?", "trino", "select", execution_count=1, file_paths=["src/a.sql"]
            ),
        ]
    )
    repo.upsert_sql_files(
        [SqlFileRecord(path="src/a.sql", content_hash="h", fingerprints=["file-only", "shared"])]
    )
    repo.detach_file("src/a.sql")
    remaining = {t.fingerprint: t for t in repo.list_templates()}
    assert set(remaining) == {"shared"}
    assert remaining["shared"].file_paths == []
    assert remaining["shared"].source_kinds == ["history"]
    repo.detach_file("src/unknown.sql")  # no-op


def test_sql_files_roundtrip_and_delete(repo: SqliteCatalogRepository) -> None:
    repo.upsert_templates([TemplateRecord("f", "SELECT ?", "trino", "select", file_paths=["p"])])
    repo.upsert_sql_files([SqlFileRecord(path="p", content_hash="h1", fingerprints=["f"])])
    repo.upsert_sql_files([SqlFileRecord(path="p", content_hash="h2", commit_sha="abc")])
    (record,) = repo.list_sql_files()
    assert (record.content_hash, record.commit_sha, record.fingerprints) == ("h2", "abc", [])
    assert record.processed_at is not None
    repo.delete_sql_files(["p"])
    assert repo.list_sql_files() == []


def test_parse_failures_are_counted_not_stored(repo: SqliteCatalogRepository) -> None:
    failure = ParseFailureRecord("hash1", "trino,spark", "ParseError", "history")
    repo.record_parse_failures([failure, failure], seen_at=NOW)
    repo.record_parse_failures(
        [ParseFailureRecord("hash1", "trino", "ParseError", "file", "src/a.sql")], seen_at=NOW
    )
    assert repo.count_parse_failures() == 2


def test_join_and_lineage_edges_are_replaced(repo: SqliteCatalogRepository) -> None:
    repo.replace_join_edges([JoinEdgeRecord("a", "id", "b", "a_id", "INNER", 3, 6.0, {"file": 1})])
    repo.replace_join_edges([JoinEdgeRecord("a", "id", "c", "a_id", "LEFT", 1, 1.0)])
    (edge,) = repo.list_join_edges()
    assert (edge.right_table, edge.join_type, edge.score) == ("c", "LEFT", 1.0)

    repo.replace_lineage_edges([LineageEdgeRecord("a", "b", "src/x.sql", "insert")])
    repo.replace_lineage_edges([LineageEdgeRecord("a", "d", "src/y.sql", "ctas")])
    assert repo.list_lineage_edges() == [LineageEdgeRecord("a", "d", "src/y.sql", "ctas")]


def test_update_usage_resets_missing(repo: SqliteCatalogRepository) -> None:
    repo.upsert_tables(
        [table("cos.a.t", ("id", "bigint"), ("v", "varchar")), table("cos.a.u", ("k", "date"))],
        seen_at=NOW,
    )
    repo.update_usage(
        [
            TableUsage(
                "cos.a.t",
                use_count=5,
                user_count=2,
                team_count=1,
                last_queried_at=NOW,
                columns={"id": ColumnUsage(5, 3, 0, True, False)},
            ),
            TableUsage("cos.a.u", use_count=1),
        ]
    )
    t = repo.get_table("cos.a.t")
    assert t is not None and t.use_count == 5 and t.last_queried_at == NOW
    assert t.columns[0].is_join_key and t.columns[0].join_key_count == 3
    assert not t.columns[1].is_join_key

    repo.update_usage([TableUsage("cos.a.t", use_count=1)])
    u = repo.get_table("cos.a.u")
    assert u is not None and u.use_count == 0
    repo.update_usage([TableUsage("cos.a.u", use_count=9)], reset_missing=False)
    t = repo.get_table("cos.a.t")
    assert t is not None and t.use_count == 1


def test_build_runs(repo: SqliteCatalogRepository) -> None:
    assert repo.last_successful_run() is None
    run_id = repo.start_build_run("build", started_at=NOW)
    repo.finish_build_run(
        run_id,
        status="failed",
        finished_at=NOW,
        summary={"error": "boom"},
    )
    assert repo.last_successful_run() is None
    second = repo.start_build_run("refresh", started_at=NOW)
    repo.finish_build_run(
        second,
        status="succeeded",
        finished_at=NOW + timedelta(minutes=1),
        history_processed_until=NOW,
        repo_commit_sha="abc",
        tables_added=2,
    )
    last = repo.last_successful_run()
    assert last is not None and last.id == second and last.tables_added == 2
    assert last.history_processed_until == NOW
    assert [r.status for r in repo.list_build_runs()] == ["failed", "succeeded"]
    with pytest.raises(KeyError):
        repo.finish_build_run(999, status="succeeded", finished_at=NOW)
    with pytest.raises(AttributeError):
        repo.finish_build_run(second, status="succeeded", finished_at=NOW, nope=1)


def test_transaction_rolls_back_on_error(repo: SqliteCatalogRepository) -> None:
    with pytest.raises(RuntimeError), repo.transaction():
        repo.upsert_tables([table("cos.a.t", ("id", "bigint"))], seen_at=NOW)
        with repo.transaction():  # nested: joins the outer transaction
            repo.upsert_tables([table("cos.a.u", ("id", "bigint"))], seen_at=NOW)
        raise RuntimeError("abort")
    assert repo.list_tables() == []


def test_naive_datetimes_are_rejected(repo: SqliteCatalogRepository) -> None:
    with pytest.raises(StatementError):
        repo.start_build_run("build", started_at=datetime(2026, 1, 1))


def test_factory_and_postgres_stub(tmp_path: Path) -> None:
    repo = create_repository(StorageSettings(url=f"sqlite:///{tmp_path}/nested/dir/c.db"))
    repo.initialize()
    assert (tmp_path / "nested" / "dir" / "c.db").exists()
    with pytest.raises(NotImplementedError):
        create_repository(StorageSettings(backend="postgres", url="postgresql://x"))
    with pytest.raises(ValueError, match="SQLite"):
        SqliteCatalogRepository("postgresql://nope")


# -- table cards and audit (phase 2 / 4) -------------------------------------------


def test_cards_are_saved_reviewed_and_protected(repo: SqliteCatalogRepository) -> None:
    from data_navigator.catalog.records import AuditRecord, CardDraft, KeyColumn

    repo.upsert_tables([table("c.s.t", ("id", "bigint"), ("v", "double"))], seen_at=NOW)
    draft = CardDraft(
        purpose="first",
        key_columns=[KeyColumn("id", "identifier", 0.9, "comment")],
        common_usage="usage",
        example_questions=["q1"],
        overall_confidence=0.8,
        evidence=["e"],
    )
    assert repo.get_card("c.s.t") is None and repo.list_cards() == []
    assert (
        repo.save_generated_card("c.s.missing", draft, input_hash="h", model="m", generated_at=NOW)
        is None
    )
    card = repo.save_generated_card("c.s.t", draft, input_hash="h1", model="m", generated_at=NOW)
    assert card is not None and card.purpose == "first" and card.key_columns[0].name == "id"
    assert card.review_status == "ai_inferred" and card.input_hash == "h1"
    assert [c.table for c in repo.list_cards(status="ai_inferred")] == ["c.s.t"]

    edited = repo.review_card(
        "c.s.t",
        status="ai_inferred",
        reviewed_by="eve",
        reviewed_at=NOW,
        edits={
            "example_questions": ["human q"],
            "key_columns": [{"name": "v", "meaning": "value"}],
        },
    )
    assert edited is not None and edited.confirmed_fields == ["example_questions", "key_columns"]
    assert edited.key_columns[0].name == "v"
    draft.purpose = "second"
    again = repo.save_generated_card("c.s.t", draft, input_hash="h2", model="m", generated_at=NOW)
    assert again is not None and again.purpose == "second"
    assert again.example_questions == ["human q"] and again.key_columns[0].name == "v"

    confirmed = repo.review_card("c.s.t", status="confirmed", reviewed_by="eve", reviewed_at=NOW)
    assert confirmed is not None and confirmed.is_confirmed
    draft.purpose = "third"
    kept = repo.save_generated_card("c.s.t", draft, input_hash="h3", model="m", generated_at=NOW)
    assert kept is not None and kept.purpose == "second" and kept.input_hash == "h3"

    with pytest.raises(ValueError, match="unknown card status"):
        repo.review_card("c.s.t", status="bogus", reviewed_by="eve", reviewed_at=NOW)
    with pytest.raises(ValueError, match="cannot be edited"):
        repo.review_card(
            "c.s.t", status="confirmed", reviewed_by="eve", reviewed_at=NOW, edits={"model": "x"}
        )
    assert (
        repo.review_card("c.s.none", status="confirmed", reviewed_by="eve", reviewed_at=NOW) is None
    )
    fresh = repo.review_card("c.s.t2", status="rejected", reviewed_by="eve", reviewed_at=NOW)
    assert fresh is None

    repo.set_column_descriptions("c.s.t", {"ID": "the id", "ghost": "x"})
    repo.set_column_descriptions("c.s.none", {"id": "x"})
    state = repo.get_table("c.s.t")
    assert state is not None and state.columns[0].ai_description == "the id"

    entry_id = repo.add_audit_entry(
        AuditRecord(user="alice", question="q", generated_sql="SELECT ?", validation_status="ok")
    )
    repo.add_audit_entry(
        AuditRecord(user="bob", question="q2", sample_executed=True, sample_row_count=3)
    )
    entries = repo.list_audit_entries()
    assert [e.user for e in entries] == ["bob", "alice"] and entries[1].id == entry_id
    assert entries[0].sample_row_count == 3 and entries[1].occurred_at is not None
    assert [e.user for e in repo.list_audit_entries(user="alice", limit=5)] == ["alice"]
