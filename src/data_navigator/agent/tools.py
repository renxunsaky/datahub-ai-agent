"""Tools the model may call.

The user's identity never appears in a tool argument: the end-user client is
bound to it when the session is created (closure), so the model cannot act as
someone else.  Every tool returns a JSON-serialisable mapping; errors are
returned as ``{"error": ...}`` text rather than raised.

Visibility rules (section 3.9 of the task description):

* everyone sees table names, owning teams and one-line purposes;
* column details, card details and similar query patterns are returned only
  when ``check_access`` says the user may read the table;
* SQL constants and other teams' raw SQL never exist in the catalog.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp

from data_navigator.agent.state import sql_hash
from data_navigator.catalog.records import CardRecord, JoinEdgeRecord, TableState, TemplateRecord
from data_navigator.catalog.repository import CatalogRepository
from data_navigator.config import AgentSettings, SampleSettings
from data_navigator.search.documents import (
    TABLE_PREFIX,
    TEMPLATE_PREFIX,
    fingerprint_from_doc_id,
    table_name_from_doc_id,
)
from data_navigator.search.hybrid import HybridSearch
from data_navigator.sqlproc.readonly_guard import readonly_guard
from data_navigator.starburst.end_user_client import EndUserClient, SampleResult
from data_navigator.starburst.errors import AccessDecision, access_decision

log = logging.getLogger(__name__)

ToolResult = dict[str, Any]

FINAL_ANSWER = "final_answer"
RUN_SAMPLE = "run_sample"


def _spec(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


TOOL_SPECS: list[dict[str, Any]] = [
    _spec(
        "search_tables",
        "Find catalog tables matching a natural-language description. Returns table names, "
        "owning teams and one-line purposes.",
        {
            "query": {"type": "string", "description": "What the data should contain."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        ["query"],
    ),
    _spec(
        "get_table_card",
        "Read the documentation card of one table (columns, key column meanings, common usage, "
        "example questions). Details are only returned when the user may read the table.",
        {"table": {"type": "string", "description": "catalog.schema.table"}},
        ["table"],
    ),
    _spec(
        "get_join_path",
        "How the given tables are related according to observed joins. Returns the join edges "
        "(columns and join type) that connect them, and the pairs with no known path.",
        {"tables": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
        ["tables"],
    ),
    _spec(
        "similar_queries",
        "Existing de-identified query patterns similar to the question, restricted to patterns "
        "whose tables the user may all read.",
        {
            "question": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        ["question"],
    ),
    _spec(
        "validate_sql",
        "Check that a statement is a single read-only SELECT and run EXPLAIN with the user's "
        "identity. Returns ok, or the error to fix (syntax/semantic/permission).",
        {"sql": {"type": "string"}},
        ["sql"],
    ),
    _spec(
        "check_access",
        "Whether the user may read a table: allowed, denied or unknown.",
        {"table": {"type": "string"}},
        ["table"],
    ),
    _spec(
        RUN_SAMPLE,
        "Run SQL that already passed validate_sql and return a few sample rows. The user must "
        "confirm first; call this only when the user asked to see sample data.",
        {"sql": {"type": "string"}},
        ["sql"],
    ),
]

FINAL_ANSWER_SPEC: dict[str, Any] = _spec(
    FINAL_ANSWER,
    "Deliver the answer to the user. mode 'sql' requires sql that passed validate_sql; "
    "otherwise use mode 'candidates' with the tables that may fit and a clarifying question.",
    {
        "mode": {"type": "string", "enum": ["sql", "candidates"]},
        "sql": {"type": "string"},
        "tables": {"type": "array", "items": {"type": "string"}},
        "joins": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "explanation": {"type": "string"},
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                    "why": {"type": "string"},
                    "uncertainty": {"type": "string"},
                },
                "required": ["table"],
            },
        },
        "clarifying_questions": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reasons": {"type": "array", "items": {"type": "string"}},
    },
    ["mode"],
)

ALL_TOOL_SPECS: list[dict[str, Any]] = [*TOOL_SPECS, FINAL_ANSWER_SPEC]


@dataclass
class ValidationRecord:
    status: str
    message: str | None = None
    plan: str | None = None
    error_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "plan": self.plan,
            "error_name": self.error_name,
        }


@dataclass
class AgentTools:
    """Tool implementations bound to one user (identity injected, never a parameter)."""

    repository: CatalogRepository
    search: HybridSearch
    client: EndUserClient
    settings: AgentSettings
    samples: SampleSettings
    _access_cache: dict[str, AccessDecision] = field(default_factory=dict, init=False)
    _validated: dict[str, ValidationRecord] = field(default_factory=dict, init=False)
    _cards: dict[str, CardRecord] | None = field(default=None, init=False)

    # -- dispatch ----------------------------------------------------------------------

    def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Run a tool by name; every failure becomes an ``error`` entry."""
        handlers: dict[str, Callable[..., ToolResult]] = {
            "search_tables": self.search_tables,
            "get_table_card": self.get_table_card,
            "get_join_path": self.get_join_path,
            "similar_queries": self.similar_queries,
            "validate_sql": self.validate_sql,
            "check_access": self.check_access,
        }
        handler = handlers.get(name)
        if handler is None:
            return {"error": f"unknown tool {name!r}"}
        if "__invalid_json__" in arguments:
            return {"error": "tool arguments were not valid JSON"}
        try:
            return handler(**arguments)
        except TypeError as error:
            return {"error": f"invalid arguments for {name}: {error}"}
        except Exception as error:
            log.warning("tool %s failed: %s", name, type(error).__name__)
            return {"error": f"{name} failed: {type(error).__name__}"}

    # -- catalog tools -----------------------------------------------------------------

    def search_tables(self, query: str, limit: int | None = None) -> ToolResult:
        if not isinstance(query, str) or not query.strip():
            return {"error": "query must be a non-empty string"}
        limit = self._limit(limit, self.settings.search_limit)
        hits = self.search.search(query, limit=limit, prefix=TABLE_PREFIX)
        results = []
        for hit in hits:
            name = table_name_from_doc_id(hit.doc_id)
            if name is None:
                continue
            table = self.repository.get_table(name)
            if table is None or table.is_deleted:
                continue
            results.append(self._table_summary(table))
        return {"tables": results, "count": len(results)}

    def get_table_card(self, table: str) -> ToolResult:
        state = self._table(table)
        if state is None:
            return {"error": f"table {table!r} is not in the catalog"}
        summary = self._table_summary(state)
        decision = self.check_access(state.full_name)["access"]
        summary["access"] = decision
        if decision != "allowed":
            summary["note"] = (
                "column details and query patterns are hidden: the user "
                f"{'has no' if decision == 'denied' else 'may not have'} access to this table"
            )
            return summary
        card = self._card(state.full_name)
        summary["columns"] = [
            {
                "name": c.name,
                "type": c.data_type,
                "comment": c.comment,
                "ai_description": c.ai_description,
                "join_key": c.is_join_key,
                "filter": c.is_filter,
                "uses": c.use_count,
            }
            for c in state.columns
        ]
        if card is not None and not card.is_rejected:
            summary["key_columns"] = [
                {"name": k.name, "meaning": k.meaning, "confidence": k.confidence}
                for k in card.key_columns
            ]
            summary["common_usage"] = card.common_usage
            summary["example_questions"] = card.example_questions
        summary["join_edges"] = [
            _edge_dict(edge)
            for edge in sorted(
                (
                    e
                    for e in self.repository.list_join_edges()
                    if state.full_name in (e.left_table, e.right_table)
                ),
                key=lambda e: -e.score,
            )[:10]
        ]
        return summary

    def get_join_path(self, tables: Sequence[str]) -> ToolResult:
        if not isinstance(tables, list | tuple) or not tables:
            return {"error": "tables must be a non-empty list"}
        resolved: list[str] = []
        unknown: list[str] = []
        for name in tables:
            state = self._table(str(name))
            if state is None:
                unknown.append(str(name))
            elif state.full_name not in resolved:
                resolved.append(state.full_name)
        if unknown:
            return {"error": f"unknown tables: {', '.join(unknown)}"}
        if len(resolved) == 1:
            return {"edges": [], "missing": [], "note": "one table: no join needed"}
        edges = self.repository.list_join_edges()
        adjacency: dict[str, list[JoinEdgeRecord]] = {}
        for edge in edges:
            adjacency.setdefault(edge.left_table, []).append(edge)
            adjacency.setdefault(edge.right_table, []).append(edge)
        used: dict[tuple[str, str, str, str, str], JoinEdgeRecord] = {}
        missing: list[list[str]] = []
        for index, source in enumerate(resolved):
            for target in resolved[index + 1 :]:
                path = _shortest_path(adjacency, source, target)
                if path is None:
                    missing.append([source, target])
                    continue
                for edge in path:
                    key = (
                        edge.left_table,
                        edge.left_column,
                        edge.right_table,
                        edge.right_column,
                        edge.join_type,
                    )
                    used[key] = edge
        return {
            "edges": [_edge_dict(e) for e in sorted(used.values(), key=lambda e: -e.score)],
            "missing": missing,
            "note": None
            if not missing
            else "no observed join path for the listed pairs; do not guess keys",
        }

    def similar_queries(self, question: str, limit: int | None = None) -> ToolResult:
        if not isinstance(question, str) or not question.strip():
            return {"error": "question must be a non-empty string"}
        limit = self._limit(limit, self.settings.similar_queries_limit)
        hits = self.search.search(question, limit=limit * 4, prefix=TEMPLATE_PREFIX)
        wanted = {fp for h in hits if (fp := fingerprint_from_doc_id(h.doc_id))}
        if not wanted:
            return {"patterns": [], "count": 0}
        by_fingerprint = {t.fingerprint: t for t in self.repository.list_templates()}
        patterns: list[dict[str, Any]] = []
        hidden = 0
        for hit in hits:
            fingerprint = fingerprint_from_doc_id(hit.doc_id)
            template = by_fingerprint.get(fingerprint or "")
            if template is None:
                continue
            if not self._all_accessible(template):
                hidden += 1
                continue
            patterns.append(_template_dict(template))
            if len(patterns) >= limit:
                break
        return {"patterns": patterns, "count": len(patterns), "hidden_for_access": hidden}

    # -- end-user tools (identity injected) ------------------------------------------------

    def validate_sql(self, sql: str) -> ToolResult:
        if not isinstance(sql, str) or not sql.strip():
            return {"status": "rejected", "message": "sql must be a non-empty string"}
        guard = readonly_guard(sql)
        key = sql_hash(sql)
        if guard.rejected:
            record = ValidationRecord(status="rejected", message=guard.reason)
            self._validated[key] = record
            return record.to_dict()
        result = self.client.explain(sql)
        record = ValidationRecord(
            status=result.status,
            message=result.message,
            plan=_plan_excerpt(result.plan),
            error_name=result.error_name,
        )
        self._validated[key] = record
        return record.to_dict()

    def check_access(self, table: str) -> ToolResult:
        state = self._table(table)
        if state is None:
            return {"table": table, "access": "unknown", "reason": "not in the catalog"}
        decision = self._access(state.full_name)
        return {"table": state.full_name, "access": decision}

    def run_sample(self, sql: str) -> SampleResult | ToolResult:
        """Execute a validated statement with the row cap. Confirmation happens in the graph."""
        record = self.validation_of(sql)
        if record is None or record.status != "ok":
            return {
                "status": "rejected",
                "message": "run_sample only accepts SQL that passed validate_sql in this session",
            }
        return self.client.sample(sql)

    def validation_of(self, sql: str) -> ValidationRecord | None:
        return self._validated.get(sql_hash(sql))

    def candidate_info(self, name: str) -> dict[str, Any] | None:
        """Public summary of a table for candidates mode (``None`` when unknown)."""
        state = self._table(name)
        return self._table_summary(state) if state is not None else None

    @property
    def max_rows(self) -> int:
        return min(self.samples.max_rows, self.client.max_rows)

    # -- helpers ---------------------------------------------------------------------------

    def _access(self, full_name: str) -> AccessDecision:
        cached = self._access_cache.get(full_name)
        if cached is not None:
            return cached
        probe = f"SELECT * FROM {quote_table(full_name)} LIMIT 0"
        try:
            result = self.client.explain(probe)
            decision = access_decision(result.status)
        except Exception as error:  # the client maps errors, but never trust that fully
            log.warning("access probe failed for %s: %s", full_name, type(error).__name__)
            decision = "unknown"
        self._access_cache[full_name] = decision
        return decision

    def _all_accessible(self, template: TemplateRecord) -> bool:
        names = set(template.tables_read) | set(template.tables_written)
        return bool(names) and all(self._access(name) == "allowed" for name in names)

    def _table(self, name: str) -> TableState | None:
        cleaned = name.strip().strip('"').lower().replace('"', "")
        if not cleaned:
            return None
        state = self.repository.get_table(cleaned)
        return None if state is None or state.is_deleted else state

    def _card(self, full_name: str) -> CardRecord | None:
        if self._cards is None:
            self._cards = {card.table: card for card in self.repository.list_cards()}
        return self._cards.get(full_name)

    def _table_summary(self, table: TableState) -> dict[str, Any]:
        card = self._card(table.full_name)
        purpose = card.purpose if card is not None and not card.is_rejected else None
        return {
            "table": table.full_name,
            "owner_team": table.owner_team,
            "owner_source": table.owner_source,
            "purpose": purpose or table.comment,
            "purpose_source": (
                f"card ({card.review_status})"
                if purpose and card is not None
                else ("table comment" if table.comment else None)
            ),
            "type": table.table_type,
            "use_count": table.use_count,
        }

    @staticmethod
    def _limit(value: int | None, default: int) -> int:
        if value is None:
            return default
        try:
            return max(1, min(int(value), 20))
        except (TypeError, ValueError):
            return default


