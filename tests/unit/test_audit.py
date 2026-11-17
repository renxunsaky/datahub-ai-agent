from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from data_navigator.audit.log import AuditLogger, deidentify_sql
from data_navigator.catalog.sqlite_repo import SqliteCatalogRepository

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def test_deidentify_sql_strips_constants() -> None:
    text = deidentify_sql("SELECT a FROM cos.s.t WHERE phone = '+3361234' AND x = 42")
    assert text is not None and "+3361234" not in text and "42" not in text
    assert deidentify_sql(None) is None and deidentify_sql("") is None
    unparseable = deidentify_sql("not sql at all ((")
    assert unparseable is not None and unparseable.startswith("<unparseable sql ")


def test_audit_logger_writes_db_and_jsonl(tmp_path: Path) -> None:
    repo = SqliteCatalogRepository(f"sqlite:///{tmp_path}/catalog.db")
    repo.initialize()
    path = tmp_path / "logs" / "audit.jsonl"
    logger = AuditLogger(repo, path=path, now=lambda: NOW)
    entry = logger.record(
        user="alice",
        question="balances for +3361234",
        generated_sql="SELECT balance FROM cos.retail_ods.account WHERE customer_id = 7",
        validation_status="ok",
        sample_confirmed=True,
        sample_executed=True,
        sample_row_count=3,
        details={"mode": "sql"},
    )
    assert entry.id is not None and entry.generated_sql is not None
    assert "7" not in entry.generated_sql.split("WHERE")[1]
    stored = repo.list_audit_entries()
    assert len(stored) == 1 and stored[0].sample_row_count == 3 and stored[0].occurred_at == NOW
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["user"] == "alice" and payload["details"] == {"mode": "sql"}
    assert "customer_id = 7" not in lines[0]
    AuditLogger(repo, path=None).record(user="bob", question="q")
    assert len(repo.list_audit_entries()) == 2 and len(path.read_text().splitlines()) == 1
