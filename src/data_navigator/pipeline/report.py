"""Human-readable report of what the catalog currently knows."""

from __future__ import annotations

from collections import defaultdict

from data_navigator.catalog.repository import CatalogRepository


def render_report(repo: CatalogRepository, *, top: int = 20) -> str:
    lines: list[str] = ["# Data Navigator catalog report", ""]

    runs = repo.list_build_runs()
    if runs:
        last_run = runs[-1]
        started = last_run.started_at.isoformat()
        changes = f"{last_run.tables_added}/{last_run.tables_changed}/{last_run.tables_removed}"
        counts = (
            f"- queries processed: {last_run.queries_processed}, files processed: "
            f"{last_run.files_processed}, parse failures: {last_run.parse_failures}"
        )
        lines += [
            "## Last run",
            "",
            f"- type: {last_run.run_type}, status: {last_run.status}, started: {started}",
            f"- tables added/changed/removed: {changes}",
            counts,
            "",
        ]

    tables = repo.list_tables()
    lines += ["## Tables by usage", ""]
    lines.append(
        "| table | owner | uses | users | teams | last queried | join keys | filter columns |"
    )
    lines.append("| --- | --- | ---: | ---: | ---: | --- | --- | --- |")
    for table in sorted(tables, key=lambda t: (-t.use_count, t.full_name))[:top]:
        owner = f"{table.owner_team} ({table.owner_source})" if table.owner_team else "-"
        last_queried = table.last_queried_at.date().isoformat() if table.last_queried_at else "-"
        join_keys = ", ".join(c.name for c in table.columns if c.is_join_key) or "-"
        filters = ", ".join(c.name for c in table.columns if c.is_filter) or "-"
        lines.append(
            f"| {table.full_name} | {owner} | {table.use_count} | {table.user_count} | "
            f"{table.team_count} | {last_queried} | {join_keys} | {filters} |"
        )
    unused = [t.full_name for t in tables if t.use_count == 0]
    if unused:
        lines += ["", f"Never referenced: {', '.join(unused)}"]
    lines.append("")

    edges = repo.list_join_edges()
    lines += ["## Join graph (top edges by score)", ""]
    lines.append("| left | right | type | occurrences | score | evidence |")
    lines.append("| --- | --- | --- | ---: | ---: | --- |")
    for edge in edges[:top]:
        evidence = ", ".join(f"{k}={v}" for k, v in sorted(edge.sources.items())) or "-"
        lines.append(
            f"| {edge.left_table}.{edge.left_column} | {edge.right_table}.{edge.right_column} | "
            f"{edge.join_type} | {edge.occurrence_count} | {edge.score:g} | {evidence} |"
        )
    lines.append("")

    lineage = repo.list_lineage_edges()
    lines += ["## Lineage (from repository .sql files)", ""]
    by_downstream: dict[str, list[str]] = defaultdict(list)
    for lineage_edge in lineage:
        by_downstream[lineage_edge.downstream_table].append(
            f"{lineage_edge.upstream_table} "
            f"({lineage_edge.statement_type}, {lineage_edge.source_path})"
        )
    for downstream, upstreams in sorted(by_downstream.items()):
        lines.append(f"- {downstream} <- " + "; ".join(sorted(upstreams)))
    if not lineage:
        lines.append("- none")
    lines.append("")

    templates = repo.list_templates()
    lines += ["## Query patterns", ""]
    lines.append(
        f"- {len(templates)} distinct patterns; "
        f"{sum(1 for t in templates if t.file_paths)} from files, "
        f"{sum(1 for t in templates if t.execution_count > 0)} from history; "
        f"parse failures recorded: {repo.count_parse_failures()}"
    )
    lines.append("")
    return "\n".join(lines)
