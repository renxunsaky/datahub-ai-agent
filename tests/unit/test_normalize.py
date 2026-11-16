from __future__ import annotations

import re
from datetime import date

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from data_navigator.sqlproc.fingerprint import fingerprint
from data_navigator.sqlproc.normalize import (
    NormalizationError,
    UnsupportedStatementError,
    normalize,
    raw_sql_hash,
)

SKELETON = (
    "SELECT col_zzz, COUNT(*) AS cnt FROM cat_zzz.sch_zzz.tbl_zzz WHERE str_zzz = ? "
    "AND int_zzz = ? AND flt_zzz = ? AND dt_zzz = CAST(? AS DATE) AND ts_zzz > CAST(? AS TIMESTAMP) "
    "AND lst_zzz IN (?) AND neg_zzz = -? AND itv_zzz > CURRENT_TIMESTAMP - INTERVAL (?) DAY "
    "AND hex_zzz = ? GROUP BY col_zzz ORDER BY COUNT(*) DESC LIMIT ?"
)


def build_sql(text: str, number: int, flt: float, day: date, items: list[int], limit: int) -> str:
    escaped = text.replace("'", "''")
    in_list = ", ".join(str(i) for i in items)
    return (
        f"SELECT col_zzz, count(*) AS cnt FROM cat_zzz.sch_zzz.tbl_zzz WHERE str_zzz = '{escaped}' "
        f"AND int_zzz = {number} AND flt_zzz = {flt!r} AND dt_zzz = DATE '{day.isoformat()}' "
        f"AND ts_zzz > TIMESTAMP '{day.isoformat()} 10:11:12' AND lst_zzz IN ({in_list}) "
        f"AND neg_zzz = -{number} AND itv_zzz > now() - INTERVAL '{limit}' DAY "
        f"AND hex_zzz = X'0A{number % 10}F' GROUP BY 1 ORDER BY 2 DESC LIMIT {limit}"
    )


@settings(max_examples=150, deadline=None)
@given(
    text=st.text(
        alphabet=st.characters(categories=("L", "N", "P", "S", "Zs")), min_size=0, max_size=40
    ),
    number=st.integers(min_value=0, max_value=10**12),
    flt=st.floats(min_value=0, max_value=1e9, allow_nan=False, allow_infinity=False),
    day=st.dates(min_value=date(1900, 1, 1), max_value=date(2100, 12, 31)),
    items=st.lists(st.integers(min_value=0, max_value=10**6), min_size=1, max_size=6),
    limit=st.integers(min_value=1, max_value=10**6),
)
def test_normalised_text_is_independent_of_constants(
    text: str, number: int, flt: float, day: date, items: list[int], limit: int
) -> None:
    sql = build_sql(text, number, flt, day, items, limit)
    normalized = normalize(sql)
    assert normalized.text == SKELETON
    assert not re.search(r"\d", normalized.text)
    assert fingerprint(normalized) == fingerprint(SKELETON)


def test_constants_in_every_position_are_removed() -> None:
    sql = """
    -- a comment with a SECRET-TOKEN
    INSERT INTO cos.dm.t (a, b)
    SELECT CAST('2024-01-01' AS DATE), json_extract(x, '$.path') /* another SECRET */
    FROM cos.ods.s
    WHERE d BETWEEN DATE '2024-01-01' AND DATE '2024-02-01'
      AND ts >= TIMESTAMP '2024-01-01 00:00:00.123'
      AND v IN ('x', 'y', 'z') AND w NOT IN (1, 2)
      AND arr[1] = 'q' AND u = U&'\\0041' AND b = X'DEADBEEF'
      AND c = CAST('1.5' AS DECIMAL(18, 2))
    ORDER BY 1 LIMIT 10 OFFSET 5
    """
    normalized = normalize(sql)
    for constant in (
        "SECRET",
        "2024",
        "$.path",
        "'x'",
        "'y'",
        "'z'",
        "1.5",
        "0041",
        "DEADBEEF",
        "0.123",
    ):
        assert constant not in normalized.text, (constant, normalized.text)
    assert "DECIMAL(18, 2)" in normalized.text  # type parameters are structure, not data
    assert normalized.statement_type == "insert"
    assert normalized.text.count("?") >= 10