def quote_table(full_name: str) -> str:
    """``cos.retail_ods.account`` -> ``"cos"."retail_ods"."account"`` (Trino quoting)."""
    parts = full_name.split(".")
    if len(parts) != 3 or not all(parts):
        msg = f"not a three-part table name: {full_name!r}"
        raise ValueError(msg)
    table = exp.Table(
        this=exp.to_identifier(parts[2], quoted=True),
        db=exp.to_identifier(parts[1], quoted=True),
        catalog=exp.to_identifier(parts[0], quoted=True),
    )
    return table.sql(dialect="trino")


def _shortest_path(
    adjacency: dict[str, list[JoinEdgeRecord]], source: str, target: str
) -> list[JoinEdgeRecord] | None:
    queue: deque[str] = deque([source])
    previous: dict[str, tuple[str, JoinEdgeRecord] | None] = {source: None}
    while queue:
        node = queue.popleft()
        if node == target:
            break
        for edge in adjacency.get(node, []):
            other = edge.right_table if edge.left_table == node else edge.left_table
            if other not in previous:
                previous[other] = (node, edge)
                queue.append(other)
    if target not in previous:
        return None
    path: list[JoinEdgeRecord] = []
    node = target
    while previous[node] is not None:
        parent, edge = previous[node]  # type: ignore[misc]
        path.append(edge)
        node = parent
    path.reverse()
    return path


