import time
import re
import sqlite3
import uuid
from typing import TypedDict, Any

from langgraph.graph import StateGraph, END
from .config import get_settings
from .database import open_readonly
from .data_sources import resolve_database
from .guardrails import validate_sql, SQLRejected, SQLWriteRejected
from .llm import generate_sql, generate_summary
from .schemas import ResponsePlan


class State(TypedDict, total=False):
    question: str
    normalized: str
    schema: dict
    sql: str
    explanation: str
    columns: list[str]
    rows: list[dict[str, Any]]
    status: str
    error: str | None
    error_code: str | None
    summary: str
    attempts: int
    started: float
    duration_ms: float
    session_id: str
    database_id: str
    database_ref: Any
    response_plan: dict
    visualization: dict | None
    statistics: dict | None
    response_type: str
    previous_result: dict


def build_graph(model_fn=generate_sql, summary_fn=generate_summary):
    def normalize(s: State):
        q = " ".join(s["question"].split())
        if len(q) < 3:
            return {"status": "refused", "error": "Please enter a business question with at least 3 characters."}
        if is_destructive_request(q):
            message = "Request blocked: QueryPilot only supports read-only analytics. No database changes were made."
            return {"started": time.perf_counter(), "status": "refused", "error": message,
                    "summary": message, "error_code": "READ_ONLY_OPERATION_BLOCKED", "attempts": 0}
        plan = classify_intent(q)
        try:
            ref = resolve_database(s["session_id"], s.get("database_id"))
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
        return {"normalized": q, "started": time.perf_counter(), "attempts": 0, "database_ref": ref,
                "response_plan": plan.model_dump()}

    def schema(s: State): return {"schema": s["database_ref"].schema}

    def profile_summary(s: State):
        try:
            if s.get("response_plan", {}).get("summary_target") == "query_result":
                previous = s.get("previous_result")
                if not previous:
                    return {"status":"refused", "error":"There is no previous query result in this session to summarize."}
                return {"statistics": profile_query_result(previous)}
            return {"statistics": profile_database(s["database_ref"])}
        except Exception:
            return {"status": "error", "error": "The selected data could not be summarized within the available query limits."}

    def express_summary(s: State):
        response_type = "examine" if s.get("response_plan", {}).get("intent") == "examine" else "summary"
        statistics = s["statistics"]
        if response_type == "examine":
            # Examination is deterministic: profiling already computed the
            # facts, so do not spend an LLM round trip merely to restate them.
            text = format_examine_summary(statistics)
        else:
            try:
                text = summary_fn(s["normalized"], bounded_summary_evidence(statistics))
            except Exception:
                # Keep summaries useful when every configured provider fails.
                text = None
            if not isinstance(text, str) or not text.strip() or text.strip().casefold() in {"none", "null", "n/a"}:
                text = format_dataset_summary(statistics, s["database_ref"])
        return {"summary": text, "status": "success", "response_type": response_type, "rows": [], "columns": [],
                "duration_ms": round((time.perf_counter() - s["started"]) * 1000, 2)}

    def generate(s: State):
        try:
            result = model_fn(s["normalized"], s["schema"], s.get("error") if s["attempts"] else None)
            return {"sql": result.sql, "explanation": result.explanation, "error": None, "status": None}
        except Exception as exc:
            return {"status": "error", "error": safe_error(exc)}

    def validate(s: State):
        try:
            safe_sql = validate_sql(s["sql"], s["database_ref"].schema)
            # An intentionally empty SELECT is the model's schema refusal signal.
            compact_sql = re.sub(r"\s+", "", safe_sql).casefold()
            is_refusal_select = compact_sql in {"select1where0", "select1wherefalse"}
            if "cannot be answered" in s.get("explanation", "").lower() or is_refusal_select:
                return {"status": "refused", "error": "This question cannot be answered from the available database schema."}
            return {"sql": safe_sql, "error": None, "status": None}
        except SQLRejected as exc:
            if isinstance(exc, SQLWriteRejected):
                message = "Request blocked: QueryPilot only supports read-only analytics. No database changes were made."
                return {"status": "refused", "error": message, "summary": message,
                        "error_code": "READ_ONLY_OPERATION_BLOCKED"}
            return {"status": "refused", "error": str(exc)}

    def prepare_correction(s: State):
        return {"attempts": 1}

    def execute(s: State):
        settings = get_settings()
        started = time.perf_counter()
        con = None
        try:
            ref = s["database_ref"]
            con = open_readonly(ref.path, settings.query_timeout_ms)
            deadline = started + settings.query_timeout_ms / 1000
            con.set_progress_handler(lambda: 1 if time.perf_counter() > deadline else 0, 1000)
            # SQLite authorizer applies a second table/column allowlist at execution time.
            allowed = {t.casefold(): {col.casefold() for col in meta["columns"]} for t, meta in ref.schema["tables"].items()}

            allowed_views = {v.casefold(): {col.casefold() for col in meta["columns"]} for v, meta in ref.schema.get("views", {}).items()}
            def authorize(action, arg1, arg2, db, source):
                if action == sqlite3.SQLITE_READ:
                    table = (arg1 or "").casefold()
                    column = (arg2 or "").casefold()
                    if source:
                        # Reads of a view's dependencies carry its name in
                        # SQLite's source argument. Only validated views may
                        # grant such reads, and the referenced object/column
                        # must still be present in the inspected schema.
                        view = source.casefold()
                        view_meta = ref.schema.get("views", {}).get(source)
                        if view_meta is None:
                            view_meta = next((meta for name, meta in ref.schema.get("views", {}).items() if name.casefold() == view), None)
                        if view_meta is None:
                            return sqlite3.SQLITE_DENY
                        if table not in {name.casefold() for name in view_meta.get("depends_on", [])}:
                            return sqlite3.SQLITE_DENY
                        permitted = allowed.get(table, allowed_views.get(table, set()))
                    else:
                        # Top-level direct reads are permitted only from
                        # objects explicitly listed in the canonical schema.
                        permitted = allowed.get(table, allowed_views.get(table))
                        if permitted is None:
                            return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK if column in permitted or column == "" else sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_FUNCTION:
                    function = (arg2 or arg1 or "").casefold()
                    return sqlite3.SQLITE_DENY if function in {"eval", "load_extension", "readfile", "writefile"} else sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_SELECT: return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY
            con.set_authorizer(authorize)
            cur = con.execute(s["sql"])
            records = cur.fetchmany(settings.max_rows + 1)
            if len(records) > settings.max_rows:
                return {"status": "error", "error": f"Query exceeded the {settings.max_rows}-row result limit."}
            columns = [d[0] for d in cur.description or []]
            rows = [dict(row) for row in records]
            con.close()
            return {"columns": columns, "rows": rows, "status": "success", "error": None, "duration_ms": round((time.perf_counter()-started)*1000, 2)}
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "interrupted" in message: error = "Query exceeded the execution time limit."
            elif "not authorized" in message: error = "Query attempted an operation outside the permitted schema."
            else: error = "Query could not be executed against the selected database."
            return {"status": "error", "error": error}
        except Exception:
            return {"status": "error", "error": "The selected database is unavailable or the query failed."}
        finally:
            if con is not None:
                con.close()

    def format_result(s: State):
        if s.get("status") != "success": return {}
        if not s.get("rows"):
            summary = "The query ran successfully and returned no matching rows."
        else:
            samples = []
            for row in s["rows"][:3]:
                samples.append(", ".join(f"{key} = {value}" for key, value in row.items()))
            shown = "; ".join(samples)
            suffix = "" if len(s["rows"]) <= 3 else "; more results are shown below"
            summary = f"Returned {len(s['rows'])} row(s). Results: {shown}{suffix}."
        plan = s.get("response_plan", {"intent": "query"})
        visualization = build_visualization(plan, s.get("columns", []), s.get("rows", []))
        response_type = plan.get("intent", "query")
        if response_type == "comparison" and s.get("rows"):
            summary = comparison_summary(s["rows"], s["columns"])
        elif response_type == "visualization" and visualization.get("type") is None:
            summary = visualization.get("reason", summary)
        return {"summary": summary, "duration_ms": s.get("duration_ms", 0), "visualization": visualization,
                "response_type": response_type}

    graph = StateGraph(State)
    for name, fn in [("normalize", normalize), ("inspect_schema", schema), ("profile_summary", profile_summary), ("express_summary", express_summary), ("generate", generate), ("validate", validate), ("execute", execute), ("prepare_correction", prepare_correction), ("format", format_result)]: graph.add_node(name, fn)
    graph.set_entry_point("normalize")
    graph.add_conditional_edges("normalize", lambda s: "inspect_schema" if s.get("normalized") and s.get("status") is None else END, {"inspect_schema": "inspect_schema", END: END})
    graph.add_conditional_edges("inspect_schema", lambda s: "profile_summary" if s.get("response_plan", {}).get("intent") in {"summary", "examine"} else "generate", {"profile_summary":"profile_summary", "generate":"generate"})
    graph.add_conditional_edges("profile_summary", lambda s: "express_summary" if s.get("status") is None else END,
                                {"express_summary":"express_summary", END:END})
    graph.add_edge("express_summary", END)
    graph.add_conditional_edges("generate", lambda s: "validate" if s.get("sql") else END, {"validate": "validate", END: END})
    graph.add_conditional_edges("validate", lambda s: "execute" if s.get("status") is None else END, {"execute": "execute", END: END})
    graph.add_conditional_edges("execute", lambda s: "prepare_correction" if s.get("status") == "error" and s.get("attempts", 0) == 0 else "format", {"prepare_correction": "prepare_correction", "format": "format"})
    graph.add_edge("prepare_correction", "generate")
    graph.add_edge("format", END)
    compiled = graph.compile()
    return compiled


