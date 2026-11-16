"""Build the bounded context pack the model sees for one table.

The pack contains only catalog data: structure, usage statistics, the strongest
join edges, up to ``K`` de-identified representative query patterns, and the
de-identified write statements that populate the table.  Its SHA-256 is the
card's ``input_hash``: a card is regenerated only when the hash changes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from data_navigator.catalog.records import JoinEdgeRecord, TableState, TemplateRecord
from data_navigator.config import CardsSettings


@dataclass
class ContextPack:
    table: str
    sections: list[tuple[str, str]] = field(default_factory=list)
    truncated: bool = False

    def render(self) -> str:
        parts = []
        for name, body in self.sections:
            parts.append(f"<{name}>\n{body}\n</{name}>")
        return "\n\n".join(parts)

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.render().encode("utf-8")).hexdigest()


def build_context_pack(
    table: TableState,
    *,
    templates: Iterable[TemplateRecord],
    join_edges: Iterable[JoinEdgeRecord],
    settings: CardsSettings,
) -> ContextPack:
    pack = ContextPack(table=table.full_name)
    pack.sections.append(("table", _table_section(table)))
    pack.sections.append(("columns", _columns_section(table)))
    edges = _edges_for(table.full_name, join_edges)
    if edges:
        pack.sections.append(("join_edges", "\n".join(edges[:10])))

    relevant = [t for t in templates if _mentions(t, table.full_name)]
    reads = sorted(
        (t for t in relevant if table.full_name in t.tables_read),
        key=lambda t: (-(t.execution_count + len(t.file_paths)), t.fingerprint),
    )
    writes = sorted(
        (t for t in relevant if table.full_name in t.tables_written),
        key=lambda t: (-(len(t.file_paths)), t.fingerprint),
    )
    budget = settings.max_context_chars - len(pack.render())
    query_lines: list[str] = []
    for template in reads[: settings.max_queries_per_table]:
        line = _template_line(template)
        if len(line) > budget:
            pack.truncated = True
            break
        budget -= len(line)
        query_lines.append(line)
    if query_lines:
        pack.sections.append(("representative_queries", "\n\n".join(query_lines)))
    write_lines: list[str] = []
    for template in writes[:3]:
        line = _template_line(template)
        if len(line) > budget:
            pack.truncated = True
            break
        budget -= len(line)
        write_lines.append(line)
    if write_lines:
        pack.sections.append(("populating_statements", "\n\n".join(write_lines)))
    return pack


def _table_section(table: TableState) -> str:
    last = table.last_queried_at.date().isoformat() if table.last_queried_at else "never"
    lines = [
        f"name: {table.full_name}",
        f"type: {table.table_type or 'unknown'}",
        f"comment: {table.comment or '(none)'}",
        f"owner_team: {table.owner_team or 'unknown'} ({table.owner_source or 'n/a'})",
        f"use_count: {table.use_count}",
        f"distinct_users: {table.user_count}",
        f"distinct_teams: {table.team_count}",
        f"last_queried: {last}",
    ]
    return "\n".join(lines)


def _columns_section(table: TableState) -> str:
    lines = []
    for column in table.columns:
        flags = []
        if column.is_join_key:
            flags.append("join_key")
        if column.is_filter:
            flags.append("filter")
        flag_text = f" [{', '.join(flags)}]" if flags else ""
        comment = f" -- {column.comment}" if column.comment else ""
        lines.append(
            f"{column.name} {column.data_type} uses={column.use_count}{flag_text}{comment}"
        )
    return "\n".join(lines) or "(no columns)"


def _edges_for(table: str, edges: Iterable[JoinEdgeRecord]) -> list[str]:
    mine = [e for e in edges if table in (e.left_table, e.right_table)]
    mine.sort(key=lambda e: (-e.score, e.left_table, e.right_table))
    return [
        f"{e.left_table}.{e.left_column} = {e.right_table}.{e.right_column} "
        f"({e.join_type}, score={e.score:g}, seen={e.occurrence_count})"
        for e in mine
    ]


def _mentions(template: TemplateRecord, table: str) -> bool:
    return table in template.tables_read or table in template.tables_written


def _template_line(template: TemplateRecord) -> str:
    origin = (
        f"file {', '.join(template.file_paths)}"
        if template.file_paths
        else f"history x{template.execution_count}"
    )
    return f"-- {origin}; teams={', '.join(template.teams) or 'n/a'}\n{template.normalized_sql}"


def top_tables(tables: Sequence[TableState], limit: int) -> list[TableState]:
    """The ``limit`` most used live tables (ties broken by name)."""
    live = [t for t in tables if not t.is_deleted]
    live.sort(key=lambda t: (-t.use_count, t.full_name))
    return live[:limit]
