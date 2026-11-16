"""Plain data records exchanged with the catalog repository.

Business code never touches ORM objects; it reads and writes these records so a
different storage backend can implement ``CatalogRepository`` without leaking
SQLAlchemy details.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ColumnRecord:
    name: str
    ordinal_position: int
    data_type: str
    comment: str | None = None


@dataclass(frozen=True)
class TableRecord:
    """A table as seen in ``information_schema`` (input to ``upsert_tables``)."""

    full_name: str
    catalog: str
    schema_name: str
    table_name: str
    columns: tuple[ColumnRecord, ...] = ()
    table_type: str | None = None
    comment: str | None = None

    @property
    def structure_hash(self) -> str:
        return structure_hash(self.columns)


def structure_hash(columns: Iterable[ColumnRecord]) -> str:
    """Hash of the ordered (name, type) pairs; comments do not affect it."""
    ordered = sorted(columns, key=lambda c: (c.ordinal_position, c.name))
    payload = "\n".join(f"{c.name.lower()}:{c.data_type.lower()}" for c in ordered)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class ColumnState:
    name: str
    ordinal_position: int
    data_type: str
    comment: str | None = None
    ai_description: str | None = None
    use_count: int = 0
    join_key_count: int = 0
    filter_count: int = 0
    is_join_key: bool = False
    is_filter: bool = False
    pii_flag: bool | None = None


@dataclass
class TableState:
    """A table as stored in the catalog (output of the read methods)."""

    full_name: str
    catalog: str
    schema_name: str
    table_name: str
    structure_hash: str
    first_seen_at: datetime
    last_seen_at: datetime
    table_type: str | None = None
    comment: str | None = None
    owner_team: str | None = None
    owner_source: str | None = None
    last_queried_at: datetime | None = None
    use_count: int = 0
    user_count: int = 0
    team_count: int = 0
    deleted_at: datetime | None = None
    columns: list[ColumnState] = field(default_factory=list)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


@dataclass
class TableDiff:
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


@dataclass
class TemplateRecord:
    fingerprint: str
    normalized_sql: str
    dialect: str
    statement_type: str
    tables_read: list[str] = field(default_factory=list)
    tables_written: list[str] = field(default_factory=list)
    parsed: dict[str, Any] = field(default_factory=dict)
    execution_count: int = 0
    users: list[str] = field(default_factory=list)
    teams: list[str] = field(default_factory=list)
    file_paths: list[str] = field(default_factory=list)
    first_seen_at: datetime | None = None
    last_executed_at: datetime | None = None

    @property
    def source_kinds(self) -> list[str]:
        kinds: list[str] = []
        if self.execution_count > 0:
            kinds.append("history")
        if self.file_paths:
            kinds.append("file")
        return kinds


@dataclass
class SqlFileRecord:
    path: str
    content_hash: str
    commit_sha: str | None = None
    statement_count: int = 0
    parse_failure_count: int = 0
    fingerprints: list[str] = field(default_factory=list)
    processed_at: datetime | None = None


@dataclass(frozen=True)
class ParseFailureRecord:
    sql_hash: str
    dialects_tried: str
    error_type: str
    source_kind: str
    source_ref: str | None = None


@dataclass
class JoinEdgeRecord:
    left_table: str
    left_column: str
    right_table: str
    right_column: str
    join_type: str
    occurrence_count: int = 0
    score: float = 0.0
    sources: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class LineageEdgeRecord:
    upstream_table: str
    downstream_table: str
    source_path: str
    statement_type: str


@dataclass
class ColumnUsage:
    use_count: int = 0
    join_key_count: int = 0
    filter_count: int = 0
    is_join_key: bool = False
    is_filter: bool = False


@dataclass
class TableUsage:
    full_name: str
    use_count: int = 0
    user_count: int = 0
    team_count: int = 0
    last_queried_at: datetime | None = None
    columns: dict[str, ColumnUsage] = field(default_factory=dict)


@dataclass
class BuildRunRecord:
    id: int
    run_type: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    history_processed_until: datetime | None = None
    repo_commit_sha: str | None = None
    tables_added: int = 0
    tables_changed: int = 0
    tables_removed: int = 0
    queries_processed: int = 0
    files_processed: int = 0
    parse_failures: int = 0
    summary: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Table cards (phase 2)
# ---------------------------------------------------------------------------

CARD_STATUS_AI = "ai_inferred"
CARD_STATUS_CONFIRMED = "confirmed"
CARD_STATUS_REJECTED = "rejected"
CARD_STATUSES = (CARD_STATUS_AI, CARD_STATUS_CONFIRMED, CARD_STATUS_REJECTED)
# Fields of a card a reviewer may edit; once edited they are never regenerated.
CARD_EDITABLE_FIELDS = ("purpose", "key_columns", "common_usage", "example_questions")


@dataclass
class KeyColumn:
    name: str
    meaning: str
    confidence: float = 0.0
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "meaning": self.meaning,
            "confidence": self.confidence,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> KeyColumn:
        return cls(
            name=str(data.get("name", "")),
            meaning=str(data.get("meaning", "")),
            confidence=float(data.get("confidence", 0.0) or 0.0),
            evidence=str(data.get("evidence", "") or ""),
        )


@dataclass
class CardRecord:
    """A table card as stored in the catalog."""

    table: str
    purpose: str | None = None
    key_columns: list[KeyColumn] = field(default_factory=list)
    common_usage: str | None = None
    example_questions: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    confidence: float | None = None
    review_status: str = CARD_STATUS_AI
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    confirmed_fields: list[str] = field(default_factory=list)
    input_hash: str | None = None
    generated_at: datetime | None = None
    model: str | None = None

    @property
    def is_confirmed(self) -> bool:
        return self.review_status == CARD_STATUS_CONFIRMED

    @property
    def is_rejected(self) -> bool:
        return self.review_status == CARD_STATUS_REJECTED


@dataclass
class CardDraft:
    """What the generator produces for one table (validated LLM output)."""

    purpose: str
    key_columns: list[KeyColumn]
    common_usage: str
    example_questions: list[str]
    overall_confidence: float
    evidence: list[str]


# ---------------------------------------------------------------------------
# Audit log (phase 4)
# ---------------------------------------------------------------------------


@dataclass
class AuditRecord:
    """One audited agent event. Never carries SQL constants or sample values."""

    user: str
    question: str
    occurred_at: datetime | None = None
    generated_sql: str | None = None
    validation_status: str | None = None
    sample_confirmed: bool | None = None
    sample_executed: bool = False
    sample_row_count: int | None = None
    details: dict[str, Any] = field(default_factory=dict)
    id: int | None = None
