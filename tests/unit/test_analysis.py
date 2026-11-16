from __future__ import annotations

from datetime import UTC, datetime

from data_navigator.analysis.join_graph import compute_join_graph
from data_navigator.analysis.lineage import compute_lineage
from data_navigator.analysis.ownership import infer_owner
from data_navigator.analysis.usage import compute_usage, template_uses
from data_navigator.catalog.records import LineageEdgeRecord, TemplateRecord
from data_navigator.config import AnalysisSettings, OwnershipRule
from data_navigator.sqlproc.parse import JoinRef, ParsedQuery

T1, T2, T3 = "cos.a.t1", "cos.a.t2", "cos.b.t3"
D1 = datetime(2026, 10, 1, tzinfo=UTC)
D2 = datetime(2026, 10, 9, tzinfo=UTC)


def history_template() -> TemplateRecord:
    parsed = ParsedQuery(
        statement_type="select",
        tables_read=[T1, T2],
        columns_by_table={T1: ["id", "dt"], T2: ["t1_id", "v"]},
        join_edges=[JoinRef(T1, "id", T2, "t1_id", "LEFT")],
        filter_columns=[(T1, "dt")],
        group_by_columns=[(T2, "v")],
        aggregates=["COUNT(*)"],
    )
    return TemplateRecord(
        fingerprint="h",
        normalized_sql="SELECT ?",
        dialect="trino",
        statement_type="select",
        tables_read=[T1, T2],
        parsed=parsed.to_dict(),
        execution_count=3,
        users=["alice", "bob"],
        teams=["retail"],
        last_executed_at=D2,
    )


def file_template(paths: list[str] | None = None, execution_count: int = 0) -> TemplateRecord:
    parsed = ParsedQuery(
        statement_type="insert",
        tables_read=[T1, T3],
        tables_written=[T3],
        columns_by_table={T1: ["id"], T3: ["k"]},
        join_edges=[JoinRef(T1, "id", T3, "k", "INNER")],
        filter_columns=[(T1, "dt")],
    )
    return TemplateRecord(
        fingerprint="f",
        normalized_sql="INSERT ...",
        dialect="trino",
        statement_type="insert",
        tables_read=[T1, T3],
        tables_written=[T3],
        parsed=parsed.to_dict(),
        execution_count=execution_count,
        users=["carol"],
        teams=["risk"],
        file_paths=paths if paths is not None else ["src/job.sql"],
        last_executed_at=D1,
    )


def test_template_uses_counts_executions_and_files() -> None:
    assert template_uses(history_template()) == 3
    assert template_uses(file_template()) == 1
    assert template_uses(file_template(execution_count=2)) == 3


def test_usage_aggregates_tables_and_columns() -> None:
    usage = compute_usage(
        [history_template(), file_template()], join_key_min_count=2, filter_min_count=4
    )
    t1 = usage[T1]
    assert (t1.use_count, t1.user_count, t1.team_count) == (4, 3, 2)
    assert t1.last_queried_at == D2
    assert t1.columns["id"].use_count == 4
    assert t1.columns["id"].join_key_count == 4 and t1.columns["id"].is_join_key
    assert t1.columns["dt"].filter_count == 4 and t1.columns["dt"].is_filter
    t2 = usage[T2]
    assert t2.use_count == 3 and t2.columns["t1_id"].is_join_key and not t2.columns["v"].is_filter
    t3 = usage[T3]
    assert t3.use_count == 1 and t3.last_queried_at == D1
    assert t3.columns["k"].join_key_count == 1 and not t3.columns["k"].is_join_key


def test_join_graph_weights_by_evidence() -> None:
    settings = AnalysisSettings(
        weight_repo_file=3.0, weight_repeated_query=2.0, weight_single_query=1.0
    )
    single = history_template()
    single.fingerprint, single.execution_count = "s", 1
    edges = compute_join_graph([history_template(), file_template(), single], settings)
    by_key = {(e.left_table, e.right_table, e.join_type): e for e in edges}
    left = by_key[(T1, T2, "LEFT")]
    assert left.occurrence_count == 4 and left.score == 3.0
    assert left.sources == {"repeated_query": 1, "single_query": 1}
    inner = by_key[(T1, T3, "INNER")]
    assert inner.occurrence_count == 1 and inner.score == 3.0 and inner.sources == {"repo_file": 1}
    assert edges[0].score >= edges[-1].score
    assert compute_join_graph([], settings) == []


def test_lineage_only_from_files() -> None:
    edges = compute_lineage([history_template(), file_template(paths=["a.sql", "b.sql"])])
    assert edges == [
        LineageEdgeRecord(T1, T3, "a.sql", "insert"),
        LineageEdgeRecord(T1, T3, "b.sql", "insert"),
    ]
    assert compute_lineage([history_template()]) == []
    assert compute_lineage([file_template(paths=[])]) == []


def test_owner_inference_rules() -> None:
    rules = [
        OwnershipRule(pattern=r"^(?P<team>[a-z]+)_", applies_to="schema"),
        OwnershipRule(pattern=r"^ref$", applies_to="schema", team="data_platform"),
        OwnershipRule(
            pattern=r"^cos\.(?P<team>[a-z]+)\.", applies_to="full_name", team=r"owner-\g<team>"
        ),
        OwnershipRule(pattern=r"^bkt-(\w+)", applies_to="bucket"),
    ]
    assert (
        infer_owner(full_name="cos.retail_ods.t", schema_name="retail_ods", rules=rules) == "retail"
    )
    assert infer_owner(full_name="cos.RISK_dm.t", schema_name="RISK_dm", rules=rules) == "risk"
    assert infer_owner(full_name="cos.ref.t", schema_name="ref", rules=rules) == "data_platform"
    assert infer_owner(full_name="cos.misc.t", schema_name="misc", rules=rules) == "owner-misc"
    assert infer_owner(full_name="x.y.z", schema_name="y", rules=rules) is None
    assert (
        infer_owner(full_name="x.y.z", schema_name="y", rules=rules, bucket="bkt-cards") == "cards"
    )
    assert infer_owner(full_name="x.y.z", schema_name="y", rules=[]) is None
    bad = [OwnershipRule(pattern=r"^(?P<team>[a-z]+)_", applies_to="schema", team=r"\g<missing>")]
    assert (
        infer_owner(full_name="cos.retail_ods.t", schema_name="retail_ods", rules=bad)
        == r"\g<missing>".lower()
    )
