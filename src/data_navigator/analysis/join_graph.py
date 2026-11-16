"""Join graph: how tables are related in practice, weighted by evidence quality."""

from __future__ import annotations

from collections.abc import Iterable

from data_navigator.analysis.usage import template_uses
from data_navigator.catalog.records import JoinEdgeRecord, TemplateRecord
from data_navigator.config import AnalysisSettings
from data_navigator.sqlproc.parse import ParsedQuery

SOURCE_REPO_FILE = "repo_file"
SOURCE_REPEATED_QUERY = "repeated_query"
SOURCE_SINGLE_QUERY = "single_query"


def evidence_kind(template: TemplateRecord, settings: AnalysisSettings) -> str:
    """Classify a pattern: committed job > repeatedly executed query > one-off query."""
    if template.file_paths:
        return SOURCE_REPO_FILE
    if template.execution_count >= settings.repeated_query_threshold:
        return SOURCE_REPEATED_QUERY
    return SOURCE_SINGLE_QUERY


def evidence_weight(kind: str, settings: AnalysisSettings) -> float:
    return {
        SOURCE_REPO_FILE: settings.weight_repo_file,
        SOURCE_REPEATED_QUERY: settings.weight_repeated_query,
        SOURCE_SINGLE_QUERY: settings.weight_single_query,
    }[kind]


def compute_join_graph(
    templates: Iterable[TemplateRecord], settings: AnalysisSettings
) -> list[JoinEdgeRecord]:
    """Aggregate join edges over all patterns.

    ``occurrence_count`` sums executions (plus one per file); ``score`` adds the
    evidence weight once per distinct pattern, so a daily job counts as one strong
    pattern rather than hundreds of weak ones.
    """
    edges: dict[tuple[str, str, str, str, str], JoinEdgeRecord] = {}
    for template in templates:
        parsed = ParsedQuery.from_dict(template.parsed)
        if not parsed.join_edges:
            continue
        kind = evidence_kind(template, settings)
        weight = evidence_weight(kind, settings)
        uses = template_uses(template)
        for edge in parsed.join_edges:
            key = (
                edge.left_table,
                edge.left_column,
                edge.right_table,
                edge.right_column,
                edge.join_type,
            )
            record = edges.get(key)
            if record is None:
                record = JoinEdgeRecord(*key)
                edges[key] = record
            record.occurrence_count += uses
            record.score += weight
            record.sources[kind] = record.sources.get(kind, 0) + 1
    return sorted(
        edges.values(),
        key=lambda e: (
            -e.score,
            -e.occurrence_count,
            e.left_table,
            e.left_column,
            e.right_table,
            e.right_column,
        ),
    )
