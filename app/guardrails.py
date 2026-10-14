import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope
from .database import ALLOWED_SCHEMA


class SQLRejected(ValueError):
    pass


class SQLWriteRejected(SQLRejected):
    """A parsed statement was rejected because it is not a read-only SELECT."""


def validate_sql(sql: str, schema: dict | None = None) -> str:
    if not sql or len(sql) > 8000:
        raise SQLRejected("Generated SQL is empty or too long.")
    try:
        statements = sqlglot.parse(sql, read="sqlite")
    except sqlglot.errors.ParseError as exc:
        raise SQLRejected("Generated SQL could not be parsed.") from exc
    if len(statements) != 1 or statements[0] is None:
        raise SQLRejected("Only one SQL statement is allowed.")
    tree = statements[0]
    if not isinstance(tree, exp.Select):
        raise SQLWriteRejected("Only read-only SELECT queries are allowed.")
    for node in tree.walk():
        if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Command, exp.Transaction, exp.Merge, exp.Pragma)):
            raise SQLRejected("This SQL operation is not allowed.")

    allowed_schema = schema.get("tables", {}) if schema else ALLOWED_SCHEMA
    allowed_views = schema.get("views", {}) if schema else {}
    # Canonical introspection represents columns as metadata objects. Views
    # are a separate namespace so approved views can be distinguished from
    # physical tables at validation and execution time.
    columns_by_table = {
        table.casefold(): {name.casefold() for name in (meta.get("columns", meta) if isinstance(meta, dict) else {})}
        for table, meta in allowed_schema.items()
    }
    columns_by_view = {
        view.casefold(): {name.casefold() for name in meta.get("columns", {})}
        for view, meta in allowed_views.items()
    }
    scopes = list(traverse_scope(tree))
    if not scopes:
        raise SQLRejected("Query references a table that is not available.")

    def source_columns(source: exp.Table | Scope) -> set[str]:
        if isinstance(source, exp.Table):
            table_name = source.name.casefold()
            if source.db or source.catalog:
                raise SQLRejected("Query references a database object that is not available.")
            if table_name in columns_by_table:
                return columns_by_table[table_name]
            if table_name in columns_by_view:
                return columns_by_view[table_name]
            else:
                raise SQLRejected("Query references a table that is not available.")
        return output_columns(source)

    def output_columns(scope: Scope) -> set[str]:
        """Names visible when a CTE or derived table is referenced by its parent."""
        output: set[str] = set()
        for projection in getattr(scope.expression, "selects", []):
            if isinstance(projection, exp.Star) or isinstance(projection, exp.Column) and projection.is_star:
                qualifier = projection.table.casefold() if isinstance(projection, exp.Column) and projection.table else None
                sources = scope.sources.items()
                for alias, source in sources:
                    if qualifier is None or alias.casefold() == qualifier:
                        output.update(source_columns(source))
            else:
                name = projection.alias_or_name
                if name and name != "*":
                    output.add(name.casefold())
        return output

    def find_source(scope: Scope, alias: str) -> exp.Table | Scope | None:
        current: Scope | None = scope
        while current is not None:
            for source_name, source in current.sources.items():
                if source_name.casefold() == alias.casefold():
                    return source
            current = current.parent
        return None

    # SQLGlot scopes distinguish physical tables from CTEs/derived tables and
    # keep aliases local to each SELECT, avoiding false positives from global
    # alias maps and from projection aliases in ORDER BY/GROUP BY.
    for scope in scopes:
        for source in scope.sources.values():
            if isinstance(source, exp.Table):
                source_columns(source)  # Reject physical tables outside the allowlist.

        projection_aliases = {
            projection.alias.casefold()
            for projection in getattr(scope.expression, "selects", [])
            if isinstance(projection, exp.Alias)
        }
        for col in scope.columns:
            if col.is_star:
                continue
            if col.db or col.catalog:
                raise SQLRejected("Query references a column that is not available.")

            # SQLite permits SELECT aliases in ORDER BY and GROUP BY. They are
            # expressions from the projection, not database column names.
            if not col.table and col.name.casefold() in projection_aliases and isinstance(col.parent, (exp.Ordered, exp.Group, exp.Having)):
                continue

            if col.table:
                source = find_source(scope, col.table)
                if source is None or col.name.casefold() not in source_columns(source):
                    raise SQLRejected("Query references a column that is not available.")
            else:
                # Resolve an unqualified name in the nearest query scope where
                # it exists, and reject ambiguity instead of guessing.
                current: Scope | None = scope
                resolved = False
                while current is not None:
                    matches = [source for source in current.sources.values() if col.name.casefold() in source_columns(source)]
                    if matches:
                        if len(matches) != 1:
                            raise SQLRejected("Query references an ambiguous column.")
                        resolved = True
                        break
                    current = current.parent
                if not resolved:
                    raise SQLRejected("Query references a column that is not available.")

    # A table-free constant SELECT (such as the model's safe refusal result)
    # has no names to validate and cannot read application or system tables.
    return tree.sql(dialect="sqlite")

