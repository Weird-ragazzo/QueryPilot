"""Session-scoped SQLite uploads and canonical schema inspection."""
import hashlib
import csv
import io
import json
import math
import os
import re
import sqlite3
import uuid
import zipfile
import xml.etree.ElementTree as ET
from threading import RLock
from contextlib import contextmanager
from functools import wraps
from dataclasses import dataclass
from pathlib import Path

from .config import get_settings
from .database import open_readonly

SESSION_RE = re.compile(r"^[0-9a-f]{32}$")
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
TABULAR_SUFFIXES = {".csv", ".tsv", ".xlsx"}
SUPPORTED_SUFFIXES = SQLITE_SUFFIXES | TABULAR_SUFFIXES
_registry_guard = RLock()
_registry_locks: dict[str, RLock] = {}
_source_locks_guard = RLock()
_source_locks: dict[tuple[str, str], RLock] = {}


def _serialized_registry(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        session_id = args[0] if args else kwargs.get("session_id", "")
        key = hashlib.sha256(session_id.encode()).hexdigest()
        with _registry_guard:
            lock = _registry_locks.setdefault(key, RLock())
        with lock:
            return fn(*args, **kwargs)
    return wrapped


@contextmanager
def lock_sources(session_id: str, source_ids: list[str]):
    """Keep selected files present for a query; deletion waits for active reads."""
    locks = []
    with _source_locks_guard:
        for source_id in sorted(set(source_ids)):
            locks.append(_source_locks.setdefault((hashlib.sha256(session_id.encode()).hexdigest(), source_id), RLock()))
    for lock in locks:
        lock.acquire()
    try:
        yield
    finally:
        for lock in reversed(locks):
            lock.release()


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
    """Inspect tables and safely validate ordinary views without running them."""
    con = None
    try:
        con = open_readonly(path)
        objects = con.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        tables = [row for row in objects if row["type"] == "table"]
        view_objects = [row for row in objects if row["type"] == "view"]
        unsupported = [row for row in objects if row["type"] == "trigger" or (row["type"] == "table" and "CREATE VIRTUAL TABLE" in (row["sql"] or "").upper()) or row["type"] not in ("table", "view", "index", "trigger")]
        if unsupported:
            raise DataSourceError("This database contains triggers, virtual tables, or unsupported SQLite objects.")
        parsed_views = {}
        if view_objects:
            import sqlglot
            from sqlglot import exp

            unsafe_functions = {"eval", "load_extension", "readfile", "writefile"}
            for obj in view_objects:
                try:
                    statements = sqlglot.parse(obj["sql"] or "", read="sqlite")
                    create = statements[0] if len(statements) == 1 else None
                    query = create.expression if isinstance(create, exp.Create) and str(create.args.get("kind", "")).casefold() == "view" else None
                    if not isinstance(query, exp.Query):
                        raise ValueError()
                    if any(isinstance(fn, exp.Anonymous) and fn.name.casefold() in unsafe_functions for fn in query.find_all(exp.Func)):
                        raise DataSourceError(f"View '{obj['name']}' uses a function that QueryPilot does not permit.")
                    parsed_views[obj["name"]] = query
                except DataSourceError:
                    raise
                except Exception:
                    raise DataSourceError(f"View '{obj['name']}' could not be parsed and validated safely.") from None
        result = {}
        for obj in tables:
            name = obj["name"]
            # SQLite identifiers are quoted, and embedded quotes are doubled.
            quoted = '"' + name.replace('"', '""') + '"'
            columns = con.execute(f"PRAGMA table_xinfo({quoted})").fetchall()
            if not columns or any(row["hidden"] != 0 for row in columns):
                raise DataSourceError("This database contains generated or hidden columns, which QueryPilot does not support yet.")
            fks = con.execute(f"PRAGMA foreign_key_list({quoted})").fetchall()
            foreign_keys = []
            for row in fks:
                referenced_column = row["to"]
                if not referenced_column:
                    ref_name = '"' + row["table"].replace('"', '""') + '"'
                    referenced_pk = con.execute(f"PRAGMA table_info({ref_name})").fetchall()
                    primary_keys = [pk["name"] for pk in referenced_pk if pk["pk"]]
                    if row["seq"] < len(primary_keys):
                        referenced_column = primary_keys[row["seq"]]
                foreign_keys.append({"column": row["from"], "referenced_table": row["table"], "referenced_column": referenced_column})
            result[name] = {
                "columns": {row["name"]: {"type": row["type"] or "", "nullable": not bool(row["notnull"]) and not bool(row["pk"]), "primary_key": bool(row["pk"])} for row in columns},
                "primary_keys": [row["name"] for row in columns if row["pk"]],
                "foreign_keys": foreign_keys,
            }
        views = {}
        for obj in view_objects:
            name = obj["name"]
            quoted = '"' + name.replace('"', '""') + '"'
            try:
                columns = con.execute(f"PRAGMA table_xinfo({quoted})").fetchall()
            except sqlite3.Error as exc:
                raise DataSourceError(f"View '{name}' has an unsupported or invalid definition.") from exc
            if not columns or any(row["hidden"] != 0 for row in columns):
                raise DataSourceError(f"View '{name}' has unsupported or hidden output columns.")
            views[name] = {
                "columns": {row["name"]: {"type": row["type"] or "", "nullable": True, "primary_key": False} for row in columns},
                "primary_keys": [],
                "foreign_keys": [],
                "depends_on": [],
            }
        if not result:
            raise DataSourceError("This SQLite database has no usable tables.")
        if len(result) + len(views) > 250 or sum(len(meta["columns"]) for meta in result.values()) + sum(len(meta["columns"]) for meta in views.values()) > 5000:
            raise DataSourceError("This database schema is too large for the current QueryPilot limits.")
        schema = {"tables": result, "views": views}
        if len(json.dumps(schema)) > 100_000:
            raise DataSourceError("This database schema is too large for the current QueryPilot limits.")
        if views:
            # sqlite_master stores CREATE VIEW text. Parse the SELECT body and
            # validate it against the inspected schema; never execute it here.
            from sqlglot.optimizer.scope import traverse_scope
            from .guardrails import SQLRejected, validate_sql

            definitions = {}
            for obj in view_objects:
                name = obj["name"]
                try:
                    query = parsed_views[name]
                    validate_sql(query.sql(dialect="sqlite"), schema)
                    deps = set()
                    for scope in traverse_scope(query):
                        for source in scope.sources.values():
                            if isinstance(source, exp.Table):
                                deps.add(source.name.casefold())
                    definitions[name.casefold()] = (query, deps)
                    views[name]["depends_on"] = sorted(deps)
                except SQLRejected as exc:
                    raise DataSourceError(f"View '{name}' is not supported: {exc}") from None
                except Exception:
                    raise DataSourceError(f"View '{name}' could not be parsed and validated safely.") from None

            visiting: set[str] = set()
            visited: set[str] = set()
            view_names = set(definitions)

            def check_view_cycles(view_name: str) -> None:
                if view_name in visiting:
                    raise DataSourceError(f"View '{view_name}' is not supported because its definition contains a cycle.")
                if view_name in visited:
                    return
                visiting.add(view_name)
                for dependency in definitions[view_name][1] & view_names:
                    check_view_cycles(dependency)
                visiting.remove(view_name)
                visited.add(view_name)

            for view_name in definitions:
                check_view_cycles(view_name)
            if len(json.dumps(schema)) > 100_000:
                raise DataSourceError("This database schema is too large for the current QueryPilot limits.")

        return {
            "tables": result,
            "views": views,
            "relationships": [f"{t}.{fk['column']} references {fk['referenced_table']}.{fk['referenced_column']}" for t, meta in result.items() for fk in meta["foreign_keys"]]
            + [f"view {name} reads {', '.join(meta['depends_on'])}" for name, meta in views.items()],
        }
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
        if data.get("active") != "sample" and data.get("active") not in data["databases"]:
            raise ValueError()
        return data
    except (OSError, ValueError, TypeError):
        raise DataSourceError("This session's saved database list cannot be read. Clear QueryPilot's saved browser session to start a new session.") from None


def _save_registry(session_id: str, data: dict) -> None:
    directory = _session_dir(session_id, create=True)
    temp = directory / "registry.tmp"
    temp.write_text(json.dumps(data))
    temp.replace(directory / "registry.json")


def _column_names(header) -> list[str]:
    raw_names = [str(value).strip() if value is not None else "" for value in header]
    if not raw_names or any("\x00" in name for name in raw_names):
        raise DataSourceError("The header row is invalid. Remove any null characters and try again.")
    if len(raw_names) > 500:
        raise DataSourceError("This file has too many columns (maximum 500).")
    # Real spreadsheets often contain blank or repeated headings. Keep the
    # user's rows and make those headings deterministic, unique identifiers.
    names: list[str] = []
    used: set[str] = set()
    for index, raw_name in enumerate(raw_names, start=1):
        base = raw_name or f"column_{index}"
        candidate = base
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        names.append(candidate)
        used.add(candidate.casefold())
    return names


def _infer_column(values: list[object]) -> tuple[str, list[object]]:
    nonempty = [v for v in values if v is not None and v != ""]
    if not nonempty:
        return "TEXT", [None if v == "" else v for v in values]
    try:
        ints = [int(v) for v in nonempty]
        if all(-(2**63) <= value < 2**63 for value in ints):
            it = iter(ints)
            return "INTEGER", [None if v is None or v == "" else next(it) for v in values]
    except (ValueError, TypeError, OverflowError):
        pass
    try:
        nums = [float(v) for v in nonempty]
        if all(math.isfinite(value) for value in nums):
            it = iter(nums)
            return "REAL", [None if v is None or v == "" else next(it) for v in values]
    except (ValueError, TypeError, OverflowError):
        pass
    return "TEXT", [None if v == "" else str(v) if v is not None else None for v in values]


def _safe_table_name(name: str, used: set[str]) -> str:
    base = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_]", "_", name)).strip("_") or "sheet"
    if base[0].isdigit():
        base = "sheet_" + base
    candidate = base[:120]
    suffix = 2
    while candidate.casefold() in used:
        tail = f"_{suffix}"
        candidate = base[:120-len(tail)] + tail
        suffix += 1
    used.add(candidate.casefold())
    return candidate


