"""Session-scoped SQLite uploads and canonical schema inspection."""
import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import get_settings
from .database import open_readonly

SESSION_RE = re.compile(r"^[0-9a-f]{32}$")
SUPPORTED_SUFFIXES = {".db", ".sqlite", ".sqlite3"}


class DataSourceError(ValueError):
    pass


@dataclass(frozen=True)
class DatabaseRef:
    database_id: str
    name: str
    path: Path
    is_sample: bool
    schema: dict


def uploads_root() -> Path:
    configured = os.getenv("UPLOADS_DIR", "data/uploads")
    root = Path(configured)
    if not root.is_absolute():
        root = Path.cwd() / root
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _session_dir(session_id: str, create: bool = False) -> Path:
    if not SESSION_RE.fullmatch(session_id or ""):
        raise DataSourceError("Your data session is missing or invalid. Reload the page and try again.")
    path = uploads_root() / hashlib.sha256(session_id.encode()).hexdigest()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def validate_session(session_id: str) -> None:
    if not SESSION_RE.fullmatch(session_id or ""):
        raise DataSourceError("Your data session is missing or invalid. Reload the page and try again.")


def inspect_schema(path: Path) -> dict:
    """Inspect plain SQLite tables using read-only metadata queries."""
    con = None
    try:
        con = open_readonly(path)
        objects = con.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        tables = [row for row in objects if row["type"] == "table"]
        unsupported = [row for row in objects if row["type"] in ("view", "trigger") or (row["type"] == "table" and "CREATE VIRTUAL TABLE" in (row["sql"] or "").upper())]
        if unsupported:
            raise DataSourceError("This database contains views, triggers, or virtual tables, which QueryPilot does not support yet.")
        result = {}
        for obj in tables:
            name = obj["name"]
            # SQLite identifiers are quoted, and embedded quotes are doubled.
            quoted = '"' + name.replace('"', '""') + '"'
            columns = con.execute(f"PRAGMA table_xinfo({quoted})").fetchall()
            if not columns or any(row["hidden"] != 0 for row in columns):
                raise DataSourceError("This database contains generated or hidden columns, which QueryPilot does not support yet.")
            fks = con.execute(f"PRAGMA foreign_key_list({quoted})").fetchall()
            result[name] = {
                "columns": {row["name"]: {"type": row["type"] or "", "nullable": not bool(row["notnull"]), "primary_key": bool(row["pk"])} for row in columns},
                "primary_keys": [row["name"] for row in columns if row["pk"]],
                "foreign_keys": [{"column": row["from"], "referenced_table": row["table"], "referenced_column": row["to"]} for row in fks],
            }
        if not result:
            raise DataSourceError("This SQLite database has no usable tables.")
        if len(result) > 250 or sum(len(meta["columns"]) for meta in result.values()) > 5000:
            raise DataSourceError("This database schema is too large for the current QueryPilot limits.")
        return {"tables": result, "relationships": [f"{t}.{fk['column']} references {fk['referenced_table']}.{fk['referenced_column']}" for t, meta in result.items() for fk in meta["foreign_keys"]]}
    except DataSourceError:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise DataSourceError("The file is not a valid, readable SQLite database.") from exc
    finally:
        if con is not None:
            con.close()


def _load_registry(session_id: str) -> dict:
    directory = _session_dir(session_id)
    path = directory / "registry.json"
    if not path.exists():
        return {"active": "sample", "databases": {}}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data.get("databases"), dict):
            raise ValueError()
        # Ignore entries whose generated file is missing or no longer valid.
        data["databases"] = {key: value for key, value in data["databases"].items() if (directory / value.get("file", "")).is_file()}
        if data.get("active") != "sample" and data.get("active") not in data["databases"]:
            data["active"] = "sample"
        return data
    except (OSError, ValueError, TypeError):
        return {"active": "sample", "databases": {}}


def _save_registry(session_id: str, data: dict) -> None:
    directory = _session_dir(session_id, create=True)
    temp = directory / "registry.tmp"
    temp.write_text(json.dumps(data))
    temp.replace(directory / "registry.json")


