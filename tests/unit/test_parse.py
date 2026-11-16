from __future__ import annotations

from pathlib import Path

import pytest
from sqlglot import exp

from data_navigator.sources.metadata import FixtureMetadataSource
from data_navigator.sqlproc.normalize import normalize
from data_navigator.sqlproc.parse import JoinRef, ParsedQuery, TableResolver, parse
from data_navigator.sqlproc.statements import quiet_sqlglot

SchemaMap = dict[str, dict[str, dict[str, dict[str, str]]]]


@pytest.fixture(scope="module")
def schema() -> SchemaMap:
    quiet_sqlglot()
    snapshot = FixtureMetadataSource(Path("tests/fixtures/information_schema")).fetch()
    result: SchemaMap = {}
    for table in snapshot.tables:
        result.setdefault(table.catalog, {}).setdefault(table.schema_name, {})[table.table_name] = {
            c.name: c.data_type for c in table.columns
        }
    return result


@pytest.fixture(scope="module")
def resolver(schema: SchemaMap) -> TableResolver:
    known = [
        f"{c}.{s}.{t}"
        for c, schemas in schema.items()
        for s, tables in schemas.items()
        for t in tables
    ]
    return TableResolver(known, placeholder="dn_tpl_var")


def _table(name: str) -> exp.Table:
    table = exp.to_table(name)
    assert isinstance(table, exp.Table)
    return table


def run(
    sql: str,
    schema: SchemaMap,
    resolver: TableResolver,
    *,
    session_catalog: str | None = None,
    session_schema: str | None = None,
) -> ParsedQuery:
    normalized = normalize(sql, placeholder="dn_tpl_var")
    return parse(
        normalized,
        schema=schema,
        resolver=resolver.with_defaults(session_catalog, session_schema),
    )


def test_join_filters_group_by_and_aggregates(schema: SchemaMap, resolver: TableResolver) -> None:
    parsed = run(
        "SELECT c.customer_id, c.segment_code, SUM(a.balance) AS total_balance "
        "FROM cos.retail_ods.account a JOIN cos.retail_ods.customer c ON a.customer_id = c.customer_id "
        "WHERE a.dt = DATE '2026-10-01' AND a.status = 'OPEN' GROUP BY 1, 2",
        schema,
        resolver,
    )
    assert parsed.statement_type == "select" and parsed.qualified
    assert parsed.tables_read == ["cos.retail_ods.account", "cos.retail_ods.customer"]
    assert parsed.tables_written == []
    assert parsed.columns_by_table == {
        "cos.retail_ods.account": ["balance", "customer_id", "dt", "status"],
        "cos.retail_ods.customer": ["customer_id", "segment_code"],
    }
    assert parsed.join_edges == [
        JoinRef(
            "cos.retail_ods.account",
            "customer_id",
            "cos.retail_ods.customer",
            "customer_id",
            "INNER",
        )
    ]
    assert parsed.filter_columns == [
        ("cos.retail_ods.account", "dt"),
        ("cos.retail_ods.account", "status"),
    ]
    assert parsed.group_by_columns == [
        ("cos.retail_ods.customer", "customer_id"),
        ("cos.retail_ods.customer", "segment_code"),
    ]
    assert parsed.aggregates == ["SUM(cos.retail_ods.account.balance)"]