_DESTRUCTIVE_REQUEST = re.compile(
    r"^\s*(?:(?:please|can you|could you|would you|will you|i want you to|i need you to|i want to|i need to|we need to|i'd like you to)\s+)*"
    r"(?:delete|remove|erase|drop|alter|truncate|update|insert|add|modify|change|overwrite|clear|wipe|destroy|purge|create|set)\b|"
    r"^\s*(?:(?:please|can you|could you|would you|i want you to|i need you to|i want to|we need to)\s+)*(?:get rid of|wipe out|empty)\b",
    re.IGNORECASE,
)


def is_destructive_request(question: str) -> bool:
    """Catch direct write requests before a provider is called; SQL guardrails remain authoritative."""
    return bool(_DESTRUCTIVE_REQUEST.search(" ".join(question.split())))


def safe_error(exc: Exception) -> str:
    message = str(exc).lower()
    if "no model api key" in message: return "No model API key is configured. Set GEMINI_API_KEY1, NVIDIA_API_KEY, or GEMINI_API_KEY2 in .env."
    if "no gemini api key" in message: return "No Gemini API key is configured. Set GEMINI_API_KEY1 or GEMINI_API_KEY2 in .env."
    if "all configured model providers failed" in message: return "Gemini and NVIDIA could not generate a query. Check the configured keys, quotas, and model availability, then try again."
    if "all configured gemini api keys" in message: return "All configured Gemini API keys are rate limited, unavailable, or timed out. Check the keys or try again later."
    if "429" in message or "rate" in message: return "The language model is temporarily rate limited. Please try again shortly."
    if "timeout" in message or "deadline" in message: return "The language model request timed out. Please try again."
    if "404" in message or "notfound" in message: return "The configured Gemini model is unavailable. Update GEMINI_MODEL in your environment."
    return "The language model could not generate a valid query. Please try again."


