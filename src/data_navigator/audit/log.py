"""Audit logging.

Every entry is stored in the catalog database (``audit_logs``) and, when
``audit.path`` is set, appended as one JSON line to that file.  Entries carry
the question, the *normalised* SQL (constants replaced), validation outcomes
and sample execution facts (confirmed? executed? how many rows?).  They never
carry SQL constants or sample values.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from data_navigator.catalog.records import AuditRecord
from data_navigator.catalog.repository import CatalogRepository
from data_navigator.catalog.types import utcnow
from data_navigator.sqlproc.normalize import NormalizationError, normalize

log = logging.getLogger(__name__)


def deidentify_sql(sql: str | None) -> str | None:
    """Normalise *sql* for the audit trail; unparseable text is replaced by a marker."""
    if not sql:
        return None
    try:
        return normalize(sql).text
    except NormalizationError as error:
        return f"<unparseable sql {error.sql_hash[:16]}>"


class AuditLogger:
    def __init__(
        self,
        repository: CatalogRepository,
        *,
        path: Path | None = None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._repo = repository
        self._path = path
        self._now = now

    def record(
        self,
        *,
        user: str,
        question: str,
        generated_sql: str | None = None,
        validation_status: str | None = None,
        sample_confirmed: bool | None = None,
        sample_executed: bool = False,
        sample_row_count: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> AuditRecord:
        entry = AuditRecord(
            user=user,
            question=question[:2000],
            occurred_at=self._now(),
            generated_sql=deidentify_sql(generated_sql),
            validation_status=validation_status,
            sample_confirmed=sample_confirmed,
            sample_executed=sample_executed,
            sample_row_count=sample_row_count,
            details=dict(details or {}),
        )
        try:
            entry.id = self._repo.add_audit_entry(entry)
        except Exception as error:  # pragma: no cover - storage failure must not break the agent
            log.error("audit entry could not be stored: %s", type(error).__name__)
        if self._path is not None:
            self._append_jsonl(entry)
        return entry

    def _append_jsonl(self, entry: AuditRecord) -> None:
        assert self._path is not None
        payload = {
            "id": entry.id,
            "occurred_at": entry.occurred_at.isoformat() if entry.occurred_at else None,
            "user": entry.user,
            "question": entry.question,
            "generated_sql": entry.generated_sql,
            "validation_status": entry.validation_status,
            "sample_confirmed": entry.sample_confirmed,
            "sample_executed": entry.sample_executed,
            "sample_row_count": entry.sample_row_count,
            "details": entry.details,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError as error:  # pragma: no cover - disk problems must not break the agent
            log.error("audit file could not be written: %s", type(error).__name__)