def list_databases(session_id: str) -> dict:
    state = _load_registry(session_id)
    dbs = [{"id": "sample", "name": "Retail demo data", "is_sample": True}]
    dbs.extend({"id": key, "name": item["name"], "is_sample": False} for key, item in state["databases"].items())
    return {"active_id": state["active"], "databases": dbs}


def resolve_database(session_id: str, database_id: str | None = None) -> DatabaseRef:
    state = _load_registry(session_id)
    selected = database_id or state["active"]
    if selected == "sample":
        path = get_settings().database_path.resolve()
        schema = inspect_schema(path)
        schema["revenue_definition"] = "Sum of order_items.quantity * order_items.unit_price for orders whose status is completed or shipped."
        schema["sample_date_context"] = "Orders are from calendar year 2025. Use 2026-01-01 as the sample snapshot date for relative periods: last quarter is 2025 Q4 and last month is December 2025."
        return DatabaseRef("sample", "Retail demo data", path, True, schema)
    item = state["databases"].get(selected)
    if not item:
        raise DataSourceError("That database is not available in this session.")
    path = (_session_dir(session_id) / item["file"]).resolve()
    if path.parent != _session_dir(session_id).resolve():
        raise DataSourceError("That database is not available in this session.")
    return DatabaseRef(selected, item["name"], path, False, inspect_schema(path))


def active_database(session_id: str) -> dict:
    ref = resolve_database(session_id)
    return {"id": ref.database_id, "name": ref.name, "is_sample": ref.is_sample, "schema": ref.schema}


def select_database(session_id: str, database_id: str) -> dict:
    state = _load_registry(session_id)
    if database_id != "sample" and database_id not in state["databases"]:
        raise DataSourceError("That database is not available in this session.")
    state["active"] = database_id
    _save_registry(session_id, state)
    return active_database(session_id)


def register_upload(session_id: str, filename: str, content: bytes, max_bytes: int, max_databases: int) -> dict:
    if Path(filename or "").suffix.lower() not in SUPPORTED_SUFFIXES:
        raise DataSourceError("Choose a .db, .sqlite, or .sqlite3 file.")
    if not content or len(content) > max_bytes:
        raise DataSourceError(f"The database file is empty or exceeds the {max_bytes // (1024 * 1024)} MB upload limit.")
    directory = _session_dir(session_id, create=True)
    state = _load_registry(session_id)
    if len(state["databases"]) >= max_databases:
        raise DataSourceError(f"This session can store at most {max_databases} uploaded databases.")
    identifier = uuid.uuid4().hex
    file_name = f"{identifier}.sqlite3"
    temp_path = directory / f"{identifier}.uploading"
    final_path = directory / file_name
    try:
        with temp_path.open("xb") as stream:
            stream.write(content)
        # SQLite database header records rollback-journal versus WAL format.
        with temp_path.open("rb") as stream:
            header = stream.read(20)
        if len(header) < 20 or header[:16] != b"SQLite format 3\x00":
            raise DataSourceError("The file is not a valid SQLite database.")
        if header[18] == 2 or header[19] == 2:
            raise DataSourceError("This database uses SQLite WAL sidecar files. Export or checkpoint it before uploading.")
        schema = inspect_schema(temp_path)
        # Ensure a completed single-file database, not a WAL-dependent upload.
        check = open_readonly(temp_path)
        try:
            journal_mode = check.execute("PRAGMA journal_mode").fetchone()[0].lower()
        finally:
            check.close()
        if journal_mode == "wal":
            raise DataSourceError("This database uses SQLite WAL sidecar files. Export or checkpoint it before uploading.")
        temp_path.replace(final_path)
        safe_name = Path(filename).name[:120]
        state["databases"][identifier] = {"file": file_name, "name": safe_name}
        state["active"] = identifier
        _save_registry(session_id, state)
        return {"id": identifier, "name": safe_name, "is_sample": False, "schema": schema}
    except Exception:
        temp_path.unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        raise

