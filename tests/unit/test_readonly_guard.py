from __future__ import annotations

import pytest
from sqlglot import exp, parse_one

from data_navigator.sqlproc.readonly_guard import GuardResult, apply_row_limit, readonly_guard

REJECTED = [
    "",
    "   \n",
    "/* only a comment */",
    "-- only a comment\n",
    "DROP TABLE t",
    "INSERT INTO t SELECT 1",
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET a = 1",
    "DELETE FROM t WHERE 1 = 1",
    "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE",
    "CREATE TABLE t AS SELECT 1",
    "CREATE TABLE t (a INT)",
    "CREATE OR REPLACE VIEW v AS SELECT 1",
    "CREATE SCHEMA s",
    "ALTER TABLE t ADD COLUMN c INT",
    "TRUNCATE TABLE t",
    "CALL system.flush_metadata_cache()",
    "SET SESSION query_max_execution_time = '1h'",
    "RESET SESSION x",
    "USE hive.sales",
    "EXECUTE stmt",
    "EXECUTE IMMEDIATE 'DROP TABLE t'",
    "PREPARE s FROM SELECT 1",
    "DEALLOCATE PREPARE s",
    "GRANT SELECT ON t TO alice",
    "COMMIT",
    "ROLLBACK",
    "START TRANSACTION",
    "ANALYZE t",
    "REFRESH MATERIALIZED VIEW mv",
    "COMMENT ON TABLE t IS 'x'",
    "SHOW TABLES",
    "SHOW CATALOGS",
    "DESCRIBE t",
    "EXPLAIN SELECT 1",
    "EXPLAIN ANALYZE SELECT 1",
    "VALUES (1, 2)",
    "SELECT 1; DROP TABLE t",
    "SELECT 1;DROP TABLE t",
    "SELECT 1; SELECT 2",
    "sElEcT 1; dRoP TABLE t",
    "SELECT 1 /* x */; DELETE FROM t",
    "SELECT * FROM t WHERE a = 'x'; DROP TABLE t; --'",
    "SELECT 1 ; DROP TABLE t",
    "SELECT * INTO t2 FROM t",
    "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
    "SELECT * FROM TABLE(system.query(query => 'DELETE FROM t'))",
    "SELECT * FROM TABLE(sequence(start => 1, stop => 10))",
    "DENY SELECT ON t TO alice",
    "SELECT 1 UNION ALL DELETE FROM t",
    "SELECT * FROM t FOR UPDATE",
    "LOAD DATA INPATH 'x' INTO TABLE t",
    "SELECT a FROM t WHERE a = (CALL foo())",
    "SELECT 1 FROM t\x00; DROP TABLE t",
    "ALTER SESSION SET x = 1",
    "KILL QUERY 'x'",
]

ALLOWED = [
    "SELECT 1",
    "select 1",
    "SELECT 1;",
    "SELECT 1; ",
    "SELECT a, b FROM hive.ods.t WHERE a = 1 ORDER BY b LIMIT 5",
    "WITH x AS (SELECT a FROM t) SELECT * FROM x",
    "WITH RECURSIVE r (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT n FROM r",
    "SELECT * FROM (SELECT a FROM t) s",
    "SELECT a FROM t UNION ALL SELECT a FROM u",
    "SELECT a FROM t INTERSECT SELECT a FROM u",
    "SELECT a FROM t EXCEPT SELECT a FROM u",
    "SELECT t.x FROM hive.ods.a CROSS JOIN UNNEST(a.arr) WITH ORDINALITY AS t (x, n)",
    "SELECT x FROM UNNEST(ARRAY[1, 2]) AS t (x)",
    "SELECT id, row_number() OVER (PARTITION BY c ORDER BY d DESC) AS rn FROM t",
    'SELECT "Weird Col" FROM "hive"."ods"."My Table" AS "T"',
    "SELECT * FROM a LEFT JOIN b ON a.id = b.id WHERE b.x IN (SELECT x FROM c)",
    "SELECT 1 -- ; DROP TABLE t",
    "SELECT 1 /* ; DROP TABLE t */",
    "SELECT 1; -- trailing comment",
    "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.id = t.id)",
    "SELECT * FROM t FETCH FIRST 5 ROWS ONLY",
    "SELECT * FROM t LIMIT 10 OFFSET 5",
    "SELECT * FROM a, LATERAL (SELECT * FROM b WHERE b.id = a.id) l",
    "SELECT count(*) FILTER (WHERE a > 1), approx_distinct(b) FROM t GROUP BY c HAVING count(*) > 1",
    "SELECT CAST(a AS VARCHAR), DATE '2024-01-01', INTERVAL '1' DAY FROM t",
    "TABLE hive.ods.a",
]


@pytest.mark.parametrize("sql", REJECTED)
def test_rejects_everything_that_is_not_a_single_select(sql: str) -> None:
    result = readonly_guard(sql)
    assert result.rejected, sql
    assert result.reason


@pytest.mark.parametrize("sql", ALLOWED)
def test_allows_single_read_only_select(sql: str) -> None:
    result = readonly_guard(sql)
    assert result.allowed, (sql, result.reason)
    assert result.statement_type in {"SELECT", "UNION", "INTERSECT", "EXCEPT"}


def test_result_is_frozen_dataclass() -> None:
    result = GuardResult(allowed=True)
    with pytest.raises(AttributeError):
        result.allowed = False  # type: ignore[misc]


def test_spark_dialect_is_supported() -> None:
    assert readonly_guard(
        "SELECT a FROM t LATERAL VIEW explode(arr) x AS y", dialect="spark"
    ).allowed
    assert readonly_guard("INSERT OVERWRITE TABLE t SELECT 1", dialect="spark").rejected


def _limit_value(sql: str) -> int | None:
    node = parse_one(sql, read="trino").args.get("limit")
    if isinstance(node, exp.Limit) and isinstance(node.expression, exp.Literal):
        return int(node.expression.this)
    return None


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT a FROM t", 10),
        ("SELECT a FROM t LIMIT 1000", 10),
        ("SELECT a FROM t LIMIT 3", 3),
        ("SELECT a FROM t LIMIT 10", 10),
        ("SELECT a FROM t UNION ALL SELECT a FROM u LIMIT 100", 10),
        ("SELECT a FROM t UNION ALL SELECT a FROM u", 10),
        ("SELECT a FROM t FETCH FIRST 50 ROWS ONLY", 10),
        ("SELECT a FROM t LIMIT 10 OFFSET 5", 10),
        ("SELECT a FROM t ORDER BY a LIMIT 99999999999", 10),
        ("SELECT a FROM t LIMIT ?", 10),
        ("WITH x AS (SELECT a FROM t LIMIT 500) SELECT * FROM x", 10),
    ],
)
def test_apply_row_limit_caps_rows(sql: str, expected: int) -> None:
    limited = apply_row_limit(parse_one(sql, read="trino"), 10)
    limited_sql = limited.sql(dialect="trino")
    assert _limit_value(limited_sql) == expected, limited_sql
    assert readonly_guard(limited_sql).allowed


def test_apply_row_limit_does_not_mutate_input() -> None:
    statement = parse_one("SELECT a FROM t LIMIT 500", read="trino")
    apply_row_limit(statement, 10)
    assert _limit_value(statement.sql(dialect="trino")) == 500


def test_apply_row_limit_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        apply_row_limit(parse_one("SELECT 1", read="trino"), 0)
    with pytest.raises(TypeError):
        apply_row_limit(parse_one("DROP TABLE t", read="trino"), 10)