def run_query(question: str, model_fn=generate_sql, session_id: str = "", database_id: str | None = None, summary_fn=generate_summary, previous_result: dict | None = None) -> dict:
    started = time.perf_counter()
    session_id = session_id or uuid.uuid4().hex
    result = build_graph(model_fn, summary_fn).invoke({"question": question, "session_id": session_id, "database_id": database_id, "previous_result": previous_result})
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result


def classify_intent(question: str) -> ResponsePlan:
    q = question.casefold()
    chart = None
    if re.search(r"\b(donut|doughnut)\b", q): chart = "donut"
    elif re.search(r"\bpie\b", q): chart = "pie"
    elif re.search(r"\b(line chart|line graph|line plot)\b", q): chart = "line"
    elif re.search(r"\b(scatter(?:\s+(?:plot|chart|graph))?)\b", q): chart = "scatter"
    elif re.search(r"\b(bar chart|bar graph|bar plot)\b", q): chart = "bar"
    explicit = chart is not None
    examine = bool(re.search(r"\b(examine|inspect)\s+(?:the\s+)?data\b|\bdata quality\b", q))
    summary = bool(re.search(r"\b(summarize|summarise|summary|overview|profile)\b", q) or re.search(r"\bdescribe\b.*\b(dataset|file|data|database|table)\b", q) or re.search(r"\bexplain\b.*\b(result|chart|answer)\b", q))
    visualization = explicit or bool(re.search(r"\b(chart|graph|plot|visuali[sz]e|visuali[sz]ation)\b|\bshow (?:me )?(?:a )?(?:trend|visualization)\b", q))
    comparison = bool(re.search(r"\b(compare|comparison|versus|\bvs\.?\b|across)\b", q) or re.search(r"\b(which|what)\b.*\b(best|most|least|highest|lowest)\b", q))
    intent = "examine" if examine else "summary" if summary else "visualization" if visualization else "comparison" if comparison else "query"
    summary_target = "query_result" if re.search(r"\b(query result|query results|these results|those results|(?:the|this|last|previous) (?:query )?results?)\b", q) else "dataset"
    return ResponsePlan(intent=intent, requested_chart=chart, explicit_chart=explicit, summary_target=summary_target)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def profile_database(ref: Any) -> dict:
    """Compute bounded, factual statistics on the selected read-only database."""
    import sqlite3
    settings = get_settings()
    con = open_readonly(ref.path, settings.query_timeout_ms)
    deadline = time.perf_counter() + settings.query_timeout_ms / 1000
    con.set_progress_handler(lambda: int(time.perf_counter() > deadline), 1000)
    profile_started = time.perf_counter()
    output: dict[str, Any] = {"tables": {}, "limitations": [], "relationships": ref.schema.get("relationships", [])[:50]}
    objects = list(ref.schema.get("tables", {}).items()) + list(ref.schema.get("views", {}).items())
    if len(objects) > 20:
        output["limitations"].append("Only the first 20 tables or views were profiled.")
        objects = objects[:20]
    try:
        for table_name, metadata in objects:
            table_sql = _quote_identifier(table_name)
            count = con.execute(f"SELECT COUNT(*) FROM {table_sql}").fetchone()[0]
            table_info = {"row_count": count, "column_count": len(metadata["columns"]),
                          "kind": "view" if table_name in ref.schema.get("views", {}) else "table", "columns": {}, "duplicate_row_count": None}
            if table_info["kind"] == "table":
                table_info["duplicate_row_count"] = 0
                if count:
                    duplicate_columns = ", ".join(_quote_identifier(column) for column in metadata["columns"])
                    duplicate_count = con.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM {table_sql} GROUP BY {duplicate_columns} HAVING COUNT(*) > 1)").fetchone()[0]
                    table_info["duplicate_row_count"] = duplicate_count
            for column_name, column in list(metadata["columns"].items())[:40]:
                column_sql = _quote_identifier(column_name)
                declared_type = str(column.get("type", "")).upper()
                stats = {"type": declared_type, "nullable": column.get("nullable", True),
                         "primary_key": column.get("primary_key", False), "missing_count": con.execute(
                    f"SELECT COUNT(*) - COUNT({column_sql}) FROM {table_sql}"
                ).fetchone()[0], "distinct_count": con.execute(
                    f"SELECT COUNT(DISTINCT {column_sql}) FROM {table_sql}"
                ).fetchone()[0]}
                stats["missing_percent"] = round(stats["missing_count"] * 100 / count, 2) if count else 0.0
                if any(marker in declared_type for marker in ("INT", "REAL", "NUM", "DEC", "DOUBLE", "FLOAT")):
                    row = con.execute(f"SELECT SUM({column_sql}), AVG({column_sql}), MIN({column_sql}), MAX({column_sql}) FROM {table_sql}").fetchone()
                    stats.update({"sum": row[0], "average": row[1], "minimum": row[2], "maximum": row[3]})
                elif any(token in declared_type for token in ("DATE", "TIME")) or any(token in column_name.casefold() for token in ("date", "time", "year", "month")):
                    row = con.execute(f"SELECT MIN({column_sql}), MAX({column_sql}) FROM {table_sql}").fetchone()
                    stats.update({"minimum": row[0], "maximum": row[1]})
                else:
                    # Bound value disclosure: omit top-value distributions for likely personal/free-text fields.
                    if "BLOB" not in declared_type and not any(token in column_name.casefold() for token in ("email", "name", "note", "address", "phone", "description", "message")) and stats["distinct_count"] <= 50:
                        top_values = con.execute(
                            f"SELECT {column_sql}, COUNT(*) FROM {table_sql} WHERE {column_sql} IS NOT NULL GROUP BY {column_sql} ORDER BY COUNT(*) DESC LIMIT 5"
                        ).fetchall()
                        stats["top_values"] = [{"value": (row[0][:120] + "…") if isinstance(row[0], str) and len(row[0]) > 120 else row[0], "count": row[1]} for row in top_values if not isinstance(row[0], bytes)]
                table_info["columns"][column_name] = stats
            if len(metadata["columns"]) > 40:
                output["limitations"].append(f"Only 40 columns of {table_name} were profiled.")
            output["tables"][table_name] = table_info
    finally:
        con.close()
    tables = list(output["tables"].values())
    physical_tables = [table for table in tables if table["kind"] == "table"]
    views = [table for table in tables if table["kind"] == "view"]
    output["overview"] = {
        "table_count": len(physical_tables),
        "view_count": len(views),
        "row_count": sum(table["row_count"] for table in physical_tables),
        "view_row_count": sum(table["row_count"] for table in views),
        "column_count": sum(table["column_count"] for table in tables),
        "missing_value_count": sum(column["missing_count"] for table in tables for column in table["columns"].values()),
        "columns_with_missing": sum(column["missing_count"] > 0 for table in tables for column in table["columns"].values()),
        "duplicate_row_count": sum(table["duplicate_row_count"] or 0 for table in physical_tables),
    }
    output["timings_ms"] = {"profiling": round((time.perf_counter() - profile_started) * 1000, 2)}
    return output