def test_values_rows_are_collapsed() -> None:
    normalized = normalize("INSERT INTO t (a, b) VALUES (1, 'x'), (2, 'y'), (3, 'z')")
    assert normalized.text == "INSERT INTO t (a, b) VALUES (?, ?)"


def test_positional_references_are_expanded() -> None:
    normalized = normalize("SELECT channel, count(*) AS n FROM t GROUP BY 1 ORDER BY 2 DESC, 1")
    assert (
        normalized.text
        == "SELECT channel, COUNT(*) AS n FROM t GROUP BY channel ORDER BY COUNT(*) DESC, channel"
    )
    star = normalize("SELECT * FROM t ORDER BY 1")
    assert star.text == "SELECT * FROM t ORDER BY ?"
    out_of_range = normalize("SELECT a FROM t GROUP BY 7")
    assert out_of_range.text == "SELECT a FROM t GROUP BY ?"


def test_identifier_case_and_whitespace_do_not_matter() -> None:
    a = normalize("select   A, B\nfrom Cos.Ods.T where A = 1")
    b = normalize("SELECT a, b FROM cos.ods.t WHERE a = 999")
    assert a.text == b.text
    assert fingerprint(a) == fingerprint(b)
    # Trino treats quoted identifiers case-insensitively as well, so they are lower-cased.
    quoted = normalize('SELECT "A" FROM cos.ods.t')
    assert quoted.text == 'SELECT "a" FROM cos.ods.t'


def test_template_placeholder_columns_become_constants() -> None:
    normalized = normalize(
        "SELECT a FROM t WHERE id IN (dn_tpl_var) AND dt = 'dn_tpl_var' AND x = t.DN_TPL_VAR",
        placeholder="dn_tpl_var",
    )
    assert normalized.text == "SELECT a FROM t WHERE id IN (?) AND dt = ? AND x = ?"
    kept = normalize("SELECT a FROM t WHERE id IN (dn_tpl_var)")
    assert "dn_tpl_var" in kept.text


def test_spark_fallback_and_dialect_report() -> None:
    normalized = normalize("SELECT `a` FROM `db`.`t` LATERAL VIEW explode(arr) x AS y WHERE a = 1")
    assert normalized.dialect == "spark"
    assert "?" in normalized.text and "1" not in normalized.text
    trino = normalize("SELECT a FROM cos.ods.t WHERE a = 1")
    assert trino.dialect == "trino"


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("SELECT 1", "select"),
        ("SELECT 1 UNION ALL SELECT 2", "select"),
        ("INSERT INTO t SELECT 1", "insert"),
        ("CREATE TABLE t AS SELECT 1", "ctas"),
        ("CREATE VIEW v AS SELECT 1", "create_view_as"),
        ("CREATE TABLE t (a INT)", "create"),
        ("MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE", "merge"),
        ("DELETE FROM t WHERE a = 1", "delete"),
        ("UPDATE t SET a = 1 WHERE b = 2", "update"),
    ],
)
def test_statement_types(sql: str, kind: str) -> None:
    assert normalize(sql).statement_type == kind


@pytest.mark.parametrize(
    ("sql", "error_type"),
    [
        ("SELEC customer_id FORM customer WHEER x = 'oops'", "ParseError"),
        ("", "EmptyStatement"),
        ("SELECT 1; SELECT 2", "MultipleStatements"),
    ],
)
def test_unparseable_sql_raises_with_hash_only(sql: str, error_type: str) -> None:
    with pytest.raises(NormalizationError) as info:
        normalize(sql)
    error = info.value
    assert error.error_type == error_type
    assert error.dialects_tried == ("trino", "spark")
    assert error.sql_hash == raw_sql_hash(sql)
    assert "oops" not in str(error) and "customer" not in str(error)


@pytest.mark.parametrize(
    "sql",
    ["SHOW TABLES", "EXPLAIN SELECT 1", "SET SESSION x = 1", "USE a.b", "DROP TABLE t", "CALL x()"],
)
def test_non_query_statements_are_unsupported(sql: str) -> None:
    with pytest.raises(UnsupportedStatementError) as info:
        normalize(sql)
    assert info.value.error_type.startswith("Unsupported:")


def test_fingerprint_accepts_text_and_is_stable() -> None:
    assert fingerprint("SELECT ?") == fingerprint("  SELECT ?\n")
    assert len(fingerprint("x")) == 64