def test_cte_subquery_window_and_unnest(schema: SchemaMap, resolver: TableResolver) -> None:
    parsed = run(
        "WITH latest AS (SELECT customer_id, max(score_date) AS score_date FROM cos.risk_dm.customer_risk_score GROUP BY customer_id), "
        "bal AS (SELECT * FROM (SELECT customer_id, total_balance FROM cos.retail_dm.customer_daily_balance) inner_q) "
        "SELECT s.customer_id, s.pd_score, b.total_balance, t.tag, "
        "row_number() OVER (PARTITION BY s.customer_id ORDER BY s.score_date DESC) AS rn "
        "FROM cos.risk_dm.customer_risk_score s "
        "JOIN latest l ON s.customer_id = l.customer_id AND s.score_date = l.score_date "
        "LEFT JOIN bal b ON b.customer_id = s.customer_id "
        "CROSS JOIN UNNEST(ARRAY['a', 'b']) AS t (tag) "
        "WHERE s.pd_score > 0.05",
        schema,
        resolver,
    )
    assert parsed.tables_read == [
        "cos.retail_dm.customer_daily_balance",
        "cos.risk_dm.customer_risk_score",
    ]
    # The join through the CTE resolves to the physical table behind it.
    assert parsed.join_edges == [
        JoinRef(
            "cos.retail_dm.customer_daily_balance",
            "customer_id",
            "cos.risk_dm.customer_risk_score",
            "customer_id",
            "RIGHT",
        )
    ]
    assert ("cos.risk_dm.customer_risk_score", "pd_score") in parsed.filter_columns
    assert parsed.aggregates == [
        "MAX(cos.risk_dm.customer_risk_score.score_date)"
    ]  # window functions excluded
    assert "total_balance" in parsed.columns_by_table["cos.retail_dm.customer_daily_balance"]


def test_insert_select_and_lineage_tables(schema: SchemaMap, resolver: TableResolver) -> None:
    parsed = run(
        "INSERT INTO cos.retail_dm.customer_daily_balance "
        "SELECT c.customer_id, DATE 'dn_tpl_var' AS as_of_date, SUM(a.balance), COUNT(*) "
        "FROM cos.retail_ods.account a JOIN cos.retail_ods.customer c ON a.customer_id = c.customer_id "
        "WHERE a.dt = DATE 'dn_tpl_var' GROUP BY c.customer_id",
        schema,
        resolver,
    )
    assert parsed.statement_type == "insert"
    assert parsed.tables_written == ["cos.retail_dm.customer_daily_balance"]
    assert parsed.tables_read == ["cos.retail_ods.account", "cos.retail_ods.customer"]
    assert parsed.aggregates == ["COUNT(*)", "SUM(cos.retail_ods.account.balance)"]


def test_ctas_with_cte_and_merge(schema: SchemaMap, resolver: TableResolver) -> None:
    ctas = run(
        "CREATE TABLE cos.cards_dm.card_spend_monthly WITH (format = 'ORC') AS "
        "WITH spend AS (SELECT cd.account_id, SUM(ct.amount) AS spend FROM cos.cards_ods.card_transaction ct "
        "JOIN cos.cards_ods.card cd ON ct.card_id = cd.card_id GROUP BY cd.account_id) "
        "SELECT s.account_id, s.spend FROM spend s JOIN cos.retail_ods.account a ON s.account_id = a.account_id",
        schema,
        resolver,
    )
    assert ctas.statement_type == "ctas"
    assert ctas.tables_written == ["cos.cards_dm.card_spend_monthly"]
    assert ctas.tables_read == [
        "cos.cards_ods.card",
        "cos.cards_ods.card_transaction",
        "cos.retail_ods.account",
    ]
    assert (
        JoinRef("cos.cards_ods.card", "account_id", "cos.retail_ods.account", "account_id", "INNER")
        in ctas.join_edges
    )
    assert (
        JoinRef(
            "cos.cards_ods.card", "card_id", "cos.cards_ods.card_transaction", "card_id", "INNER"
        )
        in ctas.join_edges
    )

    merge = run(
        "MERGE INTO cos.risk_dm.customer_risk_score t USING ("
        "SELECT b.customer_id, b.total_balance FROM cos.retail_dm.customer_daily_balance b "
        "JOIN cos.retail_ods.customer c ON b.customer_id = c.customer_id) s "
        "ON t.customer_id = s.customer_id "
        "WHEN MATCHED THEN UPDATE SET pd_score = 0.1 "
        "WHEN NOT MATCHED THEN INSERT (customer_id) VALUES (s.customer_id)",
        schema,
        resolver,
    )
    assert merge.statement_type == "merge"
    assert merge.tables_written == ["cos.risk_dm.customer_risk_score"]
    assert merge.tables_read == ["cos.retail_dm.customer_daily_balance", "cos.retail_ods.customer"]
    assert (
        JoinRef(
            "cos.retail_dm.customer_daily_balance",
            "customer_id",
            "cos.risk_dm.customer_risk_score",
            "customer_id",
            "MERGE",
        )
        in merge.join_edges
    )

    merge_table = run(
        "MERGE INTO cos.risk_dm.customer_risk_score t USING cos.retail_dm.customer_daily_balance b "
        "ON t.customer_id = b.customer_id WHEN MATCHED THEN DELETE",
        schema,
        resolver,
    )
    assert merge_table.tables_read == ["cos.retail_dm.customer_daily_balance"]
    assert merge_table.join_edges[0].join_type == "MERGE"