def _create_import_database(path: Path, source_name: str, suffix: str, content: bytes) -> dict:
    """Convert a bounded CSV/TSV/XLSX upload to a server-generated SQLite file."""
    sources: list[tuple[str, list[str], list[list[object]]]] = []
    max_rows = get_settings().max_import_rows
    max_sheets = get_settings().max_import_sheets
    max_cells = get_settings().max_import_cells
    if suffix in {".csv", ".tsv"}:
        try:
            text = io.TextIOWrapper(io.BytesIO(content), encoding="utf-8-sig", errors="strict", newline="")
            reader = csv.reader(text, delimiter="\t" if suffix == ".tsv" else ",", strict=True)
            rows = iter(reader)
            header = next((row for row in rows if row and any(cell.strip() for cell in row)), None)
            if header is None:
                raise DataSourceError("The uploaded file is empty or has no header row.")
            names = _column_names(header)
            data = []
            for row in rows:
                if not row or not any(cell.strip() for cell in row):
                    continue
                if len(row) > len(names):
                    raise DataSourceError("A data row contains more fields than the header; no rows were imported.")
                data.append(row + [None] * (len(names) - len(row)))
                if len(data) > max_rows or len(data) * len(names) > max_cells:
                    raise DataSourceError("This file exceeds the configured row or cell limit.")
            sources.append((source_name, names, data))
        except DataSourceError:
            raise
        except UnicodeDecodeError:
            raise DataSourceError("CSV and TSV uploads must use UTF-8 encoding (a UTF-8 BOM is supported).") from None
        except (csv.Error, StopIteration):
            raise DataSourceError("The CSV or TSV file is malformed or has no usable header.") from None
    else:
        try:
            import openpyxl
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                members = archive.infolist()
                if sum(member.file_size for member in members) > 100 * 1024 * 1024 or any(member.file_size > 25 * 1024 * 1024 for member in members):
                    raise DataSourceError("The XLSX workbook expands beyond the supported processing size.")
                if any(any(marker in item.filename.casefold() for marker in ("externallinks/", "vbaproject", "embeddings/", "connections.xml")) for item in members):
                    raise DataSourceError("This workbook contains macros, external links, or embedded objects that QueryPilot does not support.")
                for member in members:
                    if member.filename.startswith("xl/worksheets/") and member.filename.endswith(".xml"):
                        try:
                            with archive.open(member) as sheet_xml:
                                for _, element in ET.iterparse(sheet_xml, events=("end",)):
                                    if element.tag.endswith("}mergeCell"):
                                        raise DataSourceError("Merged cells are not supported in XLSX uploads. Unmerge them and upload again.")
                                    element.clear()
                        except ET.ParseError:
                            raise DataSourceError("The XLSX workbook contains malformed worksheet data.") from None
            workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
            if len(workbook.worksheets) > max_sheets:
                raise DataSourceError(f"The workbook exceeds the configured limit of {max_sheets} worksheets.")
            used: set[str] = set()
            for sheet in workbook.worksheets:
                iterator = sheet.iter_rows(values_only=False)
                header_cells = next((row for row in iterator if any(c.value is not None for c in row)), None)
                if header_cells is None:
                    continue
                if any(cell.data_type == "f" for cell in header_cells):
                    raise DataSourceError(f"Worksheet '{sheet.title}' contains formulas. Formula cells are not evaluated or imported.")
                names = _column_names([cell.value for cell in header_cells])
                data = []
                for row in iterator:
                    if any(cell.data_type == "f" for cell in row):
                        raise DataSourceError(f"Worksheet '{sheet.title}' contains formulas. Formula cells are not evaluated or imported.")
                    values = [cell.value for cell in row]
                    if not any(value is not None for value in values):
                        continue
                    if len(values) > len(names) and any(value is not None for value in values[len(names):]):
                        raise DataSourceError(f"Worksheet '{sheet.title}' contains data beyond its header columns.")
                    data.append(values[:len(names)] + [None] * max(0, len(names)-len(values)))
                    if len(data) > max_rows or sum(len(item[2]) * len(item[1]) for item in sources) + len(data) * len(names) > max_cells:
                        raise DataSourceError("This workbook exceeds the configured row or cell limit.")
                if data:
                    sources.append((sheet.title, names, data))
                    if len(sources) > max_sheets:
                        raise DataSourceError(f"The workbook exceeds the configured limit of {max_sheets} non-empty worksheets.")
            workbook.close()
            if not sources:
                raise DataSourceError("The workbook has no non-empty worksheets with a header and data.")
        except DataSourceError:
            raise
        except Exception:
            raise DataSourceError("The XLSX workbook is invalid or uses an unsupported feature.") from None

    con = sqlite3.connect(path)
    source_names = {}
    used_names: set[str] = set()
    try:
        for original_name, columns, rows in sources:
            table = _safe_table_name(original_name if suffix == ".xlsx" else Path(source_name).stem, used_names)
            inferred = [_infer_column([row[i] for row in rows]) for i in range(len(columns))]
            quoted_table = '"' + table.replace('"', '""') + '"'
            definitions = []
            for column, (kind, _) in zip(columns, inferred):
                quoted_column = '"' + column.replace('"', '""') + '"'
                definitions.append(f"{quoted_column} {kind}")
            con.execute(f"CREATE TABLE {quoted_table} ({', '.join(definitions)})")
            if rows:
                converted = [tuple(inferred[i][1][row_index] for i in range(len(columns))) for row_index in range(len(rows))]
                marks = ",".join("?" for _ in columns)
                con.executemany(f"INSERT INTO {quoted_table} VALUES ({marks})", converted)
            source_names[table] = original_name
        con.commit()
    except sqlite3.Error:
        raise DataSourceError("QueryPilot could not build the SQLite database from this file.") from None
    finally:
        con.close()
    return source_names


