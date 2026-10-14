from typing import Any, Literal
from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


class QueryResponse(BaseModel):
    status: Literal["success", "error", "refused"]
    summary: str
    sql: str | None = None
    columns: list[str] = []
    rows: list[dict[str, Any]] = []
    duration_ms: float = 0
    correction_attempts: int = 0
    error: str | None = None
    error_code: str | None = None


class ModelQuery(BaseModel):
    sql: str
    explanation: str


class DatabaseItem(BaseModel):
    id: str
    name: str
    is_sample: bool
    source_type: str = "sqlite"
    original_filename: str | None = None


class DatabaseListResponse(BaseModel):
    active_id: str
    databases: list[DatabaseItem]
    max_upload_bytes: int = 26214400


class ActiveDatabaseResponse(BaseModel):
    id: str
    name: str
    is_sample: bool
    source_type: str = "sqlite"
    original_filename: str | None = None
    schema_: dict[str, Any] = Field(alias="schema")


class DatabaseSelectRequest(BaseModel):
    database_id: str