def _edge_dict(edge: JoinEdgeRecord) -> dict[str, Any]:
    return {
        "left": f"{edge.left_table}.{edge.left_column}",
        "right": f"{edge.right_table}.{edge.right_column}",
        "join_type": edge.join_type,
        "score": edge.score,
        "seen": edge.occurrence_count,
    }


def _template_dict(template: TemplateRecord) -> dict[str, Any]:
    return {
        "sql": template.normalized_sql,
        "tables": sorted(set(template.tables_read) | set(template.tables_written)),
        "statement_type": template.statement_type,
        "executions": template.execution_count,
        "from_repository": bool(template.file_paths),
    }


def _plan_excerpt(plan: str | None, max_lines: int = 12) -> str | None:
    if not plan:
        return None
    lines = plan.splitlines()
    excerpt = "\n".join(lines[:max_lines])
    if len(lines) > max_lines:
        excerpt += f"\n... ({len(lines) - max_lines} more lines)"
    return excerpt


def tables_in(results: Iterable[ToolResult]) -> list[str]:
    """Table names mentioned in search results (for forced candidate replies)."""
    names: list[str] = []
    for result in results:
        for item in result.get("tables", []):
            name = item.get("table") if isinstance(item, dict) else None
            if name and name not in names:
                names.append(name)
    return names
