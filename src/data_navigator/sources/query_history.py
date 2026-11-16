"""Historical SQL executed on Starburst."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sqlglot import exp

from data_navigator.config import QueryHistorySettings
from data_navigator.starburst.builder_client import BuilderClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueryHistoryRecord:
    query_id: str
    query_text: str
    started_at: datetime
    user: str | None = None
    state: str | None = None
    ended_at: datetime | None = None
    catalog: str | None = None
    schema: str | None = None


class QueryHistorySource(Protocol):
    def fetch(
        self, *, since: datetime | None, until: datetime | None
    ) -> Iterator[QueryHistoryRecord]: ...


def _window_field(settings: QueryHistorySettings) -> str:
    """Column used for the time window: ``ended_at`` when available (FINISHED queries
    always have one), otherwise ``started_at``."""
    return settings.columns.ended_at or settings.columns.started_at


def parse_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        # TODO(confirm): timezone of the history table's timestamps; UTC is assumed.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


# ---------------------------------------------------------------------------
# JSONL fixture implementation
# ---------------------------------------------------------------------------


class JsonlQueryHistorySource:
    """Reads one JSON object per line, keyed like the configured history columns."""

    def __init__(self, path: Path, settings: QueryHistorySettings) -> None:
        self._path = path
        self._settings = settings

    def fetch(
        self, *, since: datetime | None, until: datetime | None
    ) -> Iterator[QueryHistoryRecord]:
        columns = self._settings.columns
        states = {s.upper() for s in self._settings.success_states}
        window_key = _window_field(self._settings)
        with self._path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                state = row.get(columns.state)
                if state is not None and str(state).upper() not in states:
                    continue
                started = parse_timestamp(row.get(columns.started_at))
                if started is None:
                    continue
                ended = parse_timestamp(row.get(columns.ended_at)) if columns.ended_at else None
                window_value = parse_timestamp(row.get(window_key)) or started
                if since is not None and window_value <= since:
                    continue
                if until is not None and window_value > until:
                    continue
                yield QueryHistoryRecord(
                    query_id=str(row.get(columns.query_id, "")),
                    query_text=str(row.get(columns.query_text, "")),
                    user=_optional_str(row.get(columns.user)),
                    state=_optional_str(state),
                    started_at=started,
                    ended_at=ended,
                    catalog=_optional_str(row.get(columns.catalog)) if columns.catalog else None,
                    schema=_optional_str(row.get(columns.schema_)) if columns.schema_ else None,
                )


# ---------------------------------------------------------------------------
# Starburst implementation
# ---------------------------------------------------------------------------


class StarburstQueryHistorySource:
    """Reads the configured history table through the whitelisted builder client.

    TODO(confirm): table name, column names, timestamp timezone, retention, and
    whether rows appear only once a query has finished (assumed: yes).
    """

    def __init__(
        self, client: BuilderClient, settings: QueryHistorySettings, *, dialect: str = "trino"
    ) -> None:
        if not settings.table:
            msg = "query_history.table must be configured for the starburst source"
            raise ValueError(msg)
        self._client = client
        self._settings = settings
        self._table: str = settings.table
        self._dialect = dialect

    def build_query(self, *, since: datetime | None, until: datetime | None) -> str:
        columns = self._settings.columns
        selected: list[tuple[str, str]] = [
            ("query_id", columns.query_id),
            ("query_text", columns.query_text),
            ("user", columns.user),
            ("state", columns.state),
            ("started_at", columns.started_at),
        ]
        for alias, name in (
            ("ended_at", columns.ended_at),
            ("catalog", columns.catalog),
            ("schema", columns.schema_),
        ):
            if name:
                selected.append((alias, name))
        query = exp.select(
            *[exp.alias_(exp.column(name), alias) for alias, name in selected]
        ).from_(exp.to_table(self._table, dialect=self._dialect))
        conditions: list[exp.Expr] = [
            exp.In(
                this=exp.column(columns.state),
                expressions=[exp.Literal.string(s) for s in self._settings.success_states],
            )
        ]
        window = exp.column(_window_field(self._settings))
        if since is not None:
            conditions.append(exp.GT(this=window, expression=_timestamp_literal(since)))
        if until is not None:
            conditions.append(exp.LTE(this=window, expression=_timestamp_literal(until)))
        query = query.where(exp.and_(*conditions)).order_by(window.copy())
        return query.sql(dialect=self._dialect, identify=True)

    def fetch(
        self, *, since: datetime | None, until: datetime | None
    ) -> Iterator[QueryHistoryRecord]:
        # TODO(confirm): history volume; fetching a 90-day window in one statement may
        # need day-by-day batching once real numbers are known.
        result = self._client.query(self.build_query(since=since, until=until))
        for raw in result.rows:
            row = dict(zip(result.columns, raw, strict=True))
            started = parse_timestamp(row.get("started_at"))
            if started is None:
                continue
            yield QueryHistoryRecord(
                query_id=str(row.get("query_id", "")),
                query_text=str(row.get("query_text", "")),
                user=_optional_str(row.get("user")),
                state=_optional_str(row.get("state")),
                started_at=started,
                ended_at=parse_timestamp(row.get("ended_at")),
                catalog=_optional_str(row.get("catalog")),
                schema=_optional_str(row.get("schema")),
            )


def _timestamp_literal(value: datetime) -> exp.Expr:
    text = value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return exp.cast(exp.Literal.string(text), "timestamp(3)")


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
