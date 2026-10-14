from collections import deque
import uuid

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .database import initialize_database
from .data_sources import DataSourceError, active_database, list_databases, register_upload, select_database, validate_session
from .schemas import ActiveDatabaseResponse, DatabaseListResponse, DatabaseSelectRequest, QueryRequest, QueryResponse
from .workflow import run_query

history = deque(maxlen=20)
settings = get_settings()
if not settings.database_path.exists():
    initialize_database(settings.database_path)

app = FastAPI(title="QueryPilot API", description="Guarded natural language analytics over SQLite databases.", version="1.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-QueryPilot-Session"], expose_headers=["X-QueryPilot-Session"])


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
        return select_database(session_id, body.database_id)
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
        return register_upload(session_id, file.filename or "", content, settings.max_upload_bytes, settings.max_databases_per_session)
    except DataSourceError as exc:
        raise api_error(exc) from None
    finally:
        file.file.close()


@app.get("/api/history")
def get_history():
    return list(history)


@app.post("/api/query", response_model=QueryResponse)
def query(request: QueryRequest, response: Response, http_request: Request):
    session_id = session_for(http_request, response)
    try:
        result = run_query(request.question, session_id=session_id)
    except DataSourceError as exc:
        raise api_error(exc) from None
    status = result.get("status", "error")
    summary = result.get("summary") or result.get("error") or "The query could not be completed."
    if status == "success":
        history.appendleft({"question": " ".join(request.question.split()), "summary": summary})
    elif status == "refused":
        response.status_code = 403 if result.get("error_code") == "READ_ONLY_OPERATION_BLOCKED" else 422
    elif status == "error":
        response.status_code = 503 if any(term in summary.lower() for term in ("language model", "gemini model", "gemini api key", "nvidia", "model provider", "model api key")) else 422
    return QueryResponse(status=status, summary=summary, sql=result.get("sql") if status == "success" else None,
                         columns=result.get("columns", []), rows=result.get("rows", []),
                         duration_ms=result.get("duration_ms", 0), correction_attempts=result.get("attempts", 0),
                         error=result.get("error"), error_code=result.get("error_code"))
