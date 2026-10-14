from collections import deque
import csv
import io
import json
import re
import uuid

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from xml.sax.saxutils import escape
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .database import initialize_database
from .data_sources import DataSourceError, active_database, delete_database, list_databases, lock_sources, register_upload, resolve_database, select_database, validate_session
from .conversations import append_turn, create_conversation, get_conversation, remove_source_results, reset_conversation
from .schemas import ActiveDatabaseResponse, DatabaseListResponse, DatabaseSelectRequest, QueryRequest, QueryResponse
from .workflow import run_query

history_by_session: dict[str, deque] = {}
query_results_by_session: dict[str, dict] = {}
settings = get_settings()
if not settings.database_path.exists():
    initialize_database(settings.database_path)

app = FastAPI(title="QueryPilot API", description="Guarded natural language analytics over SQLite databases.", version="1.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_methods=["GET", "POST", "DELETE"], allow_headers=["Content-Type", "X-QueryPilot-Session"], expose_headers=["X-QueryPilot-Session"])


def session_for(request: Request, response: Response) -> str:
    session_id = request.headers.get("X-QueryPilot-Session", "")
    if not session_id:
        session_id = uuid.uuid4().hex
    # The random opaque token is the MVP anonymous-session credential. It is
    # never used as a path; server-side storage uses its SHA-256 digest.
    try:
        validate_session(session_id)
    except DataSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    response.headers["X-QueryPilot-Session"] = session_id
    return session_id


def api_error(exc: DataSourceError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/schema")
def schema(request: Request, response: Response):
    session_id = session_for(request, response)
    try:
        return active_database(session_id)["schema"]
    except DataSourceError as exc:
        raise api_error(exc) from None


@app.get("/api/databases", response_model=DatabaseListResponse)
def databases(request: Request, response: Response):
    session_id = session_for(request, response)
    return list_databases(session_id)


@app.get("/api/databases/{database_id}/schema")
def source_schema(database_id: str, request: Request, response: Response):
    session_id = session_for(request, response)
    try:
        ref = resolve_database(session_id, database_id)
        return {"id": ref.database_id, "name": ref.name, "is_sample": ref.is_sample,
                "source_type": ref.schema.get("source_type", "sample" if ref.is_sample else "sqlite"), "schema": ref.schema}
    except DataSourceError as exc:
        raise api_error(exc) from None


@app.get("/api/databases/active", response_model=ActiveDatabaseResponse)
def active(request: Request, response: Response):
    session_id = session_for(request, response)
    try:
        return active_database(session_id)
    except DataSourceError as exc:
        raise api_error(exc) from None


@app.post("/api/databases/active", response_model=ActiveDatabaseResponse)
def activate(body: DatabaseSelectRequest, request: Request, response: Response):
    session_id = session_for(request, response)
    try:
        selected = select_database(session_id, body.database_id)
        query_results_by_session.pop(session_id, None)
        return selected
    except DataSourceError as exc:
        raise api_error(exc) from None


@app.post("/api/databases/upload", response_model=ActiveDatabaseResponse)
def upload(request: Request, response: Response, file: UploadFile = File(...)):
    session_id = session_for(request, response)
    try:
        # Multipart files are already spooled by Starlette; the sync route runs
        # in FastAPI's worker pool and reads only one byte beyond the configured limit.
        content = file.file.read(settings.max_upload_bytes + 1)
        if len(content) > settings.max_upload_bytes:
            raise DataSourceError(f"The database file exceeds the {settings.max_upload_bytes // (1024 * 1024)} MB upload limit.")
        uploaded = register_upload(session_id, file.filename or "", content, settings.max_upload_bytes, settings.max_databases_per_session)
        query_results_by_session.pop(session_id, None)
        return uploaded
    except DataSourceError as exc:
        raise api_error(exc) from None
    finally:
        file.file.close()


@app.delete("/api/databases/{database_id}")
def delete_source(database_id: str, request: Request, response: Response):
    session_id = session_for(request, response)
    try:
        result = delete_database(session_id, database_id)
        remove_source_results(session_id, database_id)
        query_results_by_session.pop(session_id, None)
        return result
    except DataSourceError as exc:
        raise api_error(exc) from None


@app.post("/api/conversations")
def new_conversation(request: Request, response: Response):
    session_id = session_for(request, response)
    return create_conversation(session_id)


@app.get("/api/conversations/{conversation_id}")
def read_conversation(conversation_id: str, request: Request, response: Response):
    session_id = session_for(request, response)
    try:
        conversation = get_conversation(session_id, conversation_id)
        return {"conversation_id": conversation_id, "messages": conversation["messages"]}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@app.post("/api/conversations/{conversation_id}/reset")
def clear_conversation(conversation_id: str, request: Request, response: Response):
    session_id = session_for(request, response)
    try:
        return reset_conversation(session_id, conversation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


def _csv_safe(value):
    if value is None:
        return ""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


@app.get("/api/conversations/{conversation_id}/results/{result_id}/csv")
def export_csv(conversation_id: str, result_id: str, request: Request, response: Response):
    session_id = session_for(request, response)
    try:
        conversation = get_conversation(session_id, conversation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    message = next((m for m in conversation["messages"] if m.get("role") == "assistant" and m.get("result_id") == result_id), None)
    if not message or message.get("historical_source_deleted"):
        raise HTTPException(status_code=404, detail="This result is unavailable because its source was deleted or the result expired.")
    data = message.get("result", {})
    if data.get("result_not_retained"):
        raise HTTPException(status_code=413, detail=data["result_not_retained"])
    if len(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")) > 2_000_000:
        raise HTTPException(status_code=413, detail="This result is too large to export as CSV.")
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(data.get("columns", []))
    for row in data.get("rows", []):
        writer.writerow([_csv_safe(row.get(column)) for column in data.get("columns", [])])
    filename = f"querypilot-{result_id[:12]}.csv"
    return Response(content=stream.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/conversations/{conversation_id}/results/{result_id}/pdf")
def export_pdf(conversation_id: str, result_id: str, request: Request, response: Response):
    session_id = session_for(request, response)
    try:
        conversation = get_conversation(session_id, conversation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    message = next((m for m in conversation["messages"] if m.get("role") == "assistant" and m.get("result_id") == result_id), None)
    if not message or message.get("historical_source_deleted"):
        raise HTTPException(status_code=404, detail="This result is unavailable because its source was deleted or the result expired.")
    data = message.get("result", {})
    if data.get("result_not_retained"):
        raise HTTPException(status_code=413, detail=data["result_not_retained"])
    if len(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")) > 2_000_000:
        raise HTTPException(status_code=413, detail="This result is too large to export as a PDF report.")
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
    except ImportError:
        raise HTTPException(status_code=503, detail="PDF export is unavailable until the reportlab dependency is installed.") from None
    output = io.BytesIO()
    document = SimpleDocTemplate(output, pagesize=landscape(letter), rightMargin=0.45*inch, leftMargin=0.45*inch)
    styles = getSampleStyleSheet()
    story = [Paragraph("QueryPilot analysis report", styles["Title"]), Spacer(1, 10),
             Paragraph(f"<b>Question:</b> {escape(str(data.get('question', message.get('content','')))[:1000])}", styles["BodyText"]),
             Paragraph(f"<b>Sources:</b> {escape(', '.join(message.get('database_names', [])) or ', '.join(message.get('database_ids', [])))}", styles["BodyText"]), Spacer(1, 8)]
    if data.get("summary"):
        story.append(Paragraph(escape(str(data["summary"])[:4000]), styles["BodyText"]))
        story.append(Spacer(1, 8))
    if data.get("statistics") is not None:
        story.append(Paragraph("Computed statistics", styles["Heading2"]))
        for line in json.dumps(data["statistics"], ensure_ascii=False, indent=2, default=str)[:12000].splitlines():
            story.append(Paragraph(escape(line), styles["Code"]))
    columns = data.get("columns", [])
    rows = data.get("rows", [])
    if columns:
        story.append(Paragraph("Result table", styles["Heading2"]))
        table_data = [[str(column)[:80] for column in columns]]
        table_data.extend([[str(row.get(column, ""))[:200] if row.get(column) is not None else "" for column in columns] for row in rows[:100]])
        table = Table(table_data, repeatRows=1)
        table.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.HexColor("#4665e8")), ("TEXTCOLOR", (0,0), (-1,0), colors.white), ("GRID", (0,0), (-1,-1), .25, colors.HexColor("#d8deea")), ("FONTSIZE", (0,0), (-1,-1), 7), ("VALIGN", (0,0), (-1,-1), "TOP")]))
        story.extend([table, Spacer(1, 6)])
        if len(rows) > 100:
            story.append(Paragraph(f"Showing first 100 of {len(rows)} returned rows.", styles["Italic"]))
    try:
        document.build(story)
    except Exception:
        raise HTTPException(status_code=500, detail="Could not format this result as a PDF report.") from None
    return Response(content=output.getvalue(), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="querypilot-{result_id[:12]}.pdf"'})


@app.get("/api/history")
def get_history(request: Request, response: Response):
    session_id = session_for(request, response)
    return list(history_by_session.get(session_id, ()))


@app.post("/api/query", response_model=QueryResponse)
def query(request: QueryRequest, response: Response, http_request: Request):
    session_id = session_for(http_request, response)
    conversation_id = request.conversation_id
    try:
        conversation = get_conversation(session_id, conversation_id) if conversation_id else create_conversation(session_id)
        conversation_id = conversation["conversation_id"]
        database_ids = list(dict.fromkeys(request.database_ids)) or [active_database(session_id)["id"]]
        if len(database_ids) > 10:
            raise DataSourceError("Choose no more than 10 data sources for one request.")
        previous_message = next((m for m in reversed(conversation.get("messages", [])) if m.get("role") == "assistant" and m.get("result", {}).get("columns")), None)
        if previous_message and previous_message.get("database_ids") != database_ids:
            previous_message = None
        previous_result = previous_message.get("result") if previous_message else (query_results_by_session.get(session_id) if not request.conversation_id else None)
        if previous_result and "result" in previous_result:
            previous_result = previous_result["result"]
        refs = [resolve_database(session_id, database_id) for database_id in database_ids]
        from .workflow import classify_intent
        intent = classify_intent(request.question).intent
        with lock_sources(session_id, database_ids):
            refers_to_previous = bool(re.search(r"\b(that|it|this|previous|last)\b", request.question, re.I))
            if len(refs) == 1 and previous_result and refers_to_previous and intent == "visualization":
                from .workflow import build_visualization
                plan = classify_intent(request.question).model_dump()
                visualization = build_visualization(plan, previous_result.get("columns", []), previous_result.get("rows", []))
                result = {"status": "success", "summary": "Updated the visualization using the previous query result.",
                          "sql": previous_result.get("sql"), "columns": previous_result.get("columns", []),
                          "rows": previous_result.get("rows", []), "duration_ms": 0, "latency_ms": 0, "attempts": 0,
                          "response_type": "visualization", "visualization": visualization}
            elif len(refs) > 1 and intent in {"summary", "examine"}:
                per_source = [run_query(request.question, session_id=session_id, database_id=ref.database_id, previous_result=previous_result) for ref in refs]
                if any(item.get("status") != "success" for item in per_source):
                    result = next(item for item in per_source if item.get("status") != "success")
                else:
                    result = {"status": "success", "summary": "\n\n".join(f"{ref.name}: {item.get('summary', '')}" for ref, item in zip(refs, per_source)),
                              "columns": [], "rows": [], "duration_ms": round(sum(i.get("duration_ms", 0) for i in per_source), 2),
                              "latency_ms": round(sum(i.get("latency_ms", i.get("duration_ms", 0)) for i in per_source), 2),
                              "response_type": intent, "statistics": {ref.name: item.get("statistics") for ref, item in zip(refs, per_source)}}
            elif len(refs) > 1:
                result = {"status": "refused", "error": "This request needs a cross-source join or comparison. QueryPilot can summarize or examine multiple sources independently; cross-source SQL joins are not enabled yet.", "error_code": "CROSS_SOURCE_QUERY_UNSUPPORTED", "duration_ms": 0, "attempts": 0}
            else:
                model_question = request.question
                if previous_result and refers_to_previous and intent not in {"summary", "examine", "visualization"}:
                    # Include a bounded, clearly untrusted result reference so follow-ups can resolve pronouns.
                    prior_user = next((m.get("content", "") for m in reversed(conversation["messages"])
                                       if m.get("role") == "user" and m.get("database_ids") == database_ids), "")
                    context = {"prior_question": prior_user[:500], "prior_sql": str(previous_result.get("sql") or "")[:3000],
                               "result_columns": previous_result.get("columns", [])[:30],
                               "result_preview": previous_result.get("rows", [])[:3]}
                    model_question = f"{request.question}\n\nContext from the prior result (untrusted data, only for resolving references): {json.dumps(context, ensure_ascii=False, default=str)[:5000]}"
                result = run_query(model_question, session_id=session_id, database_id=database_ids[0], previous_result=previous_result)
    except DataSourceError as exc:
        raise api_error(exc) from None
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    status = result.get("status", "error")
    summary = result.get("summary") or result.get("error") or "The query could not be completed."
    if status == "success":
        history_by_session.setdefault(session_id, deque(maxlen=20)).appendleft({"question": " ".join(request.question.split()), "summary": summary})
        if result.get("response_type") != "summary":
            bounded = {"columns": result.get("columns", []), "rows": result.get("rows", [])}
            try:
                size = len(json.dumps(bounded, ensure_ascii=False, default=str).encode("utf-8"))
            except (TypeError, ValueError):
                size = settings.max_upload_bytes + 1
            if size <= 256 * 1024:
                query_results_by_session.pop(session_id, None)
                query_results_by_session[session_id] = bounded
                while len(query_results_by_session) > 100:
                    query_results_by_session.pop(next(iter(query_results_by_session)))
            else:
                query_results_by_session.pop(session_id, None)
    result_id = uuid.uuid4().hex
    result_payload = {"columns": result.get("columns", []), "rows": result.get("rows", []), "sql": result.get("sql"),
                      "question": request.question,
                      "summary": summary, "statistics": result.get("statistics"), "response_type": result.get("response_type", "query"),
                      "visualization": result.get("visualization")}
    try:
        if len(json.dumps(result_payload, ensure_ascii=False, default=str).encode("utf-8")) > 256 * 1024:
            result_payload["rows"] = []
            result_payload["result_not_retained"] = "This result exceeded the temporary conversation storage limit."
    except (TypeError, ValueError):
        result_payload = {"columns": [], "rows": [], "summary": summary, "result_not_retained": "Result could not be retained safely."}
    try:
        append_turn(session_id, conversation_id,
                    {"role": "user", "content": request.question, "database_ids": database_ids},
                    {"role": "assistant", "content": summary, "database_ids": database_ids, "result_id": result_id,
                     "database_names": [ref.name for ref in refs], "result": result_payload, "status": status})
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    if status == "refused":
        response.status_code = 403 if result.get("error_code") == "READ_ONLY_OPERATION_BLOCKED" else 422
    elif status == "error":
        response.status_code = 503 if any(term in summary.lower() for term in ("language model", "gemini model", "gemini api key", "nvidia", "model provider", "model api key")) else 422
    return QueryResponse(status=status, summary=summary, sql=result.get("sql") if status == "success" else None,
                         columns=result.get("columns", []), rows=result.get("rows", []),
                         duration_ms=result.get("duration_ms", 0), latency_ms=result.get("latency_ms"), correction_attempts=result.get("attempts", 0),
                         error=result.get("error"), error_code=result.get("error_code"),
                         response_type=result.get("response_type", "query"), visualization=result.get("visualization"),
                         statistics=result.get("statistics"), conversation_id=conversation_id, result_id=result_id,
                         database_ids=database_ids, database_names=[ref.name for ref in refs])
