"""Structural analysis of a normalised statement.

Given a normalised statement and the catalog schema, ``parse`` qualifies every
column with sqlglot's optimizer and extracts:

* tables read and written (fully qualified, lower case);
* the columns used per table;
* join edges (``left.col = right.col`` in ``ON``/``USING``/``WHERE``/``MERGE ON``);
* filter columns (``WHERE`` and non-join ``ON`` predicates);
* ``GROUP BY`` columns;
* aggregate expressions with physical column references.

Partially qualified names are resolved against the known catalog tables
(unique suffix match) or the statement's default catalog/schema.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlglot import exp
from sqlglot.errors import OptimizeError, SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope, traverse_scope
from sqlglot.schema import MappingSchema

from data_navigator.sqlproc.normalize import NormalizedSQL

log = logging.getLogger(__name__)

SchemaMapping = Mapping[str, Mapping[str, Mapping[str, Mapping[str, str]]]]

_MAX_RESOLVE_DEPTH = 8


@dataclass(frozen=True, order=True)
class JoinRef:
    left_table: str
    left_column: str
    right_table: str
    right_column: str
    join_type: str

    @classmethod
    def canonical(cls, left: tuple[str, str], right: tuple[str, str], join_type: str) -> JoinRef:
        """Order endpoints deterministically, flipping LEFT/RIGHT when swapped."""
        if right < left:
            left, right = right, left
            join_type = {"LEFT": "RIGHT", "RIGHT": "LEFT"}.get(join_type, join_type)
        return cls(left[0], left[1], right[0], right[1], join_type)


@dataclass
class ParsedQuery:
    statement_type: str
    tables_read: list[str] = field(default_factory=list)
    tables_written: list[str] = field(default_factory=list)
    columns_by_table: dict[str, list[str]] = field(default_factory=dict)
    join_edges: list[JoinRef] = field(default_factory=list)
    filter_columns: list[tuple[str, str]] = field(default_factory=list)
    group_by_columns: list[tuple[str, str]] = field(default_factory=list)
    aggregates: list[str] = field(default_factory=list)
    unresolved_tables: list[str] = field(default_factory=list)
    qualified: bool = True

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["join_edges"] = [asdict(edge) for edge in self.join_edges]
        data["filter_columns"] = [list(item) for item in self.filter_columns]
        data["group_by_columns"] = [list(item) for item in self.group_by_columns]
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ParsedQuery:
        return cls(
            statement_type=str(data.get("statement_type", "select")),
            tables_read=list(data.get("tables_read", [])),
            tables_written=list(data.get("tables_written", [])),
            columns_by_table={
                k: list(v) for k, v in dict(data.get("columns_by_table", {})).items()
            },
            join_edges=[JoinRef(**edge) for edge in data.get("join_edges", [])],
            filter_columns=[(str(t), str(c)) for t, c in data.get("filter_columns", [])],
            group_by_columns=[(str(t), str(c)) for t, c in data.get("group_by_columns", [])],
            aggregates=list(data.get("aggregates", [])),
            unresolved_tables=list(data.get("unresolved_tables", [])),
            qualified=bool(data.get("qualified", True)),
        )


class TableResolver:
    """Resolves (possibly partial) table references to ``catalog.schema.table``."""

    def __init__(
        self,
        known_tables: Iterable[str],
        *,
        default_catalog: str | None = None,
        default_schema: str | None = None,
        placeholder: str | None = None,
    ) -> None:
        self._known = {name.lower() for name in known_tables}
        self._by_suffix2: dict[str, set[str]] = {}
        self._by_suffix1: dict[str, set[str]] = {}
        for name in self._known:
            parts = name.split(".")
            if len(parts) != 3:
                continue
            self._by_suffix2.setdefault(f"{parts[1]}.{parts[2]}", set()).add(name)
            self._by_suffix1.setdefault(parts[2], set()).add(name)
        self._default_catalog = default_catalog.lower() if default_catalog else None
        self._default_schema = default_schema.lower() if default_schema else None
        self._placeholder = placeholder.lower() if placeholder else None

    def with_defaults(self, catalog: str | None, schema: str | None) -> TableResolver:
        """A resolver with per-statement defaults (e.g. the session catalog/schema)."""
        clone = TableResolver.__new__(TableResolver)
        clone.__dict__.update(self.__dict__)
        clone._default_catalog = catalog.lower() if catalog else self._default_catalog
        clone._default_schema = schema.lower() if schema else self._default_schema
        return clone

    def is_placeholder(self, table: exp.Table) -> bool:
        return self.is_placeholder_name(table.name)

    def is_placeholder_name(self, name: str) -> bool:
        return self._placeholder is not None and name.lower() == self._placeholder

    def resolve(self, table: exp.Table) -> str | None:
        catalog = table.catalog.lower()
        schema = table.db.lower()
        name = table.name.lower()
        if not name or self.is_placeholder(table):
            return None
        if catalog and schema:
            return f"{catalog}.{schema}.{name}"
        if schema:
            candidates = self._by_suffix2.get(f"{schema}.{name}", set())
            if len(candidates) == 1:
                return next(iter(candidates))
            if self._default_catalog:
                return f"{self._default_catalog}.{schema}.{name}"
            return None
        if self._default_catalog and self._default_schema:
            return f"{self._default_catalog}.{self._default_schema}.{name}"
        candidates = self._by_suffix1.get(name, set())
        if len(candidates) == 1:
            return next(iter(candidates))
        return None


def parse(
    normalized: NormalizedSQL,
    *,
    schema: SchemaMapping,
    resolver: TableResolver,
) -> ParsedQuery:
    """Extract tables, columns, joins, filters and aggregates from *normalized*."""
    tree = normalized.expression.copy()
    result = ParsedQuery(statement_type=normalized.statement_type)
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}

    # 1. Resolve every physical table reference in place so qualification can use the schema.
    for table in list(tree.find_all(exp.Table)):
        if not isinstance(table.this, exp.Identifier):
            continue
        if not table.db and not table.catalog and table.name.lower() in cte_names:
            continue
        resolved = resolver.resolve(table)
        if resolved is None:
            if not resolver.is_placeholder(table):
                _add_unique(result.unresolved_tables, _written_name(table))
            continue
        catalog, db, name = resolved.split(".")
        table.set("catalog", exp.to_identifier(catalog))
        table.set("db", exp.to_identifier(db))
        table.set("this", exp.to_identifier(name))

    # 2. Qualify columns (best effort).
    try:
        tree = qualify(
            tree,
            schema=MappingSchema(dict(schema), dialect=normalized.dialect),
            dialect=normalized.dialect,
            validate_qualify_columns=False,
            quote_identifiers=False,
            identify=False,
        )
    except (OptimizeError, SqlglotError, RecursionError, ValueError, AttributeError) as error:
        log.debug("qualification failed (%s); continuing unqualified", type(error).__name__)
        result.qualified = False

    # 3. Write targets.
    for target in _write_targets(tree):
        resolved = _table_name(target)
        if resolved is not None:
            _add_unique(result.tables_written, resolved)

    # 4. Scopes of every embedded query.
    analyser = _Analyser(result, resolver)
    for query in _embedded_queries(tree):
        try:
            scopes = traverse_scope(query)
        except (SqlglotError, RecursionError):  # pragma: no cover - defensive
            scopes = []
        for scope in scopes:
            analyser.visit_scope(scope)
    if isinstance(tree, exp.Merge):
        analyser.visit_merge(tree)
    if isinstance(tree, exp.Delete | exp.Update):
        analyser.visit_dml_where(tree)

    result.tables_read.sort()
    result.tables_written.sort()
    result.columns_by_table = {
        table: sorted(columns) for table, columns in sorted(result.columns_by_table.items())
    }
    result.join_edges = sorted(set(result.join_edges))
    result.filter_columns = sorted(set(result.filter_columns))
    result.group_by_columns = sorted(set(result.group_by_columns))
    result.aggregates = sorted(set(result.aggregates))
    result.unresolved_tables.sort()
    return result


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _write_targets(tree: exp.Expr) -> list[exp.Table]:
    if isinstance(tree, exp.Insert | exp.Create | exp.Merge | exp.Delete | exp.Update):
        target = tree.this
        if isinstance(target, exp.Schema):  # INSERT INTO t (a, b)
            target = target.this
        if isinstance(target, exp.Table):
            return [target]
    return []


def _embedded_queries(tree: exp.Expr) -> list[exp.Expr]:
    if isinstance(tree, exp.Query):
        return [tree]
    if isinstance(tree, exp.Insert | exp.Create):
        inner = tree.expression
        return [inner] if isinstance(inner, exp.Query) else []
    if isinstance(tree, exp.Merge):
        using = tree.args.get("using")
        queries: list[exp.Expr] = []
        if isinstance(using, exp.Subquery) and isinstance(using.this, exp.Query):
            queries.append(using.this)
        return queries
    if isinstance(tree, exp.Delete | exp.Update):
        return [sub.this for sub in tree.find_all(exp.Subquery) if isinstance(sub.this, exp.Query)]
    return []


def _table_name(table: exp.Table) -> str | None:
    if not isinstance(table.this, exp.Identifier):
        return None
    if table.catalog and table.db:
        return f"{table.catalog}.{table.db}.{table.name}".lower()
    return None


def _written_name(table: exp.Table) -> str:
    parts = [p for p in (table.catalog, table.db, table.name) if p]
    return ".".join(parts).lower()


def _add_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def _join_type(join: exp.Join) -> str:
    side = (join.side or "").upper()
    kind = (join.kind or "").upper()
    if side in ("LEFT", "RIGHT", "FULL"):
        return side
    if kind in ("CROSS", "SEMI", "ANTI"):
        return kind
    return "INNER"


class _Analyser:
    def __init__(self, result: ParsedQuery, resolver: TableResolver) -> None:
        self._result = result
        self._resolver = resolver

    # -- scopes ----------------------------------------------------------------------

    def visit_scope(self, scope: Scope) -> None:
        expression = scope.expression
        for alias, source in scope.sources.items():
            if isinstance(source, exp.Table):
                name = _table_name(source)
                if name is not None:
                    _add_unique(self._result.tables_read, name)
                elif isinstance(source.this, exp.Identifier) and not self._resolver.is_placeholder(
                    source
                ):
                    _add_unique(self._result.unresolved_tables, _written_name(source))
            _ = alias
        if not isinstance(expression, exp.Select):
            return

        for column in scope.columns:
            physical = self._physical(scope, column)
            if physical is not None:
                self._result.columns_by_table.setdefault(physical[0], [])
                if physical[1] not in self._result.columns_by_table[physical[0]]:
                    self._result.columns_by_table[physical[0]].append(physical[1])

        join_columns: set[int] = set()
        for join in expression.args.get("joins") or []:
            condition = join.args.get("on")
            if condition is not None:
                right_alias = join.this.alias_or_name.lower() if join.this is not None else ""
                join_columns |= self._collect_edges(
                    scope, condition, _join_type(join), right_aliases={right_alias}
                )
        where = expression.args.get("where")
        if where is not None:
            join_columns |= self._collect_edges(scope, where.this, "INNER", right_aliases=set())
            self._collect_filters(scope, where.this, join_columns)
        for join in expression.args.get("joins") or []:
            condition = join.args.get("on")
            if condition is not None:
                self._collect_filters(scope, condition, join_columns)

        group = expression.args.get("group")
        if isinstance(group, exp.Group):
            for item in group.expressions:
                for column in item.find_all(exp.Column):
                    physical = self._physical(scope, column)
                    if physical is not None:
                        self._result.group_by_columns.append(physical)

        for agg in expression.walk():
            if (
                isinstance(agg, exp.AggFunc)
                and agg.find_ancestor(exp.Select) is expression
                and not isinstance(agg.parent, exp.Window)
            ):
                self._result.aggregates.append(self._render_aggregate(scope, agg))

    def visit_merge(self, merge: exp.Merge) -> None:
        target = merge.this
        using = merge.args.get("using")
        if not isinstance(target, exp.Table):
            return
        target_name = _table_name(target)
        if target_name is None:
            return
        sources: dict[str, Any] = {target.alias_or_name.lower(): target}
        if isinstance(using, exp.Table):
            sources[using.alias_or_name.lower()] = using
            using_name = _table_name(using)
            if using_name is not None:
                _add_unique(self._result.tables_read, using_name)
        elif isinstance(using, exp.Subquery) and isinstance(using.this, exp.Query):
            root = build_scope(using.this)
            if root is not None:
                sources[using.alias_or_name.lower()] = root
        condition = merge.args.get("on")
        if condition is None:
            return
        for eq in condition.find_all(exp.EQ):
            left, right = eq.this, eq.expression
            if isinstance(left, exp.Column) and isinstance(right, exp.Column):
                left_phys = self._physical_from(sources, left)
                right_phys = self._physical_from(sources, right)
                if left_phys and right_phys and left_phys[0] != right_phys[0]:
                    self._result.join_edges.append(
                        JoinRef.canonical(left_phys, right_phys, "MERGE")
                    )
                    for phys in (left_phys, right_phys):
                        self._result.columns_by_table.setdefault(phys[0], [])
                        if phys[1] not in self._result.columns_by_table[phys[0]]:
                            self._result.columns_by_table[phys[0]].append(phys[1])

    def visit_dml_where(self, statement: exp.Delete | exp.Update) -> None:
        target = statement.this
        if not isinstance(target, exp.Table):
            return
        target_name = _table_name(target)
        if target_name is None:
            return
        where = statement.args.get("where")
        if where is None:
            return
        for column in where.this.find_all(exp.Column):
            if column.find_ancestor(exp.Subquery) is not None:
                continue
            if not column.table or column.table.lower() == target.alias_or_name.lower():
                self._result.filter_columns.append((target_name, column.name.lower()))
                self._result.columns_by_table.setdefault(target_name, [])
                if column.name.lower() not in self._result.columns_by_table[target_name]:
                    self._result.columns_by_table[target_name].append(column.name.lower())

    # -- helpers ---------------------------------------------------------------------

    def _collect_edges(
        self, scope: Scope, condition: exp.Expr, join_type: str, *, right_aliases: set[str]
    ) -> set[int]:
        """Record column=column equalities across sources; returns ids of columns used.

        *right_aliases* names the source on the right-hand side of the JOIN so that
        ``LEFT``/``RIGHT`` keep their meaning whatever the order of the ``=`` operands.
        """
        used: set[int] = set()
        for eq in condition.find_all(exp.EQ):
            if eq.find_ancestor(exp.Select) is not scope.expression:
                continue
            left, right = eq.this, eq.expression
            if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                continue
            if not left.table or not right.table or left.table.lower() == right.table.lower():
                continue
            if left.table.lower() in right_aliases and right.table.lower() not in right_aliases:
                left, right = right, left
            left_phys = self._physical(scope, left)
            right_phys = self._physical(scope, right)
            if left_phys is None or right_phys is None or left_phys[0] == right_phys[0]:
                continue
            self._result.join_edges.append(JoinRef.canonical(left_phys, right_phys, join_type))
            used.add(id(left))
            used.add(id(right))
        return used

    def _collect_filters(self, scope: Scope, condition: exp.Expr, exclude: set[int]) -> None:
        for column in condition.find_all(exp.Column):
            if id(column) in exclude or column.find_ancestor(exp.Select) is not scope.expression:
                continue
            physical = self._physical(scope, column)
            if physical is not None:
                self._result.filter_columns.append(physical)

    def _physical(self, scope: Scope, column: exp.Column) -> tuple[str, str] | None:
        return self._physical_from(scope.sources, column)

    def _physical_from(
        self, sources: Mapping[str, Any], column: exp.Column, depth: int = 0
    ) -> tuple[str, str] | None:
        if depth > _MAX_RESOLVE_DEPTH or self._resolver.is_placeholder_name(column.name):
            return None
        alias = column.table.lower() if column.table else ""
        source = sources.get(alias) if alias else None
        if source is None and not alias:
            tables = [s for s in sources.values() if isinstance(s, exp.Table)]
            if len(sources) == 1 and tables:
                source = tables[0]
        if isinstance(source, exp.Table):
            name = _table_name(source)
            return (name, column.name.lower()) if name is not None else None
        if isinstance(source, Scope):
            inner = source.expression
            if not isinstance(inner, exp.Select):
                return None
            for projection in inner.selects:
                if projection.alias_or_name.lower() != column.name.lower():
                    continue
                target = projection.unalias()
                if isinstance(target, exp.Column):
                    return self._physical_from(source.sources, target, depth + 1)
                return None
        return None

    def _render_aggregate(self, scope: Scope, agg: exp.AggFunc) -> str:
        rendered = agg.copy()
        for column in rendered.find_all(exp.Column):
            physical = self._physical(scope, column)
            if physical is not None:
                catalog, db, table = physical[0].split(".")
                column.set("catalog", exp.to_identifier(catalog))
                column.set("db", exp.to_identifier(db))
                column.set("table", exp.to_identifier(table))
                column.set("this", exp.to_identifier(physical[1]))
        return rendered.sql(dialect="trino")
