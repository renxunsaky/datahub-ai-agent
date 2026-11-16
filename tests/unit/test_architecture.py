"""Architecture and data-safety invariants."""

from __future__ import annotations

import ast
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from importlinter import configuration as importlinter_configuration
from importlinter.application.use_cases import lint_imports

from data_navigator.catalog.sqlite_repo import SqliteCatalogRepository
from data_navigator.config import Secrets, Settings
from data_navigator.pipeline.build import CatalogBuilder
from data_navigator.sources.factory import build_sources

SRC = Path("src/data_navigator")
FORBIDDEN_FOR_AGENT = (
    "data_navigator.starburst.builder_client",
    "data_navigator.starburst.whitelist",
    "data_navigator.sources",
    "data_navigator.pipeline",
)


def test_import_linter_contracts_hold() -> None:
    importlinter_configuration.configure()  # what the ``lint-imports`` CLI does at import time
    assert lint_imports(config_filename="pyproject.toml", no_logo=True)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_agent_and_ui_never_import_builder_or_sources() -> None:
    checked = 0
    for package in ("agent", "ui"):
        for path in (SRC / package).rglob("*.py"):
            checked += 1
            for name in _imports(path):
                assert not name.startswith(FORBIDDEN_FOR_AGENT), (path, name)
    assert checked >= 2


def test_persisted_catalog_contains_no_raw_sql_or_constants(tmp_path: Path) -> None:
    db = tmp_path / "catalog.db"
    repo = SqliteCatalogRepository(f"sqlite:///{db}")
    repo.initialize()
    settings = Settings()
    sources = build_sources(settings, Secrets(_env_file=None))  # type: ignore[call-arg]
    CatalogBuilder(settings, repo, sources, now=lambda: datetime(2026, 11, 16, tzinfo=UTC)).build()

    dump: list[str] = []
    with sqlite3.connect(db) as connection:
        tables = [
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        for table in tables:
            columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            text_columns = [
                c[1]
                for c in columns
                if c[2].upper() in {"TEXT", "VARCHAR", "JSON"} or c[2].upper().startswith("VARCHAR")
            ]
            if not text_columns:
                continue
            selected = ", ".join(f'"{c}"' for c in text_columns)
            for row in connection.execute(f'SELECT {selected} FROM "{table}"'):
                dump.extend(str(v) for v in row if v is not None)
    blob = "\n".join(dump)

    raw_queries = [
        json.loads(line)["query"]
        for line in Path("tests/fixtures/query_history/queries.jsonl").read_text().splitlines()
        if line.strip()
    ]
    for query in raw_queries:
        assert query not in blob
    for constant in (
        "+33612345678",
        "9001234567",
        "AMAZ%",
        "250000.50",
        "'NORTH'",
        "'PRIVATE'",
        "'OPEN'",
        "2026-09-01",
        "oops",
        "SECRET",
    ):
        assert constant not in blob, constant
    for sql_file in Path("tests/fixtures/repo").rglob("*.sql"):
        assert sql_file.read_text() not in blob