def list_databases(session_id: str) -> dict:
    state = _load_registry(session_id)
    dbs = [{"id": "sample", "name": "Retail demo data", "is_sample": True, "source_type": "sample", "original_filename": None}]
    dbs.extend({"id": key, "name": item["name"], "is_sample": False, "source_type": item.get("source_type", "sqlite"), "original_filename": item.get("original_filename", item["name"])} for key, item in state["databases"].items())
    settings = get_settings()
    return {"active_id": state["active"], "databases": dbs, "max_upload_bytes": settings.max_upload_bytes,
            "max_import_rows": settings.max_import_rows, "max_import_sheets": settings.max_import_sheets,
            "max_import_cells": settings.max_import_cells}


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
    if not path.is_file():
        raise DataSourceError("The selected uploaded database is missing. Choose the sample database or upload the file again.")
    schema = inspect_schema(path)
    schema["source_type"] = item.get("source_type", "sqlite")
    schema["original_filename"] = item.get("original_filename", item["name"])
    schema["source_names"] = item.get("source_names", {})
    return DatabaseRef(selected, item["name"], path, False, schema)


def active_database(session_id: str) -> dict:
    ref = resolve_database(session_id)
    return {"id": ref.database_id, "name": ref.name, "is_sample": ref.is_sample,
            "source_type": ref.schema.get("source_type", "sample" if ref.is_sample else "sqlite"),
            "original_filename": ref.schema.get("original_filename"), "schema": ref.schema}