def format_examine_summary(statistics: dict) -> str:
    """Render a short human-readable overview solely from computed profile facts."""
    overview = statistics.get("overview")
    if overview is None:
        # Query-result profiles are bounded to returned rows and have a
        # separate shape; keep their report factual as well.
        profile = statistics.get("query_result", {})
        columns = profile.get("columns", {})
        missing = sum(int(item.get("missing_count", 0)) for item in columns.values())
        return (f"Examined {profile.get('row_count', 0)} result rows across {len(columns)} columns. "
                f"Found {missing} missing values in the returned result." )
    row_word = "row" if overview["row_count"] == 1 else "rows"
    column_word = "column" if overview["column_count"] == 1 else "columns"
    duplicate_word = "duplicate row" if overview["duplicate_row_count"] == 1 else "duplicate rows"
    missing_column_word = "column" if overview["columns_with_missing"] == 1 else "columns"
    text = (f"Examined {overview['table_count']} tables and {overview['view_count']} views: "
            f"{overview['row_count']} {row_word} in tables and {overview['view_row_count']} rows in views, "
            f"across {overview['column_count']} {column_word}. "
            f"Found {overview['missing_value_count']} missing values across "
            f"{overview['columns_with_missing']} {missing_column_word} with missing data, and "
            f"{overview['duplicate_row_count']} {duplicate_word} in physical tables.")
    if statistics.get("limitations"):
        text += " Some profiling was limited: " + " ".join(statistics["limitations"])
    return text


