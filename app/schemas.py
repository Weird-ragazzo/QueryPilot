from typing import Any, Literal
from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    database_ids: list[str] = Field(default_factory=list, max_length=10)
    conversation_id: str | None = Field(default=None, max_length=64)


class QueryResponse(BaseModel):
    status: Literal["success", "error", "refused"]
    summary: str
    sql: str | None = None
    columns: list[str] = []
    rows: list[dict[str, Any]] = []
    duration_ms: float = 0
    latency_ms: float | None = None
    correction_attempts: int = 0
    error: str | None = None
    error_code: str | None = None
    response_type: Literal["query", "visualization", "summary", "comparison", "examine"] = "query"
    visualization: dict[str, Any] | None = None
    statistics: dict[str, Any] | None = None
    conversation_id: str | None = None
    result_id: str | None = None
    database_ids: list[str] = []
    database_names: list[str] = []


class ResponsePlan(BaseModel):
    intent: Literal["query", "visualization", "summary", "comparison", "examine"]
    requested_chart: Literal["bar", "line", "pie", "donut", "scatter"] | None = None
    explicit_chart: bool = False
    summary_target: Literal["dataset", "query_result"] = "dataset"


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
    max_import_rows: int = 100000
    max_import_sheets: int = 20
    max_import_cells: int = 2000000


class ActiveDatabaseResponse(BaseModel):
    id: str
    name: str
    is_sample: bool
    source_type: str = "sqlite"
    original_filename: str | None = None
    schema_: dict[str, Any] = Field(alias="schema")


class DatabaseSelectRequest(BaseModel):
    database_id: str