def test_delete_update_filters(schema: SchemaMap, resolver: TableResolver) -> None:
    delete = run(
        "DELETE FROM cos.retail_dm.customer_monthly_summary WHERE month = '2026-01'",
        schema,
        resolver,
    )
    assert delete.tables_written == ["cos.retail_dm.customer_monthly_summary"]
    assert delete.filter_columns == [("cos.retail_dm.customer_monthly_summary", "month")]
    update = run(
        "UPDATE cos.retail_ods.account SET status = 'CLOSED' WHERE account_id IN (SELECT account_id FROM cos.cards_ods.card WHERE status = 'LOST')",
        schema,
        resolver,
    )
    assert update.tables_written == ["cos.retail_ods.account"]
    assert update.tables_read == ["cos.cards_ods.card"]
    assert ("cos.cards_ods.card", "status") in update.filter_columns


def test_quoted_identifiers_and_three_part_names(
    schema: SchemaMap, resolver: TableResolver
) -> None:
    parsed = run(
        'SELECT "T"."customer_id", "T"."Segment_Code" FROM "cos"."retail_ods"."customer" AS "T" '
        'JOIN cos.ref.branch b ON "T".branch_id = b.branch_id WHERE "T"."open_date" > DATE \'2020-01-01\'',
        schema,
        resolver,
    )
    assert parsed.tables_read == ["cos.ref.branch", "cos.retail_ods.customer"]
    assert parsed.join_edges == [
        JoinRef("cos.ref.branch", "branch_id", "cos.retail_ods.customer", "branch_id", "INNER")
    ]
    assert ("cos.retail_ods.customer", "open_date") in parsed.filter_columns


def test_partial_names_resolve_by_session_defaults_or_unique_suffix(
    schema: SchemaMap, resolver: TableResolver
) -> None:
    by_session = run(
        "SELECT customer_id FROM customer WHERE mobile_phone = 'x'",
        schema,
        resolver,
        session_catalog="cos",
        session_schema="retail_ods",
    )
    assert by_session.tables_read == ["cos.retail_ods.customer"]
    assert by_session.unresolved_tables == []

    by_suffix = run(
        "SELECT a.customer_id FROM retail_ods.account a LEFT JOIN ref.product p ON a.product_code = p.product_code",
        schema,
        resolver,
    )
    assert by_suffix.tables_read == ["cos.ref.product", "cos.retail_ods.account"]
    assert by_suffix.join_edges[0].join_type == "RIGHT"  # LEFT flipped by canonical ordering

    unresolved = run(
        "SELECT x FROM nowhere.tbl JOIN cos.ref.branch b ON b.branch_id = tbl.branch_id",
        schema,
        resolver,
    )
    assert unresolved.tables_read == ["cos.ref.branch"]
    assert unresolved.unresolved_tables == ["nowhere.tbl"]
    assert unresolved.join_edges == []

    ambiguous = TableResolver(["a.s.t", "b.s.t"])
    assert ambiguous.resolve(_table("s.t")) is None
    assert ambiguous.resolve(_table("t")) is None
    assert TableResolver(["a.s.t"]).resolve(_table("t")) == "a.s.t"
    assert (
        TableResolver([], default_catalog="c", default_schema="s").resolve(_table("t")) == "c.s.t"
    )
    assert TableResolver([], default_catalog="c").resolve(_table("x.t")) == "c.x.t"