def format_dataset_summary(statistics: dict, ref: Any) -> str:
    """Create a factual, readable fallback from profile facts and schema names."""
    if "query_result" in statistics:
        result = statistics["query_result"]
        columns = result.get("columns", {})
        descriptions = []
        for name, stats in list(columns.items())[:12]:
            description = f"{name}"
            if stats.get("sum") is not None:
                description += f" (average {stats.get('average')}, range {stats.get('minimum')} to {stats.get('maximum')})"
            if stats.get("missing_count"):
                description += f" ({stats['missing_count']} missing)"
            descriptions.append(description)
        text = f"The previous query result contains {result.get('row_count', 0)} rows and {len(columns)} columns."
        if descriptions:
            text += " Columns: " + "; ".join(descriptions) + "."
        if statistics.get("limitations"):
            text += " " + " ".join(statistics["limitations"])
        return text
    filename = ref.schema.get("original_filename") or ref.name
    overview = statistics.get("overview", {})
    objects = list(statistics.get("tables", {}).items())
    tables = [item for item in objects if item[1].get("kind") == "table"]
    views = [item for item in objects if item[1].get("kind") == "view"]
    if not objects:
        return f"Dataset {filename} was opened, but it contains no tables or views to summarize."
    parts = [f"Dataset {filename} contains {len(tables)} tables and {len(views)} views, with "
             f"{overview.get('row_count', 0)} table rows and {overview.get('view_row_count', 0)} view rows "
             f"across {overview.get('column_count', 0)} columns."]
    descriptions = []
    for name, table in objects[:6]:
        columns = []
        for column_name, stats in list(table.get("columns", {}).items())[:12]:
            detail = f"{column_name} ({stats.get('type') or 'type not declared'}"
            if stats.get("missing_count"):
                detail += f", {stats['missing_count']} missing"
            elif stats.get("missing_count") == 0:
                detail += ", no missing values"
            if stats.get("distinct_count") is not None:
                detail += f", {stats['distinct_count']} distinct"
            if stats.get("minimum") is not None or stats.get("maximum") is not None:
                detail += f", range {stats.get('minimum', '—')} to {stats.get('maximum', '—')}"
            elif stats.get("average") is not None:
                detail += f", average {stats['average']}"
            detail += ")"
            columns.append(detail)
        descriptions.append(f"{name} ({table.get('row_count', 0)} rows): " + ", ".join(columns))
    if descriptions:
        parts.append("Columns and profiles: " + "; ".join(descriptions) + ".")
    parts.append(f"Profiling found {overview.get('missing_value_count', 0)} missing values and "
                 f"{overview.get('duplicate_row_count', 0)} duplicate rows in physical tables.")
    if statistics.get("limitations"):
        parts.append("Limitations: " + " ".join(statistics["limitations"]))
    if len(objects) > 6:
        parts.append(f"Column details are shown for the first 6 of {len(objects)} tables and views.")
    return " ".join(parts)


