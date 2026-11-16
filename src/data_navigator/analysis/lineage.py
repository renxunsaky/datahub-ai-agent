"""Table-level lineage from write statements in the repository's ``.sql`` files."""

from __future__ import annotations

from collections.abc import Iterable

from data_navigator.catalog.records import LineageEdgeRecord, TemplateRecord
from data_navigator.sqlproc.parse import ParsedQuery


def compute_lineage(templates: Iterable[TemplateRecord]) -> list[LineageEdgeRecord]:
    """``upstream -> downstream`` edges for every INSERT/CTAS/MERGE found in a file."""
    edges: set[LineageEdgeRecord] = set()
    for template in templates:
        if not template.file_paths:
            continue
        parsed = ParsedQuery.from_dict(template.parsed)
        if not parsed.tables_written:
            continue
        for downstream in parsed.tables_written:
            for upstream in parsed.tables_read:
                if upstream == downstream:
                    continue
                for path in template.file_paths:
                    edges.add(
                        LineageEdgeRecord(
                            upstream_table=upstream,
                            downstream_table=downstream,
                            source_path=path,
                            statement_type=parsed.statement_type,
                        )
                    )
    return sorted(edges, key=lambda e: (e.downstream_table, e.upstream_table, e.source_path))
