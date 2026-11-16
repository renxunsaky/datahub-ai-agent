"""Usage statistics per table and column."""

from __future__ import annotations

from collections.abc import Iterable

from data_navigator.catalog.records import ColumnUsage, TableUsage, TemplateRecord
from data_navigator.sqlproc.parse import ParsedQuery


def template_uses(template: TemplateRecord) -> int:
    """How many times a pattern was used: one per execution plus one per file that contains it."""
    return max(template.execution_count, 0) + len(template.file_paths)


def compute_usage(
    templates: Iterable[TemplateRecord],
    *,
    join_key_min_count: int = 2,
    filter_min_count: int = 2,
) -> dict[str, TableUsage]:
    """Aggregate executions, distinct users/teams and column roles per table."""
    usages: dict[str, TableUsage] = {}
    users: dict[str, set[str]] = {}
    teams: dict[str, set[str]] = {}

    def table_usage(name: str) -> TableUsage:
        if name not in usages:
            usages[name] = TableUsage(full_name=name)
            users[name] = set()
            teams[name] = set()
        return usages[name]

    for template in templates:
        parsed = ParsedQuery.from_dict(template.parsed)
        uses = template_uses(template)
        for name in set(parsed.tables_read) | set(parsed.tables_written):
            usage = table_usage(name)
            usage.use_count += uses
            users[name].update(template.users)
            teams[name].update(template.teams)
            if template.last_executed_at is not None and (
                usage.last_queried_at is None or template.last_executed_at > usage.last_queried_at
            ):
                usage.last_queried_at = template.last_executed_at
        for name, columns in parsed.columns_by_table.items():
            usage = table_usage(name)
            for column in columns:
                usage.columns.setdefault(column, ColumnUsage()).use_count += uses
        for edge in parsed.join_edges:
            for name, column in (
                (edge.left_table, edge.left_column),
                (edge.right_table, edge.right_column),
            ):
                stats = table_usage(name).columns.setdefault(column, ColumnUsage())
                stats.join_key_count += uses
        for name, column in parsed.filter_columns:
            stats = table_usage(name).columns.setdefault(column, ColumnUsage())
            stats.filter_count += uses

    for name, usage in usages.items():
        usage.user_count = len(users[name])
        usage.team_count = len(teams[name])
        for stats in usage.columns.values():
            stats.is_join_key = stats.join_key_count >= join_key_min_count
            stats.is_filter = stats.filter_count >= filter_min_count
    return usages