def profile_query_result(result: dict) -> dict:
    """Summarize bounded rows from this session's last successful query."""
    rows = result.get("rows", [])
    columns = result.get("columns", [])
    profile = {"row_count":len(rows), "kind":"query_result", "columns":{}}
    for column in columns:
        values = [row.get(column) for row in rows]
        present = [value for value in values if value is not None]
        stats = {"missing_count":len(values)-len(present)}
        if present and all(_numeric(value) for value in present):
            stats.update({"sum":sum(present), "average":sum(present)/len(present), "minimum":min(present), "maximum":max(present)})
        else:
            stats["distinct_count"] = len(set(str(value) for value in present))
            counts: dict[str, int] = {}
            if not any(token in column.casefold() for token in ("name", "email", "note", "address", "phone", "description", "message")) and not any(isinstance(value, bytes) for value in present):
                for value in present:
                    label = str(value)[:120]
                    counts[label] = counts.get(label, 0) + 1
                stats["top_values"] = [{"value":key,"count":value} for key,value in sorted(counts.items(), key=lambda item:(-item[1],item[0]))[:5]]
        profile["columns"][column] = stats
    return {"query_result":profile, "limitations":["Statistics describe the previous query result, which is bounded by QueryPilot's result limit."]}


def bounded_summary_evidence(statistics: dict) -> dict:
    """Keep provider context small and state explicitly when profiling was sampled by schema width."""
    if "query_result" in statistics:
        result = statistics["query_result"]
        columns = list(result.get("columns", {}).items())
        limited = dict(result)
        limited["columns"] = {str(name)[:80]: value for name, value in columns[:20]}
        limitations = list(statistics.get("limitations", []))
        if len(columns) > 20: limitations.append("Only the first 20 result columns were included in the language model summary context.")
        return {"query_result":limited, "limitations":limitations}
    tables = list(statistics.get("tables", {}).items())
    bounded_tables = {}
    for table_name, table in tables[:6]:
        columns = list(table.get("columns", {}).items())
        bounded_columns = {}
        for name, stats in columns[:10]:
            value = dict(stats)
            if "top_values" in value:
                value["top_values"] = [{**item, "value":str(item["value"])[:60]} for item in value["top_values"][:2]]
            bounded_columns[str(name)[:80]] = value
        bounded_tables[str(table_name)[:80]] = {**table, "columns":bounded_columns}
    limitations = list(statistics.get("limitations", []))
    if len(tables) > 6: limitations.append("Only the first 6 tables or views were included in the language model summary context.")
    if any(len(table.get("columns", {})) > 10 for _, table in tables[:6]):
        limitations.append("Only the first 10 columns per table were included in the language model summary context.")
    return {"tables":bounded_tables, "relationships":statistics.get("relationships", [])[:20], "limitations":limitations}


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def build_visualization(plan: dict, columns: list[str], rows: list[dict]) -> dict:
    """Choose and validate a chart using only returned result columns and values."""
    requested = plan.get("requested_chart")
    base = {"type": None, "label_column": None, "x_column": None, "series_columns": [], "reason": None, "alternative": None}
    if not rows:
        base["reason"] = "No rows were returned, so there is nothing to chart."
        base["alternative"] = "Adjust the filters or choose another date range."
        return base
    allowed_types = {"bar", "line", "pie", "donut", "scatter"}
    if requested is not None and requested not in allowed_types:
        base.update(reason="The requested chart type is not supported.", alternative="Choose a bar, line, pie, donut, or scatter chart.")
        return base
    numeric = [column for column in columns if any(_numeric(row.get(column)) for row in rows)]
    category = [column for column in columns if column not in numeric]
    temporal = next((column for column in columns if re.search(r"date|time|month|year|period", column, re.I)), None)
    if requested:
        kind = requested
    elif plan.get("intent") == "visualization":
        kind = "line" if temporal else "scatter" if len(numeric) >= 2 and not category else "bar"
    elif plan.get("intent") == "comparison" or (len(rows) <= 30 and ((category and numeric) or len(numeric) >= 2)):
        kind = "line" if temporal and plan.get("intent") == "comparison" else "scatter" if not category and len(numeric) >= 2 else "bar"
    else:
        return base
    xcol = (temporal or (category[0] if category else (numeric[0] if numeric else None))) if kind == "line" else (numeric[0] if kind == "scatter" and numeric else (category[0] if category else (numeric[0] if kind == "bar" and len(numeric) > 1 else None)))
    if kind in {"pie", "donut", "bar", "line"}:
        measures = [col for col in numeric if col != xcol]
    else:
        measures = numeric[1:2]
    if kind in {"pie", "donut"}:
        if not category or not numeric:
            base.update(reason=f"A {kind} chart needs one category column and one numeric measure.", alternative="Use a bar chart if the result has categories and numeric values.")
            return base
        xcol, measures = category[0], numeric[:1]
        distinct = {row.get(xcol) for row in rows if row.get(xcol) is not None}
        if len(distinct) > 20:
            base.update(reason=f"A {kind} chart is unsuitable for {len(distinct)} categories.", alternative="Use a bar chart to compare many categories.")
            return base
        if any(_numeric(row.get(measures[0])) and row[measures[0]] < 0 for row in rows):
            base.update(reason=f"A {kind} chart cannot represent negative values reliably.", alternative="Use a bar chart that supports negative values.")
            return base
    if kind == "bar" and xcol and len({row.get(xcol) for row in rows if row.get(xcol) is not None}) > 50:
        base.update(reason="A bar chart is unsuitable for more than 50 categories.", alternative="Filter the result or use a line chart for ordered time values.")
        return base
    if kind == "scatter":
        if len(numeric) < 2:
            base.update(reason="A scatter plot needs two numeric result columns.", alternative="Use a bar chart when the result has one category and one measure.")
            return base
        xcol, measures = numeric[0], [numeric[1]]
    elif kind in {"bar", "line"} and (xcol is None or not measures):
        base.update(reason=f"A {kind} chart needs a label or x column and at least one numeric measure.", alternative="Return a category column with a numeric aggregate.")
        return base
    valid = [row for row in rows if xcol is not None and row.get(xcol) is not None and all(_numeric(row.get(m)) for m in measures)]
    if not valid:
        base.update(reason="The result has no complete numeric values for the requested chart.", alternative="Check missing values or choose a numeric measure.")
        return base
    if kind in {"pie", "donut"} and sum(row[measures[0]] for row in valid) <= 0:
        base.update(reason=f"A {kind} chart needs a positive total.", alternative="Use a bar chart to show these values.")
        return base
    base.update({"type": kind, "label_column": xcol if kind in {"bar", "pie", "donut"} else None,
                 "x_column": xcol, "series_columns": measures,
                 "omitted_rows": len(rows) - len(valid), "reason": None, "alternative": None})
    return base


def comparison_summary(rows: list[dict], columns: list[str]) -> str:
    if not rows: return "The comparison returned no matching rows."
    numeric = next((column for column in columns if any(_numeric(row.get(column)) for row in rows)), None)
    label = next((column for column in columns if column != numeric), None)
    if numeric is None or label is None: return "The comparison results are shown in the table below."
    ranked = sorted((row for row in rows if _numeric(row.get(numeric))), key=lambda row: row[numeric], reverse=True)
    if not ranked: return "The comparison has no numeric values to rank."
    best = ranked[0]
    if len(ranked) < 2: return f"{best[label]} is highest at {best[numeric]} based on the returned results."
    gap = best[numeric] - ranked[1][numeric]
    return f"{best[label]} is highest at {best[numeric]}, ahead of {ranked[1][label]} by {gap}, based on the returned results."

