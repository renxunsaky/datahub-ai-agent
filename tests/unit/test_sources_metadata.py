from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from data_navigator.config import MetadataSettings
from data_navigator.sources.metadata import (
    FixtureMetadataSource,
    StarburstMetadataSource,
    assemble_tables,
)
from data_navigator.starburst.builder_client import BuilderClient, BuilderQueryError
from data_navigator.starburst.whitelist import check_builder_statement
from tests.fakes import RoutingConnection, trino_error

FIXTURES = Path("tests/fixtures/information_schema")


def test_fixture_source_reads_bundled_metadata() -> None:
    snapshot = FixtureMetadataSource(FIXTURES).fetch()
    names = [t.full_name for t in snapshot.tables]
    assert len(names) == 12
    assert names == sorted(names)
    customer = next(t for t in snapshot.tables if t.full_name == "cos.retail_ods.customer")
    assert customer.comment == "Retail customer master data (one row per customer)"
    assert [c.name for c in customer.columns][:3] == ["customer_id", "customer_name", "birth_date"]
    assert customer.columns[0].comment == "Customer identifier"
    assert customer.table_type == "BASE TABLE"
    view = next(t for t in snapshot.tables if t.table_name == "v_active_customer")
    assert view.table_type == "VIEW"
    assert snapshot.fetched_at.tzinfo is not None


def test_fixture_source_filters_catalogs_and_schemas() -> None:
    nothing = FixtureMetadataSource(FIXTURES, catalogs=["other"]).fetch()
    assert nothing.tables == []
    filtered = FixtureMetadataSource(FIXTURES, catalogs=["COS"], excluded_schemas=["ref"]).fetch()
    assert all(t.schema_name != "ref" for t in filtered.tables)
    assert len(filtered.tables) == 10


def test_assemble_tables_normalises_case_and_orders_columns() -> None:
    records = assemble_tables(
        [
            {
                "table_catalog": "COS",
                "table_schema": "Sales",
                "table_name": "Orders",
                "table_type": " VIEW ",
            }
        ],
        [
            {
                "table_catalog": "cos",
                "table_schema": "sales",
                "table_name": "orders",
                "column_name": "B",
                "ordinal_position": 2,
                "data_type": "VARCHAR",
                "comment": "",
            },
            {
                "table_catalog": "cos",
                "table_schema": "sales",
                "table_name": "orders",
                "column_name": "a",
                "ordinal_position": 1,
                "data_type": "bigint",
            },
        ],
        [
            {
                "catalog_name": "cos",
                "schema_name": "sales",
                "table_name": "orders",
                "comment": " hi ",
            }
        ],
        catalogs=None,
        excluded_schemas=set(),
    )
    (orders,) = records
    assert orders.full_name == "cos.sales.orders"
    assert orders.table_type == "VIEW" and orders.comment == "hi"
    assert [(c.name, c.data_type, c.comment) for c in orders.columns] == [
        ("a", "bigint", None),
        ("b", "varchar", None),
    ]


# -- Starburst implementation (fake builder connection) -----------------------------------

TABLE_ROWS = [
    ("cos", "retail_ods", "customer", "BASE TABLE"),
    ("cos", "sys", "hidden", "BASE TABLE"),
]
COLUMN_ROWS = [
    ("cos", "retail_ods", "customer", "customer_id", 1, "bigint", "id comment"),
    ("cos", "retail_ods", "customer", "name", 2, "varchar", None),
]


class Router:
    def __init__(self, *, comment_column: bool = True, table_comments: bool = True) -> None:
        self.comment_column = comment_column
        self.table_comments = table_comments
        self.seen: list[str] = []

    def __call__(self, sql: str) -> tuple[Sequence[str], Sequence[Sequence[Any]]]:
        self.seen.append(sql)
        check_builder_statement(sql)  # every statement the source emits must be whitelisted
        upper = sql.upper()
        if upper.startswith("SHOW COLUMNS"):
            return ["Column", "Type", "Extra", "Comment"], [
                ("customer_id", "bigint", "", "from show columns"),
                ("name", "varchar", "", ""),
            ]
        if '"INFORMATION_SCHEMA"."TABLES"' in upper:
            return ["table_catalog", "table_schema", "table_name", "table_type"], TABLE_ROWS
        if '"INFORMATION_SCHEMA"."COLUMNS"' in upper:
            if '"COMMENT"' in upper:
                if not self.comment_column:
                    raise trino_error(
                        "COLUMN_NOT_FOUND", message="Column 'comment' cannot be resolved"
                    )
                cols = [
                    "table_catalog",
                    "table_schema",
                    "table_name",
                    "column_name",
                    "ordinal_position",
                    "data_type",
                    "comment",
                ]
                return cols, COLUMN_ROWS
            cols = [
                "table_catalog",
                "table_schema",
                "table_name",
                "column_name",
                "ordinal_position",
                "data_type",
            ]
            return cols, [row[:-1] for row in COLUMN_ROWS]
        if '"SYSTEM"."METADATA"."TABLE_COMMENTS"' in upper:
            if not self.table_comments:
                raise trino_error("PERMISSION_DENIED")
            return ["catalog_name", "schema_name", "table_name", "comment"], [
                ("cos", "retail_ods", "customer", "Customers")
            ]
        raise AssertionError(f"unexpected statement: {sql}")


def make_source(router: Router, **settings: Any) -> StarburstMetadataSource:
    client = BuilderClient(lambda: RoutingConnection(router))
    return StarburstMetadataSource(
        client, MetadataSettings(source="starburst", catalogs=["cos"], **settings)
    )


def test_starburst_source_uses_information_schema_and_table_comments() -> None:
    router = Router()
    snapshot = make_source(router).fetch()
    (customer,) = snapshot.tables  # "sys" schema is excluded by default
    assert customer.full_name == "cos.retail_ods.customer"
    assert customer.comment == "Customers"
    assert customer.columns[0].comment == "id comment"
    assert not any(sql.upper().startswith("SHOW COLUMNS") for sql in router.seen)
    assert any("NOT" in sql and "'information_schema'" in sql for sql in router.seen)


def test_starburst_source_falls_back_to_show_columns() -> None:
    router = Router(comment_column=False)
    snapshot = make_source(router).fetch()
    (customer,) = snapshot.tables
    assert customer.columns[0].comment == "from show columns"
    assert customer.columns[1].comment is None
    assert sum(sql.upper().startswith("SHOW COLUMNS") for sql in router.seen) == 1


def test_starburst_source_show_columns_strategy() -> None:
    router = Router()
    snapshot = make_source(router, column_comments="show_columns").fetch()
    assert snapshot.tables[0].columns[0].comment == "from show columns"
    assert not any(
        '"COMMENT"' in sql.upper()
        for sql in router.seen
        if "COLUMNS" in sql.upper() and "SHOW" not in sql.upper()
    )


def test_starburst_source_information_schema_strategy_does_not_fall_back() -> None:
    router = Router(comment_column=False)
    with pytest.raises(BuilderQueryError):
        make_source(router, column_comments="information_schema").fetch()


def test_starburst_source_tolerates_missing_table_comments() -> None:
    snapshot = make_source(Router(table_comments=False)).fetch()
    assert snapshot.tables[0].comment is None
