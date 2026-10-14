import time
import re
import uuid
from typing import TypedDict, Any
from langgraph.graph import StateGraph, END
from .config import get_settings
from .database import open_readonly
from .data_sources import resolve_database
from .guardrails import validate_sql, SQLRejected, SQLWriteRejected
from .llm import generate_sql


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


def build_graph(model_fn=generate_sql):
    def normalize(s: State):
        q = " ".join(s["question"].split())
        if len(q) < 3:
            return {"status": "refused", "error": "Please enter a business question with at least 3 characters."}
        if is_destructive_request(q):
            message = "Request blocked: QueryPilot only supports read-only analytics. No database changes were made."
            return {"started": time.perf_counter(), "status": "refused", "error": message,
                    "summary": message, "error_code": "READ_ONLY_OPERATION_BLOCKED", "attempts": 0}
        try:
            ref = resolve_database(s["session_id"], s.get("database_id"))
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
        return {"normalized": q, "started": time.perf_counter(), "attempts": 0, "database_ref": ref}

    def schema(s: State): return {"schema": s["database_ref"].schema}

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
            import sqlite3
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
        return {"summary": summary, "duration_ms": s.get("duration_ms", 0)}

    graph = StateGraph(State)
    for name, fn in [("normalize", normalize), ("inspect_schema", schema), ("generate", generate), ("validate", validate), ("execute", execute), ("prepare_correction", prepare_correction), ("format", format_result)]: graph.add_node(name, fn)
    graph.set_entry_point("normalize")
    graph.add_conditional_edges("normalize", lambda s: "inspect_schema" if s.get("normalized") and s.get("status") is None else END, {"inspect_schema": "inspect_schema", END: END})
    graph.add_edge("inspect_schema", "generate")
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


def run_query(question: str, model_fn=generate_sql, session_id: str = "", database_id: str | None = None) -> dict:
    started = time.perf_counter()
    session_id = session_id or uuid.uuid4().hex
    result = build_graph(model_fn).invoke({"question": question, "session_id": session_id, "database_id": database_id})
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result

