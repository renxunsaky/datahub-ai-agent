from __future__ import annotations

import pytest
from sqlglot import exp

from data_navigator.starburst.whitelist import BuilderPolicyError, check_builder_statement

HISTORY = ["hist.logs.query_history"]

ALLOWED = [
    "SELECT table_name FROM hive.information_schema.tables WHERE table_schema = 'x'",
    "SELECT * FROM information_schema.columns",
    "SELECT * FROM system.metadata.table_comments",
    "SELECT * FROM SYSTEM.METADATA.TABLE_COMMENTS",
    "SHOW COLUMNS FROM hive.ods.t",
    "SHOW TABLES FROM hive.ods",
    "SHOW SCHEMAS FROM hive",
    "SHOW CATALOGS",
    "SHOW CREATE TABLE hive.ods.t",
    "DESCRIBE hive.ods.t",
    "DESC hive.ods.t",
    "SELECT query, user FROM hist.logs.query_history WHERE state = 'FINISHED'",
    "SELECT query FROM HIST.LOGS.Query_History",
    'SELECT query FROM "hist"."logs"."query_history"',
    "WITH t AS (SELECT * FROM hive.information_schema.tables) SELECT * FROM t",
    (
        "SELECT * FROM hive.information_schema.tables t "
        "JOIN system.metadata.table_comments c ON t.table_name = c.table_name"
    ),
    "SELECT 1",
    "SELECT * FROM information_schema.tables UNION ALL SELECT * FROM system.metadata.table_comments",
]

REJECTED = [
    "",
    "SELECT * FROM hive.ods.customer",
    "SELECT * FROM t",
    "SELECT * FROM system.runtime.queries",
    "SELECT * FROM system.runtime.nodes",
    "SELECT * FROM information_schema",
    'SELECT * FROM "hive"."ods"."t"',
    "DROP TABLE hive.information_schema.tables",
    "INSERT INTO hive.ods.t SELECT * FROM information_schema.tables",
    "CREATE TABLE x AS SELECT * FROM information_schema.tables",
    "SELECT * FROM information_schema.tables; SELECT * FROM hive.ods.t",
    "CALL system.flush_metadata_cache()",
    "SET SESSION x = 'y'",
    "EXECUTE stmt",
    "EXECUTE IMMEDIATE 'SELECT * FROM information_schema.tables'",
    "USE hive.ods",
    "EXPLAIN SELECT * FROM information_schema.tables",
    "SELECT * FROM TABLE(system.query(query => 'SELECT 1'))",
    "SELECT * FROM information_schema.tables UNION ALL SELECT * FROM hive.ods.t",
    "SELECT * FROM (SELECT * FROM hive.ods.t) x",
    "WITH t AS (SELECT * FROM hive.ods.secret) SELECT * FROM t",
    "SELECT * FROM information_schema.tables t JOIN hive.ods.x ON t.table_name = x.name",
    "SELECT * FROM information_schema.tables WHERE table_name IN (SELECT name FROM hive.ods.x)",
    "SELECT * INTO hive.ods.copy FROM information_schema.tables",
    "GRANT SELECT ON information_schema.tables TO alice",
    "DESCRIBE INPUT stmt; DROP TABLE t",
    "SHOW TABLES; DROP TABLE t",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed(sql: str) -> None:
    statement = check_builder_statement(sql, allowed_tables=HISTORY)
    assert isinstance(statement, exp.Expr)


@pytest.mark.parametrize("sql", REJECTED)
def test_rejected(sql: str) -> None:
    with pytest.raises(BuilderPolicyError):
        check_builder_statement(sql, allowed_tables=HISTORY)


def test_history_table_must_be_configured() -> None:
    sql = "SELECT query FROM hist.logs.query_history"
    with pytest.raises(BuilderPolicyError, match="outside the builder whitelist"):
        check_builder_statement(sql)
    with pytest.raises(BuilderPolicyError):
        check_builder_statement(sql, allowed_tables=["hist.logs.other"])
    check_builder_statement(sql, allowed_tables=[' "HIST"."logs"."query_history" '])


def test_policy_error_is_a_permission_error() -> None:
    assert issubclass(BuilderPolicyError, PermissionError)