@_serialized_registry
def select_database(session_id: str, database_id: str) -> dict:
    state = _load_registry(session_id)
    if database_id != "sample" and database_id not in state["databases"]:
        raise DataSourceError("That database is not available in this session.")
    if database_id != "sample":
        resolve_database(session_id, database_id)
    state["active"] = database_id
    _save_registry(session_id, state)
    return active_database(session_id)


@_serialized_registry
def register_upload(session_id: str, filename: str, content: bytes, max_bytes: int, max_databases: int) -> dict:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise DataSourceError("Choose a SQLite database (.db, .sqlite, .sqlite3), CSV, TSV, or XLSX file.")
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
    safe_name = (filename or "upload").replace("\\", "/").split("/")[-1][:120]
    source_type = suffix[1:]
    try:
        source_names = {}
        if suffix in SQLITE_SUFFIXES:
            with temp_path.open("xb") as stream:
                stream.write(content)
            # SQLite database header records rollback-journal versus WAL format.
            with temp_path.open("rb") as stream:
                header = stream.read(20)
            if len(header) < 20 or header[:16] != b"SQLite format 3\x00":
                raise DataSourceError("The file is not a valid SQLite database.")
            if header[18] == 2 or header[19] == 2:
                raise DataSourceError("This database uses SQLite WAL sidecar files. Export or checkpoint it before uploading.")
        else:
            source_names = _create_import_database(temp_path, safe_name, suffix, content)
        schema = inspect_schema(temp_path)
        if suffix in SQLITE_SUFFIXES:
            # Ensure a completed single-file database, not a WAL-dependent upload.
            check = open_readonly(temp_path)
            try:
                journal_mode = check.execute("PRAGMA journal_mode").fetchone()[0].lower()
            finally:
                check.close()
            if journal_mode == "wal":
                raise DataSourceError("This database uses SQLite WAL sidecar files. Export or checkpoint it before uploading.")
        temp_path.replace(final_path)
        state["databases"][identifier] = {"file": file_name, "name": safe_name, "source_type": source_type,
                                           "original_filename": safe_name, "source_names": source_names}
        state["active"] = identifier
        _save_registry(session_id, state)
        schema["source_type"] = source_type
        schema["original_filename"] = safe_name
        schema["source_names"] = source_names
        return {"id": identifier, "name": safe_name, "is_sample": False, "source_type": source_type,
                "original_filename": safe_name, "schema": schema}
    except Exception:
        temp_path.unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        raise


@_serialized_registry
def delete_database(session_id: str, database_id: str) -> dict:
    if database_id == "sample":
        raise DataSourceError("The bundled sample database cannot be deleted.")
    state = _load_registry(session_id)
    item = state["databases"].get(database_id)
    if not item:
        raise DataSourceError("That database is not available in this session.")
    with lock_sources(session_id, [database_id]):
        directory = _session_dir(session_id).resolve()
        path = (directory / item["file"]).resolve()
        if path.parent != directory or path.name != item["file"]:
            raise DataSourceError("That database is not available in this session.")
        state["databases"].pop(database_id)
        if state.get("active") == database_id:
            state["active"] = "sample"
        _save_registry(session_id, state)
        path.unlink(missing_ok=True)
        # Generated tabular uploads may leave only the one registered SQLite file.
        return list_databases(session_id)