def test_template_tables_and_columns_are_ignored(
    schema: SchemaMap, resolver: TableResolver
) -> None:
    parsed = run(
        "SELECT COUNT(*) FROM cos.retail_dm.customer_daily_balance WHERE customer_id IN (dn_tpl_var) AND dt = 'dn_tpl_var' "
        "UNION ALL SELECT COUNT(*) FROM dn_tpl_var",
        schema,
        resolver,
    )
    assert parsed.tables_read == ["cos.retail_dm.customer_daily_balance"]
    assert parsed.unresolved_tables == []
    assert "dn_tpl_var" not in parsed.columns_by_table["cos.retail_dm.customer_daily_balance"]
    assert ("cos.retail_dm.customer_daily_balance", "customer_id") in parsed.filter_columns


def test_implicit_join_and_select_star_expansion(
    schema: SchemaMap, resolver: TableResolver
) -> None:
    parsed = run(
        "SELECT * FROM cos.retail_ods.account a, cos.ref.product p WHERE a.product_code = p.product_code AND p.category = 'LOAN'",
        schema,
        resolver,
    )
    assert parsed.join_edges == [
        JoinRef(
            "cos.ref.product", "product_code", "cos.retail_ods.account", "product_code", "INNER"
        )
    ]
    assert parsed.filter_columns == [("cos.ref.product", "category")]
    assert parsed.columns_by_table["cos.retail_ods.account"] == [
        "account_id",
        "balance",
        "close_date",
        "currency",
        "customer_id",
        "dt",
        "open_date",
        "product_code",
        "status",
    ]


def test_spark_statement(schema: SchemaMap, resolver: TableResolver) -> None:
    parsed = run(
        "INSERT OVERWRITE TABLE risk_dm.daily_exposure PARTITION (as_of_date = '${ds}') "
        "SELECT a.customer_id, SUM(a.balance) AS exposure_amount, a.product_code FROM retail_ods.account a "
        "LEFT JOIN ref.product p ON a.product_code = p.product_code WHERE a.dt = '2026-01-01' GROUP BY a.customer_id, a.product_code",
        schema,
        resolver,
    )
    assert parsed.tables_written == ["cos.risk_dm.daily_exposure"]
    assert parsed.tables_read == ["cos.ref.product", "cos.retail_ods.account"]
    assert parsed.group_by_columns == [
        ("cos.retail_ods.account", "customer_id"),
        ("cos.retail_ods.account", "product_code"),
    ]


def test_roundtrip_dict(schema: SchemaMap, resolver: TableResolver) -> None:
    parsed = run(
        "SELECT a.customer_id FROM cos.retail_ods.account a JOIN cos.retail_ods.customer c ON a.customer_id = c.customer_id WHERE c.segment_code = 'x'",
        schema,
        resolver,
    )
    data = parsed.to_dict()
    assert isinstance(data["join_edges"][0], dict)
    assert ParsedQuery.from_dict(data) == parsed


def test_unknown_tables_do_not_break_parsing(schema: SchemaMap, resolver: TableResolver) -> None:
    parsed = run(
        "SELECT x.a, y.b FROM other.sch.x x JOIN other.sch.y y ON x.id = y.x_id WHERE x.flag = 1",
        schema,
        resolver,
    )
    assert parsed.tables_read == ["other.sch.x", "other.sch.y"]
    assert parsed.join_edges == [JoinRef("other.sch.x", "id", "other.sch.y", "x_id", "INNER")]
    assert parsed.columns_by_table["other.sch.x"] == ["a", "flag", "id"]
