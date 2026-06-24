#!/usr/bin/env python3
"""
codeintel.py — Code Intelligence DB v1.1.2

Single-file, standard-library-only implementation.

Usage:
    python codeintel.py scan <path>   — scan a file or directory tree
    python codeintel.py map           — show current entity map
    python codeintel.py find <name>   — find entities by name
    python codeintel.py stale         — list files modified since last scan

DB path: .codeintel/codeintel.sqlite  (relative to cwd)
"""

import ast
import csv
import datetime
import hashlib
import inspect
import json
import re
import sqlite3
import sys
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Version constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "1.3.0"
SCANNER_VERSION = "codeintel 1.3.0"
DEFAULT_DB_PATH = Path(".codeintel") / "codeintel.sqlite"

# ---------------------------------------------------------------------------
# Embedded schema — v1.2.0 / v1.3.0
# Note: entity_location.detection_method has NO DEFAULT.
# Every extractor must supply the value explicitly.
# ---------------------------------------------------------------------------

SCHEMA_SQL = """\
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Self-description / migration metadata
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Design / meta layer
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS meta_glossary_term (
    id                   INTEGER PRIMARY KEY,
    term                 TEXT NOT NULL UNIQUE,
    layer                TEXT NOT NULL,
    concise_meaning      TEXT NOT NULL,
    minimal_members_now  TEXT NOT NULL DEFAULT '[]',
    future_bucket        TEXT NOT NULL DEFAULT '[]',
    v1_status            TEXT NOT NULL DEFAULT 'keep',
    notes                TEXT
);

CREATE TABLE IF NOT EXISTS meta_relationship_type (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE,
    concise_meaning  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta_glossary_relationship (
    id                    INTEGER PRIMARY KEY,
    subject_term_id       INTEGER NOT NULL REFERENCES meta_glossary_term(id) ON DELETE CASCADE,
    relationship_type_id  INTEGER NOT NULL REFERENCES meta_relationship_type(id),
    object_term_id        INTEGER NOT NULL REFERENCES meta_glossary_term(id) ON DELETE CASCADE,
    cardinality           TEXT NOT NULL,
    v1_status             TEXT NOT NULL DEFAULT 'keep',
    meaning               TEXT NOT NULL,
    notes                 TEXT,
    UNIQUE(subject_term_id, relationship_type_id, object_term_id, cardinality)
);

CREATE TABLE IF NOT EXISTS meta_design_bucket (
    id         INTEGER PRIMARY KEY,
    subject    TEXT NOT NULL,
    idea_kind  TEXT NOT NULL,
    idea_text  TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Core implementation layer
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS git_repo (
    id        INTEGER PRIMARY KEY,
    repo_slug TEXT NOT NULL,
    root_path TEXT NOT NULL,
    UNIQUE(root_path)
);

CREATE TABLE IF NOT EXISTS git_branch (
    id          INTEGER PRIMARY KEY,
    repo_id     INTEGER NOT NULL REFERENCES git_repo(id) ON DELETE CASCADE,
    branch_name TEXT NOT NULL,
    UNIQUE(repo_id, branch_name)
);

CREATE TABLE IF NOT EXISTS branch_artifact (
    id            INTEGER PRIMARY KEY,
    branch_id     INTEGER NOT NULL REFERENCES git_branch(id) ON DELETE CASCADE,
    artifact_kind TEXT NOT NULL,
    local_path    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_run (
    id                INTEGER PRIMARY KEY,
    branch_id         INTEGER NOT NULL REFERENCES git_branch(id) ON DELETE CASCADE,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    scanner_version   TEXT NOT NULL,
    files_scanned     INTEGER NOT NULL DEFAULT 0,
    entities_observed INTEGER NOT NULL DEFAULT 0,
    drifts_detected   INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'complete', 'failed'))
);

CREATE TABLE IF NOT EXISTS source_file (
    id               INTEGER PRIMARY KEY,
    branch_id        INTEGER NOT NULL REFERENCES git_branch(id) ON DELETE CASCADE,
    file_path        TEXT NOT NULL,
    file_hash        TEXT NOT NULL,
    language         TEXT NOT NULL DEFAULT 'python',
    last_scan_run_id INTEGER REFERENCES scan_run(id),
    UNIQUE(branch_id, file_path)
);

CREATE TABLE IF NOT EXISTS code_entity (
    id               INTEGER PRIMARY KEY,
    branch_id        INTEGER NOT NULL REFERENCES git_branch(id) ON DELETE CASCADE,
    parent_entity_id INTEGER REFERENCES code_entity(id) ON DELETE CASCADE,
    entity_type      TEXT NOT NULL,
    name             TEXT NOT NULL,
    qualified_name   TEXT NOT NULL,
    lifecycle_status TEXT NOT NULL DEFAULT 'active'
        CHECK (lifecycle_status IN ('active', 'removed')),
    UNIQUE(branch_id, qualified_name)
);

CREATE TABLE IF NOT EXISTS entity_location (
    id               INTEGER PRIMARY KEY,
    entity_id        INTEGER NOT NULL REFERENCES code_entity(id) ON DELETE CASCADE,
    source_file_id   INTEGER NOT NULL REFERENCES source_file(id) ON DELETE CASCADE,
    scan_run_id      INTEGER REFERENCES scan_run(id),
    start_line       INTEGER NOT NULL,
    end_line         INTEGER NOT NULL,
    detection_method TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_text (
    id        INTEGER PRIMARY KEY,
    entity_id INTEGER NOT NULL REFERENCES code_entity(id) ON DELETE CASCADE,
    text_kind TEXT NOT NULL,
    text_body TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_hash (
    id          INTEGER PRIMARY KEY,
    entity_id   INTEGER NOT NULL REFERENCES code_entity(id) ON DELETE CASCADE,
    scan_run_id INTEGER REFERENCES scan_run(id),
    hash_kind   TEXT NOT NULL,
    hash_value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drift_event (
    id          INTEGER PRIMARY KEY,
    entity_id   INTEGER NOT NULL REFERENCES code_entity(id) ON DELETE CASCADE,
    scan_run_id INTEGER REFERENCES scan_run(id),
    drift_kind  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'acknowledged', 'ignored')),
    old_hash    TEXT,
    new_hash    TEXT,
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    acknowledgement_note TEXT,
    resolution_kind TEXT
);

-- ---------------------------------------------------------------------------
-- Per-scan observation uniqueness + query-path indexes
-- ---------------------------------------------------------------------------

CREATE UNIQUE INDEX IF NOT EXISTS ux_entity_location_entity_scan
    ON entity_location(entity_id, scan_run_id);

CREATE UNIQUE INDEX IF NOT EXISTS ux_entity_hash_entity_scan_kind
    ON entity_hash(entity_id, scan_run_id, hash_kind);

CREATE INDEX IF NOT EXISTS ix_source_file_branch_path
    ON source_file(branch_id, file_path);

CREATE INDEX IF NOT EXISTS ix_entity_location_source_scan
    ON entity_location(source_file_id, scan_run_id);

CREATE INDEX IF NOT EXISTS ix_entity_location_entity_scan
    ON entity_location(entity_id, scan_run_id);

CREATE INDEX IF NOT EXISTS ix_entity_hash_entity_kind_scan
    ON entity_hash(entity_id, hash_kind, scan_run_id);

CREATE INDEX IF NOT EXISTS ix_drift_event_open
    ON drift_event(entity_id, status)
    WHERE status = 'open';

CREATE INDEX IF NOT EXISTS ix_scan_run_branch_status
    ON scan_run(branch_id, status, started_at);

-- ---------------------------------------------------------------------------
-- Read views
-- ---------------------------------------------------------------------------

CREATE VIEW IF NOT EXISTS v_entity_current AS
SELECT
    e.id               AS entity_id,
    e.branch_id,
    e.parent_entity_id,
    e.qualified_name,
    e.entity_type,
    e.name,
    e.lifecycle_status,
    sf.file_path,
    sf.language,
    el.start_line,
    el.end_line,
    el.detection_method,
    el.scan_run_id     AS located_in_scan
FROM code_entity e
LEFT JOIN entity_location el
    ON el.id = (
        SELECT el2.id
        FROM entity_location el2
        WHERE el2.entity_id = e.id
        ORDER BY el2.scan_run_id DESC, el2.id DESC
        LIMIT 1
    )
LEFT JOIN source_file sf
    ON sf.id = el.source_file_id
WHERE e.lifecycle_status = 'active';

CREATE VIEW IF NOT EXISTS v_open_drifts AS
SELECT
    de.id          AS drift_event_id,
    de.scan_run_id,
    e.qualified_name,
    e.entity_type,
    e.lifecycle_status,
    sf.file_path,
    el.start_line,
    de.drift_kind,
    de.old_hash,
    de.new_hash,
    de.status
FROM drift_event de
JOIN code_entity e
    ON e.id = de.entity_id
LEFT JOIN entity_location el
    ON el.id = (
        SELECT el2.id
        FROM entity_location el2
        WHERE el2.entity_id = e.id
        ORDER BY el2.scan_run_id DESC, el2.id DESC
        LIMIT 1
    )
LEFT JOIN source_file sf
    ON sf.id = el.source_file_id
WHERE de.status = 'open';

-- ---------------------------------------------------------------------------
-- Call observations
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS call_observation (
    id               INTEGER PRIMARY KEY,
    caller_entity_id INTEGER NOT NULL REFERENCES code_entity(id) ON DELETE CASCADE,
    source_file_id   INTEGER NOT NULL REFERENCES source_file(id) ON DELETE CASCADE,
    scan_run_id      INTEGER REFERENCES scan_run(id),
    call_text        TEXT NOT NULL,
    call_kind        TEXT NOT NULL,
    start_line       INTEGER NOT NULL,
    end_line         INTEGER NOT NULL,
    confidence       TEXT NOT NULL DEFAULT 'observed'
);

CREATE INDEX IF NOT EXISTS ix_call_observation_caller_scan
    ON call_observation(caller_entity_id, scan_run_id);

CREATE INDEX IF NOT EXISTS ix_call_observation_call_text
    ON call_observation(call_text);

CREATE INDEX IF NOT EXISTS ix_call_observation_source_scan
    ON call_observation(source_file_id, scan_run_id);

CREATE VIEW IF NOT EXISTS v_call_observation AS
SELECT
    co.id AS call_observation_id,
    co.scan_run_id,
    caller.qualified_name AS caller_qualified_name,
    caller.entity_type   AS caller_entity_type,
    sf.file_path,
    co.call_text,
    co.call_kind,
    co.start_line,
    co.end_line,
    co.confidence
FROM call_observation co
JOIN code_entity caller ON caller.id = co.caller_entity_id
JOIN source_file sf     ON sf.id = co.source_file_id;

CREATE VIEW IF NOT EXISTS v_call_observation_current AS
SELECT
    co.id AS call_observation_id,
    co.scan_run_id,
    caller.qualified_name AS caller_qualified_name,
    caller.entity_type   AS caller_entity_type,
    sf.file_path,
    co.call_text,
    co.call_kind,
    co.start_line,
    co.end_line,
    co.confidence
FROM call_observation co
JOIN code_entity caller ON caller.id = co.caller_entity_id
JOIN source_file sf     ON sf.id = co.source_file_id
WHERE caller.lifecycle_status = 'active'
  AND co.scan_run_id = (
      SELECT el2.scan_run_id
      FROM entity_location el2
      WHERE el2.entity_id = co.caller_entity_id
      ORDER BY el2.scan_run_id DESC, el2.id DESC
      LIMIT 1
  );

-- ---------------------------------------------------------------------------
-- Symbol observations (external/interface coupling only)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS symbol_observation (
    id              INTEGER PRIMARY KEY,
    owner_entity_id INTEGER NOT NULL REFERENCES code_entity(id) ON DELETE CASCADE,
    source_file_id  INTEGER NOT NULL REFERENCES source_file(id) ON DELETE CASCADE,
    scan_run_id     INTEGER REFERENCES scan_run(id),
    symbol_name     TEXT NOT NULL,
    access_kind     TEXT NOT NULL,
    scope_hint      TEXT NOT NULL DEFAULT 'unknown',
    start_line      INTEGER NOT NULL,
    end_line        INTEGER NOT NULL,
    confidence      TEXT NOT NULL DEFAULT 'observed'
);

CREATE INDEX IF NOT EXISTS ix_symbol_observation_owner_scan
    ON symbol_observation(owner_entity_id, scan_run_id);

CREATE INDEX IF NOT EXISTS ix_symbol_observation_symbol
    ON symbol_observation(symbol_name);

CREATE INDEX IF NOT EXISTS ix_symbol_observation_source_scan
    ON symbol_observation(source_file_id, scan_run_id);

CREATE VIEW IF NOT EXISTS v_symbol_observation AS
SELECT
    so.id AS symbol_observation_id,
    so.scan_run_id,
    owner.qualified_name AS owner_qualified_name,
    owner.entity_type   AS owner_entity_type,
    sf.file_path,
    so.symbol_name,
    so.access_kind,
    so.scope_hint,
    so.start_line,
    so.end_line,
    so.confidence
FROM symbol_observation so
JOIN code_entity owner ON owner.id = so.owner_entity_id
JOIN source_file sf    ON sf.id = so.source_file_id;

CREATE VIEW IF NOT EXISTS v_symbol_observation_current AS
SELECT
    so.id AS symbol_observation_id,
    so.scan_run_id,
    owner.qualified_name AS owner_qualified_name,
    owner.entity_type   AS owner_entity_type,
    sf.file_path,
    so.symbol_name,
    so.access_kind,
    so.scope_hint,
    so.start_line,
    so.end_line,
    so.confidence
FROM symbol_observation so
JOIN code_entity owner ON owner.id = so.owner_entity_id
JOIN source_file sf    ON sf.id = so.source_file_id
WHERE owner.lifecycle_status = 'active'
  AND so.scan_run_id = (
      SELECT el2.scan_run_id
      FROM entity_location el2
      WHERE el2.entity_id = so.owner_entity_id
      ORDER BY el2.scan_run_id DESC, el2.id DESC
      LIMIT 1
  );
"""

# ---------------------------------------------------------------------------
# SQL constraint-starter keywords — disqualify a line from being a column def
# ---------------------------------------------------------------------------

_SQL_CONSTRAINT_STARTERS = frozenset(
    {"PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT"}
)

# ---------------------------------------------------------------------------
# SQL identifier validation — used by SchemaMigrator
# ---------------------------------------------------------------------------

_SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_sql_identifier(name: str, label: str) -> None:
    """Raise MigrationError if name is not a simple SQLite identifier."""
    if not _SQL_IDENTIFIER_RE.match(name):
        raise MigrationError(f"invalid {label}: {name!r}")

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _find_git_root(start: Path) -> Optional[Path]:
    current = start.resolve()
    while True:
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _get_branch_name(git_root: Path) -> str:
    head = git_root / ".git" / "HEAD"
    try:
        content = head.read_text(encoding="utf-8").strip()
        if content.startswith("ref: refs/heads/"):
            return content[len("ref: refs/heads/"):]
        return content[:8] if len(content) >= 8 else content
    except OSError:
        return "main"


def _get_repo_info(scan_path: Path) -> Tuple[str, str, str]:
    """Return (repo_slug, root_path_str, branch_name)."""
    candidate = scan_path if scan_path.is_dir() else scan_path.parent
    git_root = _find_git_root(candidate)
    if git_root:
        return git_root.name, str(git_root), _get_branch_name(git_root)
    # No git root: use the scan root itself (the directory for a directory
    # scan, or the file's parent for a single-file scan).  Never fall back
    # to cwd unless cwd happens to be that root.
    root = candidate.resolve()
    return root.name, str(root), "main"


def _is_column_def(line: str) -> Optional[str]:
    """
    Return the column name if the line looks like a column definition inside
    a CREATE TABLE body, otherwise return None.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("--") or stripped.startswith(")"):
        return None
    m = re.match(r"^(\w+)\s+\w", stripped)
    if not m:
        return None
    first = m.group(1).upper()
    if first in _SQL_CONSTRAINT_STARTERS:
        return None
    return m.group(1)


@dataclass(frozen=True)
class MigrationSpec:
    key: str
    version: str
    sequence: int
    operation: str
    object_name: str
    payload: str
    notes: str = ""
    checksum: str = ""


class MigrationError(Exception):
    """Raised when schema migration parsing or application fails."""


class SchemaMigrator:
    """Small TSV-driven SQLite schema migrator."""

    def __init__(self, conn: sqlite3.Connection, applied_by: str = "") -> None:
        self._conn = conn
        self._applied_by = applied_by

    def migrate(self, tsv_path: Path) -> None:
        self._ensure_ledger()
        specs = self._read_tsv(tsv_path)
        self._register_and_validate(specs)
        self._conn.commit()
        self._apply_pending()
        self._conn.commit()

    def _ensure_ledger(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migration (
                key             TEXT PRIMARY KEY,
                version         TEXT NOT NULL,
                sequence        INTEGER NOT NULL,
                operation       TEXT NOT NULL,
                object_name     TEXT NOT NULL DEFAULT '',
                payload         TEXT NOT NULL,
                notes           TEXT NOT NULL DEFAULT '',
                checksum        TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'applied', 'failed')),
                applied_at      TEXT,
                applied_by      TEXT,
                error_text      TEXT
            );
            """
        )

    def _read_tsv(self, tsv_path: Path) -> List[MigrationSpec]:
        try:
            with tsv_path.open("r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh, delimiter="\t")
                required = {
                    "key",
                    "version",
                    "sequence",
                    "operation",
                    "object_name",
                    "payload",
                    "notes",
                }
                if reader.fieldnames is None or not required.issubset(reader.fieldnames):
                    raise MigrationError("migration TSV header is missing required columns")

                specs: List[MigrationSpec] = []
                for row in reader:
                    if not row:
                        continue
                    key = (row.get("key") or "").strip()
                    version = (row.get("version") or "").strip()
                    operation = (row.get("operation") or "").strip()
                    object_name = (row.get("object_name") or "").strip()
                    payload = (row.get("payload") or "").strip()
                    notes = (row.get("notes") or "").strip()
                    if not key or not version or not operation or not payload:
                        raise MigrationError(f"invalid migration row: {row}")
                    try:
                        sequence = int((row.get("sequence") or "").strip())
                    except ValueError as exc:
                        raise MigrationError(f"invalid sequence for migration {key!r}") from exc
                    checksum = self._checksum_for(
                        key=key,
                        version=version,
                        sequence=sequence,
                        operation=operation,
                        object_name=object_name,
                        payload=payload,
                        notes=notes,
                    )
                    specs.append(
                        MigrationSpec(
                            key=key,
                            version=version,
                            sequence=sequence,
                            operation=operation,
                            object_name=object_name,
                            payload=payload,
                            notes=notes,
                            checksum=checksum,
                        )
                    )
                return sorted(specs, key=lambda s: (s.sequence, s.key))
        except OSError as exc:
            raise MigrationError(f"failed to read migration TSV: {tsv_path}") from exc

    @staticmethod
    def _checksum_for(
        *,
        key: str,
        version: str,
        sequence: int,
        operation: str,
        object_name: str,
        payload: str,
        notes: str,
    ) -> str:
        joined = "|".join(
            [
                key,
                version,
                str(sequence),
                operation,
                object_name,
                payload,
                notes,
            ]
        )
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def _register_and_validate(self, specs: List[MigrationSpec]) -> None:
        """Register unseen migrations as pending; raise MigrationError on checksum mismatch."""
        for spec in specs:
            existing = self._conn.execute(
                "SELECT checksum FROM schema_migration WHERE key = ?",
                (spec.key,),
            ).fetchone()
            if existing is not None:
                if existing["checksum"] != spec.checksum:
                    raise MigrationError(
                        f"migration {spec.key!r} checksum mismatch; "
                        "registered migrations must not be edited"
                    )
                # Checksum matches — already registered, nothing to do.
                continue
            self._conn.execute(
                "INSERT INTO schema_migration"
                "(key, version, sequence, operation, object_name, payload, notes, checksum, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                (
                    spec.key,
                    spec.version,
                    spec.sequence,
                    spec.operation,
                    spec.object_name,
                    spec.payload,
                    spec.notes,
                    spec.checksum,
                ),
            )

    def _apply_pending(self) -> None:
        # Only pending rows are applied.  Failed rows are NOT retried automatically.
        # To retry a failed migration, inspect the error_text column in schema_migration,
        # fix the underlying problem, then manually reset the row's status to 'pending',
        # or create a new corrective migration with a different key.
        rows = self._conn.execute(
            "SELECT key, version, sequence, operation, object_name, payload, notes, checksum "
            "FROM schema_migration "
            "WHERE status = 'pending' "
            "ORDER BY sequence ASC, key ASC"
        ).fetchall()
        for row in rows:
            spec = MigrationSpec(
                key=row["key"],
                version=row["version"],
                sequence=row["sequence"],
                operation=row["operation"],
                object_name=row["object_name"],
                payload=row["payload"],
                notes=row["notes"],
                checksum=row["checksum"],
            )
            self._apply_one(spec)

    def _apply_one(self, spec: MigrationSpec) -> None:
        try:
            if spec.operation == "add_column":
                self._apply_add_column(spec.object_name, spec.payload)
            elif spec.operation == "exec_sql":
                self._conn.executescript(spec.payload)
            else:
                raise MigrationError(f"unsupported migration operation: {spec.operation}")
            self._conn.execute(
                "UPDATE schema_migration "
                "SET status = 'applied', applied_at = ?, applied_by = ?, error_text = NULL "
                "WHERE key = ?",
                (_utcnow(), self._applied_by, spec.key),
            )
        except Exception as exc:
            self._conn.execute(
                "UPDATE schema_migration "
                "SET status = 'failed', error_text = ? "
                "WHERE key = ?",
                (str(exc), spec.key),
            )
            self._conn.commit()
            raise MigrationError(f"failed migration {spec.key}: {exc}") from exc

    def _apply_add_column(self, table_name: str, payload: str) -> None:
        _validate_sql_identifier(table_name, "table name")
        stripped_payload = payload.strip()
        if not stripped_payload:
            raise MigrationError("add_column payload is empty")
        col_name = stripped_payload.split()[0]
        _validate_sql_identifier(col_name, "column name")

        existing = self._conn.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
        existing_cols = {row["name"] for row in existing}
        if col_name in existing_cols:
            return

        self._conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {payload}"
        )


# ---------------------------------------------------------------------------
# Directory traversal
# ---------------------------------------------------------------------------

_IGNORE_DIRS = frozenset({
    ".git",
    ".codeintel",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "dist",
    "build",
})


def _collect_files(root: Path) -> List[Path]:
    """
    Collect all files under root, skipping directories whose names are in
    _IGNORE_DIRS.  Returns a sorted list of absolute Path objects.
    """
    result: List[Path] = []
    _collect_files_inner(root, result)
    return sorted(result)


def _collect_files_inner(dir_path: Path, result: List[Path]) -> None:
    try:
        entries = sorted(dir_path.iterdir())
    except PermissionError:
        return
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.is_dir():
            if entry.name not in _IGNORE_DIRS:
                _collect_files_inner(entry, result)
        elif entry.is_file():
            result.append(entry)


# ---------------------------------------------------------------------------
# String / path helpers
# ---------------------------------------------------------------------------


def _like_escape(s: str) -> str:
    """Escape LIKE special characters so the pattern matches a literal string."""
    s = s.replace("\\", "\\\\")
    s = s.replace("%", "\\%")
    s = s.replace("_", "\\_")
    return s


def _repo_relative_path(path: Path, repo_root: Path) -> str:
    """
    Return a forward-slash repo-relative path string.
    Falls back to the absolute posix path if path is not under repo_root.
    """
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _py_module_qname(path: Path, repo_root: Path) -> str:
    """
    Derive the dotted Python module qualified name from a .py file path
    relative to repo_root.

    Examples:
        a/utils.py      -> a.utils
        b/utils.py      -> b.utils
        pkg/__init__.py -> pkg
        src/pkg/mod.py  -> src.pkg.mod
    """
    try:
        rel = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return path.stem
    parts = list(rel.parts)
    # Strip .py extension from the final part
    parts[-1] = parts[-1][:-3]
    # __init__ represents the package itself; drop it
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return path.stem
    return ".".join(parts)


def _sql_qname_prefix(path: Path, repo_root: Path) -> str:
    """
    Derive a dotted prefix from a .sql file path relative to repo_root, used
    to disambiguate SQL entity qualified names across files.

    Examples:
        schema.sql        -> schema
        other/schema.sql  -> other.schema
    """
    try:
        rel = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return path.stem
    parts = list(rel.parts)
    if path.suffix:
        parts[-1] = parts[-1][: -len(path.suffix)]
    return ".".join(parts) if parts else path.stem


def _effective_start_line(node) -> int:
    """
    Return the 1-based line where an entity's source effectively begins:
    the first decorator line if the node is decorated, otherwise the
    def/class keyword line.
    """
    decorators = getattr(node, "decorator_list", None)
    if decorators:
        return min(d.lineno for d in decorators)
    return node.lineno


def _extract_py_header(node, lines: List[str]) -> str:
    """
    Extract the full def/class header text, including any decorator lines.
    The header runs from the first decorator line (or the def/class keyword
    line) up to (but not including) the first body statement line.
    """
    start = _effective_start_line(node) - 1  # convert to 0-indexed
    if node.body and node.body[0].lineno > node.lineno:
        end = node.body[0].lineno - 1  # 0-indexed exclusive
    else:
        # Body begins on the def/class line itself (inline body); include
        # the whole keyword line.
        end = node.lineno
    return "\n".join(lines[start:end]).strip()


# ---------------------------------------------------------------------------
# CodeIndexDb — persistence layer
# ---------------------------------------------------------------------------


class CodeIndexDb:
    """
    Wraps a SQLite connection.  Owns schema application, meta seeding, and
    all upsert/insert helpers used by the scanner.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._apply_schema()
        self._migrate_schema()
        self._seed()

    # ------------------------------------------------------------------
    # Schema application

    def _apply_schema(self) -> None:
        # executescript issues an implicit COMMIT before running, which is
        # fine here — we only apply DDL during init.
        self._conn.executescript(SCHEMA_SQL)

    def _migrate_schema(self) -> None:
        tsv_path = Path(__file__).resolve().with_name("codeintel_migrations.tsv")
        if not tsv_path.exists():
            return
        migrator = SchemaMigrator(self._conn, applied_by=SCANNER_VERSION)
        migrator.migrate(tsv_path)

    # ------------------------------------------------------------------
    # Seed: meta rows + design-decision vocabulary

    def _seed(self) -> None:
        self._seed_meta()
        self._seed_design_decisions()
        self._conn.commit()

    def _seed_meta(self) -> None:
        rows = [
            ("schema_version",                  SCHEMA_VERSION),
            ("scanner_version",                  SCANNER_VERSION),
            ("source_file.file_hash.algorithm",  "sha256"),
            ("scanner_version.format",           "codeintel <semver>"),
        ]
        for key, value in rows:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def _seed_design_decisions(self) -> None:
        """
        Every controlled-vocabulary value used by this implementation is
        documented here as a meta_design_bucket 'decision' row with rationale.
        """
        decisions = [
            # entity_type values
            (
                "entity_type:sql_table",
                "CREATE TABLE statements yield sql_table entities.",
            ),
            (
                "entity_type:sql_view",
                "CREATE VIEW statements yield sql_view entities.",
            ),
            (
                "entity_type:sql_index",
                "CREATE INDEX and CREATE UNIQUE INDEX statements yield "
                "sql_index entities.",
            ),
            (
                "entity_type:sql_column",
                "Column definitions inside CREATE TABLE yield sql_column "
                "entities with qualified_name = "
                "sql_file_prefix.table_name.column_name.",
            ),
            (
                "entity_type:python_module",
                "Each .py file yields one python_module entity spanning "
                "the whole file (line 1 to last line).",
            ),
            (
                "entity_type:python_class",
                "ast.ClassDef directly inside a module body yields "
                "python_class.",
            ),
            (
                "entity_type:python_function",
                "ast.FunctionDef or ast.AsyncFunctionDef directly inside "
                "a module body yields python_function.",
            ),
            (
                "entity_type:python_method",
                "ast.FunctionDef or ast.AsyncFunctionDef directly inside "
                "a class body yields python_method.",
            ),
            (
                "entity_type:python_nested_class",
                "ast.ClassDef nested inside a class or function body yields "
                "python_nested_class.",
            ),
            (
                "entity_type:python_nested_function",
                "ast.FunctionDef or ast.AsyncFunctionDef nested inside a "
                "function or method body yields python_nested_function.",
            ),
            # text_kind values
            (
                "text_kind:signature",
                "The full def/class header (possibly multi-line, from the "
                "keyword to the closing colon) stored as entity_text with "
                "text_kind='signature'.  When the entity is decorated, the "
                "decorator lines are included in the signature.",
            ),
            (
                "text_kind:docstring",
                "The docstring extracted via ast.get_docstring() stored "
                "as entity_text with text_kind='docstring'.",
            ),
            (
                "text_kind:ddl",
                "The full CREATE statement DDL for SQL entities stored "
                "as entity_text with text_kind='ddl'.",
            ),
            (
                "text_kind:source_snippet",
                "Not currently used.  Reserved for future full-body "
                "Python source capture.",
            ),
            # hash_kind values
            (
                "hash_kind:body_sha256",
                "SHA-256 of the entity body (DDL text for SQL; source "
                "lines from start_line to end_line for Python) used as the "
                "primary drift-detection hash.",
            ),
            # detection_method values
            (
                "detection_method:sql_schema_regex",
                "SqlSchemaExtractor detects SQL entities using regex "
                "matching on CREATE TABLE / VIEW / INDEX statements.",
            ),
            (
                "detection_method:python_ast",
                "PythonAstExtractor detects Python entities using the "
                "built-in ast module.",
            ),
            # artifact_kind values
            (
                "artifact_kind:live_db",
                "The live SQLite database file maintained by the scanner "
                "for a branch.  Recorded in branch_artifact on every scan.",
            ),
            # drift_kind values
            (
                "drift_kind:added",
                "Entity was not present in any previous scan for this "
                "branch; this is its first observation.",
            ),
            (
                "drift_kind:changed",
                "Entity existed before but its body_sha256 hash changed "
                "since the previous scan run.",
            ),
            (
                "drift_kind:removed",
                "Entity was observed in a prior scan of the same source "
                "file but was not found during the current scan.",
            ),
            (
                "drift_kind:restored",
                "Entity had lifecycle_status='removed' but was observed "
                "again in a subsequent scan; lifecycle_status is restored "
                "to 'active'.",
            ),
            (
                "extraction_failure:freshness_policy",
                "Failed extraction does not refresh source_file.file_hash "
                "or last_scan_run_id; the file remains stale until "
                "successfully extracted.",
            ),
        ]
        now = _utcnow()
        # Promote any pre-existing 'open' decision rows left over from earlier
        # alpha databases to the current 'accepted' status.
        self._conn.execute(
            "UPDATE meta_design_bucket SET status = 'accepted' "
            "WHERE idea_kind = 'decision' AND status = 'open'"
        )
        for subject, rationale in decisions:
            exists = self._conn.execute(
                "SELECT id FROM meta_design_bucket "
                "WHERE subject = ? AND idea_kind = 'decision'",
                (subject,),
            ).fetchone()
            if exists is None:
                self._conn.execute(
                    "INSERT INTO meta_design_bucket"
                    "(subject, idea_kind, idea_text, status, created_at) "
                    "VALUES (?, 'decision', ?, 'accepted', ?)",
                    (subject, rationale, now),
                )

    # ------------------------------------------------------------------
    # Repo / branch

    def get_or_create_repo(self, repo_slug: str, root_path: str) -> int:
        self._conn.execute(
            "INSERT OR IGNORE INTO git_repo(repo_slug, root_path) "
            "VALUES (?, ?)",
            (repo_slug, root_path),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM git_repo WHERE root_path = ?", (root_path,)
        ).fetchone()
        return row["id"]

    def get_or_create_branch(self, repo_id: int, branch_name: str) -> int:
        self._conn.execute(
            "INSERT OR IGNORE INTO git_branch(repo_id, branch_name) "
            "VALUES (?, ?)",
            (repo_id, branch_name),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM git_branch "
            "WHERE repo_id = ? AND branch_name = ?",
            (repo_id, branch_name),
        ).fetchone()
        return row["id"]

    # ------------------------------------------------------------------
    # Branch artifact

    def upsert_branch_artifact(
        self, branch_id: int, artifact_kind: str, local_path: str
    ) -> int:
        row = self._conn.execute(
            "SELECT id FROM branch_artifact "
            "WHERE branch_id = ? AND artifact_kind = ?",
            (branch_id, artifact_kind),
        ).fetchone()
        if row is None:
            cur = self._conn.execute(
                "INSERT INTO branch_artifact"
                "(branch_id, artifact_kind, local_path) "
                "VALUES (?, ?, ?)",
                (branch_id, artifact_kind, local_path),
            )
            self._conn.commit()
            return cur.lastrowid
        self._conn.execute(
            "UPDATE branch_artifact SET local_path = ? WHERE id = ?",
            (local_path, row["id"]),
        )
        self._conn.commit()
        return row["id"]

    # ------------------------------------------------------------------
    # Scan run

    def create_scan_run(self, branch_id: int) -> int:
        cur = self._conn.execute(
            "INSERT INTO scan_run"
            "(branch_id, started_at, scanner_version, status) "
            "VALUES (?, ?, ?, 'running')",
            (branch_id, _utcnow(), SCANNER_VERSION),
        )
        self._conn.commit()
        return cur.lastrowid

    def finish_scan_run(
        self,
        scan_run_id: int,
        files_scanned: int,
        entities_observed: int,
        drifts_detected: int,
    ) -> None:
        self._conn.execute(
            "UPDATE scan_run "
            "SET finished_at = ?, files_scanned = ?, "
            "    entities_observed = ?, drifts_detected = ?, "
            "    status = 'complete' "
            "WHERE id = ?",
            (
                _utcnow(),
                files_scanned,
                entities_observed,
                drifts_detected,
                scan_run_id,
            ),
        )
        self._conn.commit()

    def fail_scan_run(self, scan_run_id: int) -> None:
        """Mark a scan run as failed (e.g. an exception escaped scanning)."""
        self._conn.execute(
            "UPDATE scan_run "
            "SET finished_at = ?, status = 'failed' "
            "WHERE id = ?",
            (_utcnow(), scan_run_id),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Source file

    def upsert_source_file(
        self,
        branch_id: int,
        file_path: str,
        file_hash: str,
        language: str,
        scan_run_id: int,
    ) -> int:
        self._conn.execute(
            "INSERT INTO source_file"
            "(branch_id, file_path, file_hash, language, last_scan_run_id) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(branch_id, file_path) DO UPDATE SET "
            "  file_hash        = excluded.file_hash, "
            "  language         = excluded.language, "
            "  last_scan_run_id = excluded.last_scan_run_id",
            (branch_id, file_path, file_hash, language, scan_run_id),
        )
        row = self._conn.execute(
            "SELECT id FROM source_file "
            "WHERE branch_id = ? AND file_path = ?",
            (branch_id, file_path),
        ).fetchone()
        return row["id"]

    def get_source_file_id(
        self, branch_id: int, file_path: str
    ) -> Optional[int]:
        """Return the existing source_file ID without modifying any rows."""
        row = self._conn.execute(
            "SELECT id FROM source_file "
            "WHERE branch_id = ? AND file_path = ?",
            (branch_id, file_path),
        ).fetchone()
        return row["id"] if row else None

    # ------------------------------------------------------------------
    # Code entity

    def upsert_entity(
        self,
        branch_id: int,
        entity_type: str,
        name: str,
        qualified_name: str,
        parent_entity_id: Optional[int],
    ) -> Tuple[int, bool, bool]:
        """
        Return (entity_id, is_new, was_restored).
        is_new is True if the entity was just inserted for the first time.
        was_restored is True if the entity was previously 'removed' and is
        now observed again (lifecycle_status set back to 'active').
        """
        row = self._conn.execute(
            "SELECT id, lifecycle_status FROM code_entity "
            "WHERE branch_id = ? AND qualified_name = ?",
            (branch_id, qualified_name),
        ).fetchone()
        if row is None:
            cur = self._conn.execute(
                "INSERT INTO code_entity"
                "(branch_id, parent_entity_id, entity_type, name, "
                " qualified_name, lifecycle_status) "
                "VALUES (?, ?, ?, ?, ?, 'active')",
                (branch_id, parent_entity_id, entity_type, name, qualified_name),
            )
            return cur.lastrowid, True, False
        entity_id = row["id"]
        was_restored = row["lifecycle_status"] == "removed"
        # Refresh metadata on every observation: entity_type, name and parent
        # may all have changed (e.g. def -> class, moved under a new parent).
        # If the entity was previously removed, restore it to active too.
        if was_restored:
            self._conn.execute(
                "UPDATE code_entity "
                "SET entity_type = ?, name = ?, parent_entity_id = ?, "
                "    lifecycle_status = 'active' "
                "WHERE id = ?",
                (entity_type, name, parent_entity_id, entity_id),
            )
        else:
            self._conn.execute(
                "UPDATE code_entity "
                "SET entity_type = ?, name = ?, parent_entity_id = ? "
                "WHERE id = ?",
                (entity_type, name, parent_entity_id, entity_id),
            )
        return entity_id, False, was_restored

    def mark_entity_removed(self, entity_id: int) -> None:
        self._conn.execute(
            "UPDATE code_entity SET lifecycle_status = 'removed' WHERE id = ?",
            (entity_id,),
        )

    # ------------------------------------------------------------------
    # Entity location

    def insert_entity_location(
        self,
        entity_id: int,
        source_file_id: int,
        scan_run_id: int,
        start_line: int,
        end_line: int,
        detection_method: str,
    ) -> None:
        # The unique index ux_entity_location_entity_scan prevents duplicate
        # (entity_id, scan_run_id) pairs; INSERT OR IGNORE is safe here.
        self._conn.execute(
            "INSERT OR IGNORE INTO entity_location"
            "(entity_id, source_file_id, scan_run_id, "
            " start_line, end_line, detection_method) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                entity_id,
                source_file_id,
                scan_run_id,
                start_line,
                end_line,
                detection_method,
            ),
        )

    def get_active_entity_ids_for_file(
        self, source_file_id: int, before_scan_run_id: int
    ) -> List[int]:
        """
        Return IDs of entities that were located in this file in any scan
        run strictly before before_scan_run_id and are still marked active.
        Used to detect entities that disappeared from the file this scan.
        """
        rows = self._conn.execute(
            "SELECT DISTINCT el.entity_id "
            "FROM entity_location el "
            "JOIN code_entity ce ON ce.id = el.entity_id "
            "WHERE el.source_file_id = ? "
            "  AND el.scan_run_id < ? "
            "  AND ce.lifecycle_status = 'active'",
            (source_file_id, before_scan_run_id),
        ).fetchall()
        return [r["entity_id"] for r in rows]

    # ------------------------------------------------------------------
    # Entity text

    def upsert_entity_text(
        self, entity_id: int, text_kind: str, text_body: str
    ) -> None:
        row = self._conn.execute(
            "SELECT id FROM entity_text "
            "WHERE entity_id = ? AND text_kind = ?",
            (entity_id, text_kind),
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO entity_text(entity_id, text_kind, text_body) "
                "VALUES (?, ?, ?)",
                (entity_id, text_kind, text_body),
            )
        else:
            self._conn.execute(
                "UPDATE entity_text SET text_body = ? WHERE id = ?",
                (text_body, row["id"]),
            )

    # ------------------------------------------------------------------
    # Entity hash

    def insert_entity_hash(
        self,
        entity_id: int,
        scan_run_id: int,
        hash_kind: str,
        hash_value: str,
    ) -> None:
        # The unique index ux_entity_hash_entity_scan_kind prevents duplicates.
        self._conn.execute(
            "INSERT OR IGNORE INTO entity_hash"
            "(entity_id, scan_run_id, hash_kind, hash_value) "
            "VALUES (?, ?, ?, ?)",
            (entity_id, scan_run_id, hash_kind, hash_value),
        )

    def get_latest_entity_hash(
        self, entity_id: int, hash_kind: str
    ) -> Optional[str]:
        row = self._conn.execute(
            "SELECT hash_value FROM entity_hash "
            "WHERE entity_id = ? AND hash_kind = ? "
            "ORDER BY scan_run_id DESC, id DESC "
            "LIMIT 1",
            (entity_id, hash_kind),
        ).fetchone()
        return row["hash_value"] if row else None

    # ------------------------------------------------------------------
    # Drift event

    def insert_drift_event(
        self,
        entity_id: int,
        scan_run_id: int,
        drift_kind: str,
        old_hash: Optional[str],
        new_hash: Optional[str],
    ) -> None:
        self._conn.execute(
            "INSERT INTO drift_event"
            "(entity_id, scan_run_id, drift_kind, status, old_hash, new_hash) "
            "VALUES (?, ?, ?, 'open', ?, ?)",
            (entity_id, scan_run_id, drift_kind, old_hash, new_hash),
        )

    # ------------------------------------------------------------------
    # Call observations

    def insert_call_observation(
        self,
        caller_entity_id: int,
        source_file_id: int,
        scan_run_id: int,
        call_text: str,
        call_kind: str,
        start_line: int,
        end_line: int,
        confidence: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO call_observation"
            "(caller_entity_id, source_file_id, scan_run_id,"
            " call_text, call_kind, start_line, end_line, confidence)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                caller_entity_id,
                source_file_id,
                scan_run_id,
                call_text,
                call_kind,
                start_line,
                end_line,
                confidence,
            ),
        )

    def insert_symbol_observation(
        self,
        owner_entity_id: int,
        source_file_id: int,
        scan_run_id: int,
        symbol_name: str,
        access_kind: str,
        scope_hint: str,
        start_line: int,
        end_line: int,
        confidence: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO symbol_observation"
            "(owner_entity_id, source_file_id, scan_run_id,"
            " symbol_name, access_kind, scope_hint,"
            " start_line, end_line, confidence)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                owner_entity_id,
                source_file_id,
                scan_run_id,
                symbol_name,
                access_kind,
                scope_hint,
                start_line,
                end_line,
                confidence,
            ),
        )

    def drift_event_exists(self, drift_event_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM drift_event WHERE id = ?",
            (drift_event_id,),
        ).fetchone()
        return row is not None

    def update_drift_resolution(
        self,
        drift_event_id: int,
        *,
        status: str,
        by: str,
        note: str,
        resolution_kind: str,
    ) -> None:
        self._conn.execute(
            "UPDATE drift_event "
            "SET status = ?, acknowledged_at = ?, acknowledged_by = ?, "
            "    acknowledgement_note = ?, resolution_kind = ? "
            "WHERE id = ?",
            (status, _utcnow(), by, note, resolution_kind, drift_event_id),
        )

    # ------------------------------------------------------------------
    # Source file queries for stale / vanished-file detection

    def get_source_files_for_stale(self) -> List[sqlite3.Row]:
        """Return source files joined with their repo root_path for stale checking."""
        return self._conn.execute(
            "SELECT sf.file_path, sf.file_hash, gr.root_path "
            "FROM source_file sf "
            "JOIN git_branch gb ON gb.id = sf.branch_id "
            "JOIN git_repo gr ON gr.id = gb.repo_id "
            "ORDER BY sf.file_path"
        ).fetchall()

    def get_source_file_ids_for_branch_under_prefix(
        self, branch_id: int, prefix: str
    ) -> List[int]:
        """
        Return source_file IDs for branch_id whose file_path starts with prefix.
        Use prefix='' to match all files in the branch.
        """
        if not prefix:
            rows = self._conn.execute(
                "SELECT id FROM source_file WHERE branch_id = ?",
                (branch_id,),
            ).fetchall()
        else:
            escaped = _like_escape(prefix)
            rows = self._conn.execute(
                "SELECT id FROM source_file "
                "WHERE branch_id = ? AND file_path LIKE ? ESCAPE '\\'",
                (branch_id, escaped + "%"),
            ).fetchall()
        return [r["id"] for r in rows]

    # ------------------------------------------------------------------
    # Transaction control

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Generic read helper used by CLI commands

    def query(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# LanguageExtractor — plug-in seam
# ---------------------------------------------------------------------------


class LanguageExtractor:
    """
    Base class for language-specific extractors.

    Subclasses must set class attributes language_name and detection_method,
    and implement supports_path() and extract_file().

    extract_file() returns a list of EntityRecord dicts with keys:
        entity_type           str
        name                  str
        qualified_name        str
        parent_qualified_name Optional[str]
        start_line            int
        end_line              int
        detection_method      str
        body_text             str   — hashed to produce body_sha256
        texts                 list of (text_kind: str, text_body: str)

    A return value of [] means extraction succeeded but found no entities.
    A return value of None means extraction FAILED (e.g. read error or
    syntax error); the scanner must not infer removals for that file.
    """

    language_name: str = ""
    detection_method: str = ""

    def supports_path(self, path: Path) -> bool:
        raise NotImplementedError

    def extract_file(self, path: Path, repo_root: Path) -> Optional[List[Dict]]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# SqlSchemaExtractor
# ---------------------------------------------------------------------------


class SqlSchemaExtractor(LanguageExtractor):
    """
    Lightweight SQLite DDL orientation extractor, not a general SQL parser.

    Extracts sql_table, sql_view, sql_index, and sql_column entities from
    .sql files using a line-by-line state machine with regex matching on
    CREATE TABLE / VIEW / INDEX statements.

    Supported constructs:
        CREATE TABLE <name> (...)  → sql_table + sql_column entities
        CREATE VIEW <name> AS ...  → sql_view entity
        CREATE [UNIQUE] INDEX <name> ON <table> (...) → sql_index entity

    Unsupported constructs (silently ignored, not modelled):
        triggers, stored procedures, DML, complex generated columns,
        vendor-specific dialects.

    Agents must read SQL source directly before editing or reasoning about
    complete SQL semantics.

    detection_method = 'sql_schema_regex'
    """

    language_name = "sql"
    detection_method = "sql_schema_regex"

    def supports_path(self, path: Path) -> bool:
        return path.suffix.lower() == ".sql"

    def extract_file(self, path: Path, repo_root: Path) -> Optional[List[Dict]]:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

        lines = content.splitlines()
        records: List[Dict] = []
        # File-path-aware prefix disambiguates same-named objects across files
        # (e.g. schema.sql:code_entity -> schema.code_entity).
        prefix = _sql_qname_prefix(path, repo_root)

        # State machine state
        state = "idle"            # 'idle' | 'table' | 'semi'
        obj_kind: Optional[str] = None
        obj_name: Optional[str] = None
        obj_parent: Optional[str] = None   # table name for indexes/columns
        obj_start: int = 0
        obj_lines: List[str] = []
        paren_depth: int = 0

        def _finalize(end_lineno: int) -> None:
            nonlocal state, obj_kind, obj_name, obj_parent
            nonlocal obj_start, obj_lines, paren_depth

            ddl = "\n".join(obj_lines).strip()

            # For indexes where ON table_name appeared on a continuation line,
            # scan the collected DDL now.
            if obj_kind == "sql_index" and obj_parent is None:
                m = re.search(r"\bON\s+(\w+)", ddl, re.IGNORECASE)
                if m:
                    obj_parent = m.group(1)

            qualified_name = f"{prefix}.{obj_name}"
            parent_qname = f"{prefix}.{obj_parent}" if obj_parent else None

            rec: Dict = {
                "entity_type": obj_kind,
                "name": obj_name,
                "qualified_name": qualified_name,
                "parent_qualified_name": parent_qname,
                "start_line": obj_start,
                "end_line": end_lineno,
                "detection_method": "sql_schema_regex",
                "body_text": ddl,
                "texts": [("ddl", ddl)],
            }
            records.append(rec)

            # For tables: extract column definitions from the body lines.
            # Skip line 0 (the CREATE TABLE line itself).
            if obj_kind == "sql_table":
                for i, col_line in enumerate(obj_lines[1:], start=1):
                    col_name = _is_column_def(col_line)
                    if col_name:
                        col_ddl = col_line.strip()
                        records.append(
                            {
                                "entity_type": "sql_column",
                                "name": col_name,
                                "qualified_name": f"{qualified_name}.{col_name}",
                                "parent_qualified_name": qualified_name,
                                "start_line": obj_start + i,
                                "end_line": obj_start + i,
                                "detection_method": "sql_schema_regex",
                                "body_text": col_ddl,
                                "texts": [],
                            }
                        )

            # Reset state
            state = "idle"
            obj_kind = obj_name = obj_parent = None
            obj_start = 0
            obj_lines = []
            paren_depth = 0

        for lineno, raw_line in enumerate(lines, start=1):
            stripped = raw_line.strip()

            if state == "idle":
                if not stripped or stripped.startswith("--"):
                    continue

                # CREATE TABLE
                m = re.match(
                    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
                    stripped,
                    re.IGNORECASE,
                )
                if m:
                    state = "table"
                    obj_kind = "sql_table"
                    obj_name = m.group(1)
                    obj_parent = None
                    obj_start = lineno
                    obj_lines = [raw_line]
                    paren_depth = raw_line.count("(") - raw_line.count(")")
                    continue

                # CREATE VIEW
                m = re.match(
                    r"CREATE\s+VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
                    stripped,
                    re.IGNORECASE,
                )
                if m:
                    state = "semi"
                    obj_kind = "sql_view"
                    obj_name = m.group(1)
                    obj_parent = None
                    obj_start = lineno
                    obj_lines = [raw_line]
                    if ";" in raw_line:
                        _finalize(lineno)
                    continue

                # CREATE [UNIQUE] INDEX
                m = re.match(
                    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
                    stripped,
                    re.IGNORECASE,
                )
                if m:
                    on_m = re.search(r"\bON\s+(\w+)", stripped, re.IGNORECASE)
                    state = "semi"
                    obj_kind = "sql_index"
                    obj_name = m.group(1)
                    obj_parent = on_m.group(1) if on_m else None
                    obj_start = lineno
                    obj_lines = [raw_line]
                    if ";" in raw_line:
                        _finalize(lineno)
                    continue

            elif state == "table":
                obj_lines.append(raw_line)
                paren_depth += raw_line.count("(") - raw_line.count(")")
                if paren_depth <= 0:
                    _finalize(lineno)

            elif state == "semi":
                obj_lines.append(raw_line)
                # Resolve ON table for multi-line index definitions
                if obj_kind == "sql_index" and obj_parent is None:
                    on_m = re.search(r"\bON\s+(\w+)", stripped, re.IGNORECASE)
                    if on_m:
                        obj_parent = on_m.group(1)
                if ";" in raw_line:
                    _finalize(lineno)

        return records


# ---------------------------------------------------------------------------
# PythonAstExtractor
# ---------------------------------------------------------------------------


class PythonAstExtractor(LanguageExtractor):
    """
    Extract python_module, python_class, python_function, python_method,
    python_nested_class, and python_nested_function entities from .py files
    using the built-in ast module.

    detection_method = 'python_ast'

    Entity-type assignment rules:
        ClassDef  at module scope           → python_class
        ClassDef  inside class or function  → python_nested_class
        FunctionDef at module scope         → python_function
        FunctionDef inside class            → python_method
        FunctionDef inside function/method  → python_nested_function
    """

    language_name = "python"
    detection_method = "python_ast"

    def supports_path(self, path: Path) -> bool:
        return path.suffix.lower() == ".py"

    def extract_file(self, path: Path, repo_root: Path) -> Optional[List[Dict]]:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

        lines = content.splitlines()
        try:
            tree = ast.parse(content, filename=str(path))
        except SyntaxError:
            return None

        records: List[Dict] = []
        module_qname = _py_module_qname(path, repo_root)
        module_name = module_qname.rsplit(".", 1)[-1] if module_qname else path.stem

        # Module entity — spans the entire file
        module_texts: List[Tuple[str, str]] = []
        module_doc = ast.get_docstring(tree)
        if module_doc:
            module_texts.append(("docstring", module_doc))

        records.append(
            {
                "entity_type": "python_module",
                "name": module_name,
                "qualified_name": module_qname,
                "parent_qualified_name": None,
                "start_line": 1,
                "end_line": len(lines) or 1,
                "detection_method": "python_ast",
                "body_text": content,
                "texts": module_texts,
            }
        )

        self._walk(tree.body, module_qname, module_qname, "module", lines, records)
        return records

    @staticmethod
    def _child_stmts(node: ast.AST) -> List[list]:
        """
        Return all direct child statement-lists reachable from a transparent
        container node (if, for, while, with, try, except, finally, else,
        match/case, etc.).

        ClassDef and FunctionDef bodies are handled by the visitor methods;
        this helper is only called for everything else.
        """
        result: List[list] = []
        for _name, value in ast.iter_fields(node):
            if not isinstance(value, list) or not value:
                continue
            first = value[0]
            if isinstance(first, ast.stmt):
                # Direct statement list: if.body, for.body, try.body,
                # try.orelse, try.finalbody, while.body, with.body, etc.
                result.append(value)
            elif isinstance(first, ast.AST) and hasattr(first, "body"):
                # Handler/case lists whose elements carry their own body:
                # try.handlers (ExceptHandler), match.cases (match_case).
                for item in value:
                    body = getattr(item, "body", None)
                    if body:
                        result.append(body)
        return result

    @staticmethod
    def _calls_in_body(stmts: list) -> List[Tuple[str, str, int, int]]:
        """
        Collect all call expressions in stmts as (call_text, call_kind,
        start_line, end_line) tuples.

        Traverses transparent containers (if, for, while, with, try, etc.)
        but stops at nested ClassDef/FunctionDef/AsyncFunctionDef boundaries
        so calls inside nested entities are not double-counted under the
        enclosing entity.
        """
        result: List[Tuple[str, str, int, int]] = []
        worklist: list = list(stmts)
        while worklist:
            node = worklist.pop()
            if isinstance(
                node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            if isinstance(node, ast.Call):
                func = node.func
                is_super_call = False
                if isinstance(func, ast.Name):
                    text, kind = func.id, "name_call"
                elif isinstance(func, ast.Attribute):
                    attr = func.attr
                    val = func.value
                    if (
                        isinstance(val, ast.Call)
                        and isinstance(val.func, ast.Name)
                        and val.func.id == "super"
                    ):
                        text, kind = f"super.{attr}", "super_call"
                        is_super_call = True
                    elif isinstance(val, ast.Name) and val.id == "self":
                        text, kind = f"self.{attr}", "self_call"
                    elif isinstance(val, ast.Name):
                        text, kind = f"{val.id}.{attr}", "attribute_call"
                    else:
                        text, kind = f"<expr>.{attr}", "attribute_call"
                else:
                    text, kind = "<unknown>", "unknown_call"
                result.append((
                    text,
                    kind,
                    node.lineno,
                    getattr(node, "end_lineno", node.lineno),
                ))
                if is_super_call:
                    # Traverse call arguments but skip func.value (the inner
                    # super() sub-call) to avoid emitting a redundant bare
                    # name_call for 'super' alongside the super_call.
                    worklist.extend(node.args)
                    for kw in node.keywords:
                        worklist.append(kw.value)
                else:
                    worklist.extend(ast.iter_child_nodes(node))
            else:
                worklist.extend(ast.iter_child_nodes(node))
        return result

    def _walk(
        self,
        body: list,
        module_qname: str,
        parent_qname: str,
        context: str,
        lines: List[str],
        records: List[Dict],
    ) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                self._visit_class(
                    node, module_qname, parent_qname, context, lines, records
                )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._visit_function(
                    node, module_qname, parent_qname, context, lines, records
                )
            else:
                # Transparent container (if, for, while, with, try, match,
                # etc.) — descend with the same parent_qname and context so
                # any definitions found inside are attributed to the correct
                # enclosing entity, not to the control-flow construct itself.
                for child_body in self._child_stmts(node):
                    self._walk(
                        child_body,
                        module_qname,
                        parent_qname,
                        context,
                        lines,
                        records,
                    )

    def _visit_class(
        self,
        node: ast.ClassDef,
        module_qname: str,
        parent_qname: str,
        context: str,
        lines: List[str],
        records: List[Dict],
    ) -> None:
        qname = f"{parent_qname}.{node.name}"
        entity_type = (
            "python_class" if context == "module" else "python_nested_class"
        )
        start_line = _effective_start_line(node)
        end_line = getattr(node, "end_lineno", node.lineno)
        body_text = "\n".join(lines[start_line - 1 : end_line])
        signature = _extract_py_header(node, lines)

        texts: List[Tuple[str, str]] = [("signature", signature)]
        docstring = ast.get_docstring(node)
        if docstring:
            texts.append(("docstring", docstring))

        records.append(
            {
                "entity_type": entity_type,
                "name": node.name,
                "qualified_name": qname,
                "parent_qualified_name": parent_qname,
                "start_line": start_line,
                "end_line": end_line,
                "detection_method": "python_ast",
                "body_text": body_text,
                "texts": texts,
            }
        )

        # Recurse into the class body; members of a class have context='class'
        self._walk(node.body, module_qname, qname, "class", lines, records)

    def _visit_function(
        self,
        node,  # ast.FunctionDef or ast.AsyncFunctionDef
        module_qname: str,
        parent_qname: str,
        context: str,
        lines: List[str],
        records: List[Dict],
    ) -> None:
        qname = f"{parent_qname}.{node.name}"
        if context == "class":
            entity_type = "python_method"
        elif context == "function":
            entity_type = "python_nested_function"
        else:
            entity_type = "python_function"

        start_line = _effective_start_line(node)
        end_line = getattr(node, "end_lineno", node.lineno)
        body_text = "\n".join(lines[start_line - 1 : end_line])
        signature = _extract_py_header(node, lines)

        texts: List[Tuple[str, str]] = [("signature", signature)]
        docstring = ast.get_docstring(node)
        if docstring:
            texts.append(("docstring", docstring))

        calls = [
            {
                "call_text": ct,
                "call_kind": ck,
                "start_line": sl,
                "end_line": el,
                "confidence": "observed",
            }
            for ct, ck, sl, el in self._calls_in_body(node.body)
        ]

        symbols = [
            {
                "symbol_name": sn,
                "access_kind": ak,
                "scope_hint": sh,
                "start_line": sl,
                "end_line": el,
                "confidence": "observed",
            }
            for sn, ak, sh, sl, el in self._collect_symbol_observations(node)
        ]

        records.append(
            {
                "entity_type": entity_type,
                "name": node.name,
                "qualified_name": qname,
                "parent_qualified_name": parent_qname,
                "start_line": start_line,
                "end_line": end_line,
                "detection_method": "python_ast",
                "body_text": body_text,
                "texts": texts,
                "calls": calls,
                "symbols": symbols,
            }
        )

        # Recurse into the function body; nested items have context='function'
        self._walk(node.body, module_qname, qname, "function", lines, records)

    @staticmethod
    def _collect_symbol_observations(
        node,  # ast.FunctionDef or ast.AsyncFunctionDef
    ) -> List[Tuple[str, str, str, int, int]]:
        """
        Collect external/interface symbol observations for a single
        function-like AST node.  Returns a list of
        (symbol_name, access_kind, scope_hint, start_line, end_line).

        Only function-like entities are scoped here; class/module bodies are
        not processed.  Nested function/class definitions are skipped so each
        nested entity collects its own observations without double-counting.

        Persists: params, global/nonlocal decls, import bindings, reads of
        externally-unbound names, writes/deletes to declared global/nonlocal.
        Does NOT persist: ordinary local assignments, loop targets, with-as
        locals, except-handler locals, or their reads.
        """
        result: List[Tuple[str, str, str, int, int]] = []

        # --- Pass 1: collect local binders (used only for noise suppression)
        # These are names that are purely local to this function body and
        # should NOT be persisted as external_read observations.
        local_binders: set = set()
        # Parameters — also persisted; added separately
        global_decls: set = set()
        nonlocal_decls: set = set()
        import_bindings: set = set()

        # Helper: extract simple Name targets from an assignment target tree
        def _names_in_target(t) -> List[str]:
            if isinstance(t, ast.Name):
                return [t.id]
            if isinstance(t, (ast.Tuple, ast.List)):
                names: List[str] = []
                for elt in t.elts:
                    names.extend(_names_in_target(elt))
                return names
            return []

        # Parameters
        params = node.args
        for arg in (
            list(params.args)
            + list(params.posonlyargs)
            + list(params.kwonlyargs)
            + ([params.vararg] if params.vararg else [])
            + ([params.kwarg] if params.kwarg else [])
        ):
            local_binders.add(arg.arg)

        # Walk the direct body (not descending into nested funcs/classes)
        worklist_pass1: list = list(node.body)
        while worklist_pass1:
            n = worklist_pass1.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # nested entity boundary
            if isinstance(n, ast.Global):
                global_decls.update(n.names)
            elif isinstance(n, ast.Nonlocal):
                nonlocal_decls.update(n.names)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for alias in n.names:
                    bound_name = alias.asname if alias.asname else (
                        alias.name.split(".")[0]
                    )
                    import_bindings.add(bound_name)
            elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                # Collect assignment targets for noise suppression.
                # Only add to local_binders when NOT global/nonlocal —
                # those are checked after both passes.
                if isinstance(n, ast.Assign):
                    for tgt in n.targets:
                        for name in _names_in_target(tgt):
                            local_binders.add(name)
                elif isinstance(n, ast.AnnAssign):
                    for name in _names_in_target(n.target):
                        local_binders.add(name)
                elif isinstance(n, ast.AugAssign):
                    for name in _names_in_target(n.target):
                        local_binders.add(name)
            elif isinstance(n, ast.For):
                for name in _names_in_target(n.target):
                    local_binders.add(name)
                worklist_pass1.extend(n.body)
                worklist_pass1.extend(n.orelse)
                continue
            elif isinstance(n, ast.With):
                for item in n.items:
                    if item.optional_vars is not None:
                        for name in _names_in_target(item.optional_vars):
                            local_binders.add(name)
                worklist_pass1.extend(n.body)
                continue
            elif isinstance(n, ast.ExceptHandler):
                if n.name:
                    local_binders.add(n.name)
            # Comprehension targets
            elif isinstance(n, (ast.ListComp, ast.SetComp, ast.GeneratorExp,
                                 ast.DictComp)):
                continue  # skip comprehension internals entirely
            worklist_pass1.extend(ast.iter_child_nodes(n))

        # global/nonlocal names are never treated as pure locals
        local_binders -= global_decls
        local_binders -= nonlocal_decls
        # import bindings are local but handled separately
        local_binders -= import_bindings

        # --- Pass 2: emit observations
        # Parameters → param
        for arg in (
            list(params.args)
            + list(params.posonlyargs)
            + list(params.kwonlyargs)
            + ([params.vararg] if params.vararg else [])
            + ([params.kwarg] if params.kwarg else [])
        ):
            result.append((
                arg.arg, "param", "param",
                arg.lineno, getattr(arg, "end_lineno", arg.lineno),
            ))

        # Body walk for declarations, imports, reads, and global/nonlocal
        # writes/deletes
        worklist_pass2: list = list(node.body)
        while worklist_pass2:
            n = worklist_pass2.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # nested entity boundary

            if isinstance(n, ast.Global):
                for name in n.names:
                    result.append((
                        name, "global_decl", "global",
                        n.lineno, getattr(n, "end_lineno", n.lineno),
                    ))
                # don't recurse — no child nodes of interest on Global
                continue

            if isinstance(n, ast.Nonlocal):
                for name in n.names:
                    result.append((
                        name, "nonlocal_decl", "nonlocal",
                        n.lineno, getattr(n, "end_lineno", n.lineno),
                    ))
                continue

            if isinstance(n, (ast.Import, ast.ImportFrom)):
                for alias in n.names:
                    bound_name = alias.asname if alias.asname else (
                        alias.name.split(".")[0]
                    )
                    result.append((
                        bound_name, "import_write", "import",
                        n.lineno, getattr(n, "end_lineno", n.lineno),
                    ))
                continue  # don't walk into import sub-nodes

            if isinstance(n, (ast.Assign, ast.AugAssign)):
                # Only persist writes to global/nonlocal targets
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                for tgt in targets:
                    if isinstance(tgt, ast.Name):
                        nm = tgt.id
                        if nm in global_decls:
                            result.append((
                                nm, "global_write", "global",
                                n.lineno, getattr(n, "end_lineno", n.lineno),
                            ))
                        elif nm in nonlocal_decls:
                            result.append((
                                nm, "nonlocal_write", "nonlocal",
                                n.lineno, getattr(n, "end_lineno", n.lineno),
                            ))
                # Fall through to recurse on the value expression

            if isinstance(n, ast.Delete):
                for tgt in n.targets:
                    if isinstance(tgt, ast.Name):
                        nm = tgt.id
                        if nm in global_decls:
                            result.append((
                                nm, "global_delete", "global",
                                n.lineno, getattr(n, "end_lineno", n.lineno),
                            ))
                        elif nm in nonlocal_decls:
                            result.append((
                                nm, "nonlocal_delete", "nonlocal",
                                n.lineno, getattr(n, "end_lineno", n.lineno),
                            ))

            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                nm = n.id
                # Skip names that are purely local binders or import bindings
                if nm in local_binders or nm in import_bindings:
                    pass  # local — suppress
                elif nm in global_decls:
                    result.append((
                        nm, "external_read", "global",
                        n.lineno, getattr(n, "end_lineno", n.lineno),
                    ))
                elif nm in nonlocal_decls:
                    result.append((
                        nm, "external_read", "nonlocal",
                        n.lineno, getattr(n, "end_lineno", n.lineno),
                    ))
                else:
                    result.append((
                        nm, "external_read", "external_or_unknown",
                        n.lineno, getattr(n, "end_lineno", n.lineno),
                    ))
                continue  # Name has no child nodes worth visiting

            worklist_pass2.extend(ast.iter_child_nodes(n))

        return result


class CodeScanner:
    """
    Walks a path, dispatches each file to the first matching LanguageExtractor,
    and persists all observations (locations, hashes, texts, drift events) to
    CodeIndexDb.
    """

    def __init__(
        self,
        db: CodeIndexDb,
        branch_id: int,
        extractors: List[LanguageExtractor],
        repo_root: Path,
    ) -> None:
        self.db = db
        self.branch_id = branch_id
        self.extractors = extractors
        self.repo_root = repo_root

    def _find_extractor(self, path: Path) -> Optional[LanguageExtractor]:
        for ext in self.extractors:
            if ext.supports_path(path):
                return ext
        return None

    def scan(self, path: Path) -> Tuple[int, int, int, int]:
        """
        Scan path (file or directory tree).
        Returns (scan_run_id, files_scanned, entities_observed, drifts_detected).
        If an exception escapes mid-scan, the scan_run is marked 'failed'
        rather than being left 'running'.
        """
        scan_run_id = self.db.create_scan_run(self.branch_id)
        try:
            return self._scan_body(path, scan_run_id)
        except Exception:
            self.db.fail_scan_run(scan_run_id)
            raise

    def _scan_body(
        self, path: Path, scan_run_id: int
    ) -> Tuple[int, int, int, int]:
        db = self.db
        branch_id = self.branch_id

        # Record this DB as the live branch artifact on every scan.
        db.upsert_branch_artifact(
            branch_id, "live_db", self.db.db_path.as_posix()
        )

        files_scanned = 0
        entities_observed = 0
        drifts_detected = 0

        candidates = (
            [path] if path.is_file()
            else _collect_files(path)
        )

        # Track source_file IDs touched this run (for vanished-file detection).
        scanned_file_ids: set = set()

        for file_path in candidates:
            extractor = self._find_extractor(file_path)
            if extractor is None:
                continue

            files_scanned += 1
            rel_path = _repo_relative_path(file_path, self.repo_root)
            file_hash = _hash_file(file_path)

            # Extract first.  Only update source_file freshness metadata when
            # extraction succeeds.  A failed extraction (returns None) must not
            # refresh file_hash or last_scan_run_id — the file should stay
            # stale so a future scan attempt is not silently skipped.
            records = extractor.extract_file(file_path, self.repo_root)
            if records is None:
                print(
                    f"warning: extraction failed, skipping freshness update: "
                    f"{rel_path}",
                    file=sys.stderr,
                )
                # If a previous successful scan created a source_file row,
                # preserve its ID so the vanished-file detector below does not
                # mistake this file for one that was deleted from disk.
                existing_sf_id = db.get_source_file_id(branch_id, rel_path)
                if existing_sf_id is not None:
                    scanned_file_ids.add(existing_sf_id)
                continue

            source_file_id = db.upsert_source_file(
                branch_id,
                rel_path,
                file_hash,
                extractor.language_name,
                scan_run_id,
            )
            scanned_file_ids.add(source_file_id)

            # Map qualified_name → entity_id so child records can resolve
            # their parent_entity_id in a single forward pass.
            qname_to_id: Dict[str, int] = {}
            seen_ids: set = set()

            for rec in records:
                pqname = rec.get("parent_qualified_name")
                parent_entity_id = qname_to_id.get(pqname) if pqname else None

                entity_id, is_new, was_restored = db.upsert_entity(
                    branch_id,
                    rec["entity_type"],
                    rec["name"],
                    rec["qualified_name"],
                    parent_entity_id,
                )
                qname_to_id[rec["qualified_name"]] = entity_id
                seen_ids.add(entity_id)
                entities_observed += 1

                db.insert_entity_location(
                    entity_id,
                    source_file_id,
                    scan_run_id,
                    rec["start_line"],
                    rec["end_line"],
                    rec["detection_method"],
                )

                for call in rec.get("calls", []):
                    db.insert_call_observation(
                        entity_id,
                        source_file_id,
                        scan_run_id,
                        call["call_text"],
                        call["call_kind"],
                        call["start_line"],
                        call["end_line"],
                        call["confidence"],
                    )

                for sym in rec.get("symbols", []):
                    db.insert_symbol_observation(
                        entity_id,
                        source_file_id,
                        scan_run_id,
                        sym["symbol_name"],
                        sym["access_kind"],
                        sym["scope_hint"],
                        sym["start_line"],
                        sym["end_line"],
                        sym["confidence"],
                    )

                for text_kind, text_body in rec.get("texts", []):
                    if text_body:
                        db.upsert_entity_text(entity_id, text_kind, text_body)

                new_hash = _hash_text(rec["body_text"])
                prev_hash = db.get_latest_entity_hash(entity_id, "body_sha256")
                db.insert_entity_hash(entity_id, scan_run_id, "body_sha256", new_hash)

                if prev_hash is None:
                    # First observation — always 'added'
                    db.insert_drift_event(
                        entity_id, scan_run_id, "added", None, new_hash
                    )
                    drifts_detected += 1
                elif was_restored:
                    # Entity was removed in a prior scan and is observed again.
                    db.insert_drift_event(
                        entity_id, scan_run_id, "restored", prev_hash, new_hash
                    )
                    drifts_detected += 1
                elif prev_hash != new_hash:
                    db.insert_drift_event(
                        entity_id, scan_run_id, "changed", prev_hash, new_hash
                    )
                    drifts_detected += 1

            # Detect entities that vanished from this file since the last scan.
            prev_ids = db.get_active_entity_ids_for_file(source_file_id, scan_run_id)
            for entity_id in prev_ids:
                if entity_id not in seen_ids:
                    prev_hash = db.get_latest_entity_hash(entity_id, "body_sha256")
                    db.mark_entity_removed(entity_id)
                    db.insert_drift_event(
                        entity_id, scan_run_id, "removed", prev_hash, None
                    )
                    drifts_detected += 1

            db.commit()

        # For directory scans: detect previously scanned files that no longer
        # exist on disk under the scan path.
        if path.is_dir():
            if path.resolve() == self.repo_root.resolve():
                prefix = ""
            else:
                prefix = _repo_relative_path(path, self.repo_root).rstrip("/") + "/"
            prev_file_ids = db.get_source_file_ids_for_branch_under_prefix(
                branch_id, prefix
            )
            for sf_id in prev_file_ids:
                if sf_id not in scanned_file_ids:
                    prev_active_ids = db.get_active_entity_ids_for_file(
                        sf_id, scan_run_id
                    )
                    for entity_id in prev_active_ids:
                        prev_hash = db.get_latest_entity_hash(
                            entity_id, "body_sha256"
                        )
                        db.mark_entity_removed(entity_id)
                        db.insert_drift_event(
                            entity_id, scan_run_id, "removed", prev_hash, None
                        )
                        drifts_detected += 1
            db.commit()

        db.finish_scan_run(scan_run_id, files_scanned, entities_observed, drifts_detected)
        return scan_run_id, files_scanned, entities_observed, drifts_detected


# ---------------------------------------------------------------------------
# CodeIntelDbUnionConnection
# ---------------------------------------------------------------------------


class CodeIntelDbUnionConnection:
    """
    A read-only, multi-repo union connection over one or more codeintel
    SQLite databases.

    Public API resembles a database connection::

        conn = CodeIntelDbUnionConnection(repos)
        conn.execute(sql, params)   # returns self
        conn.fetchall()             # returns list[dict]
        conn.query(sql, params)     # execute + fetchall
        conn.close()                # lifecycle hook
        conn.diagnose()             # repo/db status information
        conn.cursor()               # raises NotImplementedError

    Each element of ``repos`` is a dict with string keys:

        name     — human-readable repo name
        root     — repo root path (str or Path)
        db_path  — path to the repo's SQLite database file

    Repo metadata is automatically injected as named parameters into every
    query.  User SQL may reference them as ``:__repo_name``,
    ``:__repo_root``, and ``:__repo_db_path``.  Params must be a dict
    (not a positional tuple) to reference these names.

    SQL support
    -----------
    SELECT and WITH are accepted.  The following are rejected:

        INSERT, UPDATE, DELETE, REPLACE, CREATE, DROP, ALTER,
        VACUUM, PRAGMA, ATTACH, DETACH, multiple statements.

    This is a limitation of the current execution engine, not necessarily
    a limitation of the future API.

    Current implementation
    ----------------------
    - Read-only (URI ``mode=ro`` + readonly authorizer).
    - One repo DB opened per query; rows merged in Python.

    Future implementations may support ATTACH batching, materialised
    unions, caches, and richer SQL.
    """

    _REJECT_KW_RE = re.compile(
        r"^\s*(?:INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER"
        r"|VACUUM|PRAGMA|ATTACH|DETACH)\b",
        re.IGNORECASE,
    )
    # A semicolon followed by any non-whitespace signals a second statement.
    _MULTI_STMT_RE = re.compile(r";\s*\S")

    def __init__(self, repos: List[Dict[str, Any]]) -> None:
        self._repos: List[Dict[str, Any]] = list(repos)
        self._last_rows: List[Dict] = []

    # ------------------------------------------------------------------
    # Public API

    def execute(self, sql: str, params=None) -> "CodeIntelDbUnionConnection":
        """Execute sql against all repos; store merged rows; return self."""
        self._assert_supported_sql(sql)
        self._last_rows = self._execute_map_reduce_select(sql, params)
        return self

    def fetchall(self) -> List[Dict]:
        """Return rows from the last execute() call."""
        return self._last_rows

    def query(self, sql: str, params=None) -> List[Dict]:
        """Execute sql and return rows (execute + fetchall)."""
        return self.execute(sql, params).fetchall()

    def close(self) -> None:
        """Lifecycle hook; releases no resources in the current implementation."""

    def diagnose(self) -> Dict[str, Any]:
        """Return per-repo status information (name, root, db_path, db_exists, db_size)."""
        entries: List[Dict[str, Any]] = []
        for repo in self._repos:
            db_path = self._repo_value(repo, "db_path")
            p = Path(db_path) if db_path else None
            db_exists = bool(p and p.exists())
            entries.append(
                {
                    "name":      self._repo_value(repo, "name"),
                    "root":      self._repo_value(repo, "root"),
                    "db_path":   db_path,
                    "db_exists": db_exists,
                    "db_size":   p.stat().st_size if db_exists and p else None,
                }
            )
        return {"repos": entries}

    def cursor(self):
        """Not supported; use execute(), fetchall(), or query() instead."""
        raise NotImplementedError(
            "CodeIntelDbUnionConnection does not expose a raw cursor. "
            "Use execute(), fetchall(), or query() instead."
        )

    # ------------------------------------------------------------------
    # Private: SQL safety

    @classmethod
    def _assert_supported_sql(cls, sql: str) -> None:
        """Raise ValueError for disallowed SQL or multiple statements.

        Note: this guard is a limitation of the current execution engine,
        not necessarily the future API.
        """
        stripped = sql.strip()
        if not stripped:
            raise ValueError("SQL statement is empty")

        # Reject multiple statements (semicolon followed by non-whitespace)
        if cls._MULTI_STMT_RE.search(stripped):
            raise ValueError(
                "Multiple SQL statements are not supported in this execution engine"
            )

        # Allow only SELECT and WITH
        first_kw_m = re.match(r"^\s*(\w+)", stripped, re.IGNORECASE)
        if first_kw_m:
            kw = first_kw_m.group(1).upper()
            if kw not in ("SELECT", "WITH"):
                raise ValueError(
                    f"SQL keyword {kw!r} is not supported. "
                    "Only SELECT and WITH are accepted in this execution engine."
                )

    # ------------------------------------------------------------------
    # Private: execution

    def _execute_map_reduce_select(self, sql: str, params) -> List[Dict]:
        """Query each repo independently and merge the resulting rows."""
        rows: List[Dict] = []
        for repo in self._repos:
            rows.extend(self._query_one_repo(repo, sql, params))
        return rows

    def _query_one_repo(
        self, repo: Dict[str, Any], sql: str, params
    ) -> List[Dict]:
        """Open repo's DB read-only, execute sql, return list[dict]."""
        db_path = self._repo_value(repo, "db_path")
        uri = self._readonly_sqlite_uri(db_path)
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        self._install_readonly_authorizer(conn)
        try:
            merged = self._prepare_repo_params(repo, params)
            cur = conn.execute(sql, merged)
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def _prepare_repo_params(
        self, repo: Dict[str, Any], user_params
    ) -> object:
        """
        Return a params object suitable for sqlite3.execute().

        If user_params is None or a dict, inject repo metadata named params
        (__repo_name, __repo_root, __repo_db_path) into a new dict and return
        it.  If user_params is a positional sequence, return it unchanged
        (repo metadata is not available as named params in that case).
        """
        meta: Dict[str, str] = {
            "__repo_name":    self._repo_value(repo, "name"),
            "__repo_root":    self._repo_value(repo, "root"),
            "__repo_db_path": self._repo_value(repo, "db_path"),
        }
        if user_params is None:
            return meta
        if isinstance(user_params, dict):
            merged = dict(user_params)
            merged.update(meta)
            return merged
        # Positional params (tuple/list) — pass through unchanged.
        return user_params

    @staticmethod
    def _repo_value(repo: object, key: str) -> str:
        """Return the value for ``key`` from a repo dict or repo object.

        Supports both dict-style (``repo["key"]``) and attribute-style
        (``repo.key``) access so that plain dicts and gateway repo objects
        work interchangeably.

        Special case: when ``key`` is ``"db_path"`` and the repo has no
        ``db_path`` field, ``codeintel_db_path`` is tried as a fallback.
        This lets gateway objects that expose ``codeintel_db_path`` be used
        without renaming.
        """
        keys_to_try = [key]
        if key == "db_path":
            keys_to_try.append("codeintel_db_path")

        for k in keys_to_try:
            # Dict-style access
            if isinstance(repo, dict):
                v = repo.get(k)
            else:
                v = getattr(repo, k, None)
            if v is not None and v != "":
                return str(v)
        return ""

    @staticmethod
    def _readonly_sqlite_uri(db_path: str) -> str:
        """Return a read-only SQLite URI for db_path."""
        p = Path(db_path).resolve().as_posix()
        # as_posix() on Windows yields 'C:/...' (no leading slash);
        # SQLite URI requires three slashes for absolute paths on all platforms.
        if not p.startswith("/"):
            p = "/" + p
        return f"file://{p}?mode=ro"

    @staticmethod
    def _install_readonly_authorizer(conn: sqlite3.Connection) -> None:
        """
        Install a SQLite authorizer that permits only read-related actions.

        Provides defence-in-depth on top of the URI mode=ro flag.
        """
        _ALLOWED = frozenset({
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
        })

        def _authorizer(action, arg1, arg2, db_name, trigger_name):
            return sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY

        conn.set_authorizer(_authorizer)


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def _open_db(require_existing: bool = False) -> CodeIndexDb:
    db_path = DEFAULT_DB_PATH
    if require_existing and not db_path.exists():
        raise CommandError(
            "no database found.  Run:  python codeintel.py scan <path>"
        )
    return CodeIndexDb(db_path)


# ---------------------------------------------------------------------------
# Published command registry
#
# Published commands are ordinary named-argument callables.  CLI, MCP, chat,
# and REPL are all transports/adapters over the same callable.
#
# The registry is built from an explicit PUBLISHED_COMMANDS mapping; the first
# key for each unique callable becomes the canonical command name, and any
# subsequent keys for the same callable are registered as aliases.
#
# CLI parsing rules (no eval is used):
#   str / int / float / bool  — one positional token each
#   list / dict               — parsed with json.loads
#   Optional[X]               — "none"/"null"/"" → None, else converts as X
#   --name value              — keyword override for any parameter
#   --flag                    — bare flag treated as boolean true
# ---------------------------------------------------------------------------

_NONE_TYPE = type(None)


class CommandError(Exception):
    """Raised when command-line arguments cannot be bound to a command."""


def _parse_bool(raw: str) -> bool:
    low = raw.strip().lower()
    if low in ("1", "true", "yes", "y", "on"):
        return True
    if low in ("0", "false", "no", "n", "off"):
        return False
    raise CommandError(f"invalid boolean value: {raw!r}")


def _convert_scalar(raw: str, scalar_type: object) -> object:
    if scalar_type is str:
        return raw
    if scalar_type is int:
        try:
            return int(raw)
        except ValueError:
            raise CommandError(f"invalid integer value: {raw!r}")
    if scalar_type is float:
        try:
            return float(raw)
        except ValueError:
            raise CommandError(f"invalid float value: {raw!r}")
    if scalar_type is bool:
        return _parse_bool(raw)
    # Unknown scalar — pass the raw string through unchanged.
    return raw


def _convert_value(raw: str, annotation: object) -> object:
    if annotation is inspect.Parameter.empty or annotation is str:
        return raw

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    # Optional[X] / Union[X, None]
    if origin is typing.Union:
        non_none = [a for a in args if a is not _NONE_TYPE]
        if len(args) == 2 and len(non_none) == 1:
            if raw.strip().lower() in ("none", "null", ""):
                return None
            return _convert_scalar(raw, non_none[0])
        raise CommandError(f"unsupported Union annotation: {annotation!r}")

    # list via JSON
    if annotation is list or origin is list:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CommandError(f"invalid JSON list: {exc}")
        if not isinstance(parsed, list):
            raise CommandError("expected a JSON list value")
        return parsed

    # dict via JSON
    if annotation is dict or origin is dict:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CommandError(f"invalid JSON object: {exc}")
        if not isinstance(parsed, dict):
            raise CommandError("expected a JSON object value")
        return parsed

    return _convert_scalar(raw, annotation)


def _annotation_name(annotation: object) -> str:
    if annotation is inspect.Parameter.empty:
        return "str"

    origin = typing.get_origin(annotation)
    if origin is typing.Union:
        args = typing.get_args(annotation)
        non_none = [a for a in args if a is not _NONE_TYPE]
        if len(args) == 2 and len(non_none) == 1:
            return f"Optional[{_annotation_name(non_none[0])}]"
        return " | ".join(_annotation_name(a) for a in args)
    if origin is list:
        sub = typing.get_args(annotation)
        return f"List[{_annotation_name(sub[0])}]" if sub else "list"
    if origin is dict:
        return "dict"
    if hasattr(annotation, "__name__"):
        return annotation.__name__
    return str(annotation).replace("typing.", "")


def _parse_docstring(func: Callable) -> Tuple[str, str]:
    """Return (summary, long_description) from a function's docstring."""
    doc = inspect.getdoc(func) or ""
    lines = doc.splitlines()
    summary = ""
    summary_idx = -1
    for i, line in enumerate(lines):
        if line.strip():
            summary = line.strip()
            summary_idx = i
            break
    if summary_idx < 0:
        return "", ""
    remaining = lines[summary_idx + 1:]
    while remaining and not remaining[0].strip():
        remaining.pop(0)
    long_description = "\n".join(remaining).strip()
    return summary, long_description


class _CommandSpec:
    """Introspected metadata for a single published command."""

    def __init__(
        self,
        name: str,
        func: Callable,
        aliases: Tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.func = func
        self.aliases = aliases
        self.summary, self.long_description = _parse_docstring(func)
        self.params = [
            p
            for p in inspect.signature(func).parameters.values()
            if p.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
        ]

    def describe(self) -> Dict[str, Any]:
        parameters = []
        for p in self.params:
            has_default = p.default is not inspect.Parameter.empty
            parameters.append(
                {
                    "name": p.name,
                    "type": _annotation_name(p.annotation),
                    "required": not has_default,
                    "default": p.default if has_default else None,
                    "kind": p.kind.name.lower(),
                }
            )
        return {
            "command": self.name,
            "aliases": list(self.aliases),
            "parameters": parameters,
            "summary": self.summary,
            "long_description": self.long_description,
        }


class PublishedCommandRegistry:
    """
    Lightweight registry that publishes named commands and runs them from
    sys.argv-style argument lists.

    Built from an explicit PUBLISHED_COMMANDS mapping; the first key for each
    unique callable becomes the canonical command name, and subsequent keys
    for the same callable are registered as aliases.
    """

    def __init__(self) -> None:
        self._specs: Dict[str, _CommandSpec] = {}
        self._aliases: Dict[str, str] = {}

    @classmethod
    def from_mapping(
        cls, mapping: Dict[str, Callable]
    ) -> "PublishedCommandRegistry":
        """
        Build a registry from an explicit name→callable mapping.

        The first key for each unique callable becomes the canonical command
        name; subsequent keys for the same callable are registered as aliases.
        """
        registry = cls()

        # First pass: collect canonical name and aliases per function.
        func_to_canonical: Dict[int, str] = {}
        func_to_aliases: Dict[int, List[str]] = {}
        for name, func in mapping.items():
            fid = id(func)
            if fid not in func_to_canonical:
                func_to_canonical[fid] = name
                func_to_aliases[fid] = []
            else:
                func_to_aliases[fid].append(name)

        # Second pass: register in first-occurrence order.
        seen: set = set()
        for name, func in mapping.items():
            fid = id(func)
            if fid in seen:
                continue
            seen.add(fid)
            canonical = func_to_canonical[fid]
            aliases = tuple(func_to_aliases[fid])
            spec = _CommandSpec(canonical, func, aliases)
            registry._specs[canonical] = spec
            for alias in aliases:
                registry._aliases[alias] = canonical

        return registry

    def resolve(self, token: str) -> Optional[_CommandSpec]:
        if token in self._specs:
            return self._specs[token]
        canonical = self._aliases.get(token)
        return self._specs[canonical] if canonical is not None else None

    def names(self) -> List[str]:
        return list(self._specs)

    def specs(self) -> List[_CommandSpec]:
        return [self._specs[n] for n in self.names()]

    @staticmethod
    def _parse_argv(
        rest: List[str],
    ) -> Tuple[List[str], Dict[str, str]]:
        """Split argv into positional tokens and --key value kwargs."""
        positional: List[str] = []
        kwargs: Dict[str, str] = {}
        i = 0
        while i < len(rest):
            token = rest[i]
            if token.startswith("--"):
                key = token[2:]
                if i + 1 < len(rest) and not rest[i + 1].startswith("--"):
                    kwargs[key] = rest[i + 1]
                    i += 2
                else:
                    # Bare --flag → boolean true
                    kwargs[key] = "true"
                    i += 1
            else:
                positional.append(token)
                i += 1
        return positional, kwargs

    def _bind(
        self, spec: _CommandSpec, rest: List[str]
    ) -> Dict[str, object]:
        """Bind argv tokens to the function's named parameters.

        POSITIONAL_ONLY and POSITIONAL_OR_KEYWORD params may consume
        positional CLI tokens (in signature order) or be supplied as
        --name value.  KEYWORD_ONLY params (defined after * in the
        signature) must be supplied as --name value; they never consume
        a bare positional token.
        """
        positional, kwargs = self._parse_argv(rest)
        bound: Dict[str, object] = {}
        pos_idx = 0
        for p in spec.params:
            is_kw_only = p.kind is inspect.Parameter.KEYWORD_ONLY
            if p.name in kwargs:
                bound[p.name] = _convert_value(kwargs[p.name], p.annotation)
            elif not is_kw_only and pos_idx < len(positional):
                # Positional-or-keyword: consume the next bare token.
                bound[p.name] = _convert_value(
                    positional[pos_idx], p.annotation
                )
                pos_idx += 1
            elif p.default is not inspect.Parameter.empty:
                bound[p.name] = p.default
            elif is_kw_only:
                raise CommandError(
                    f"missing required keyword argument: --{p.name}"
                )
            else:
                raise CommandError(f"missing required argument: {p.name}")

        if pos_idx < len(positional):
            extra = " ".join(positional[pos_idx:])
            raise CommandError(f"unexpected extra argument(s): {extra}")

        unknown_kwargs = set(kwargs) - {p.name for p in spec.params}
        if unknown_kwargs:
            names = ", ".join(sorted(unknown_kwargs))
            raise CommandError(f"unknown option(s): {names}")
                            
        return bound

    def print_usage(self, unknown: Optional[str] = None) -> None:
        if unknown is not None:
            print(f"Unknown command: {unknown}", file=sys.stderr)
        print("Usage: codeintel.py <command> [args]")
        print()
        for spec in self.specs():
            print(f"  {spec.name:<11} {spec.summary}")

    def run(self, argv: List[str]) -> int:
        if not argv:
            self.print_usage()
            return 0

        spec = self.resolve(argv[0])
        if spec is None:
            self.print_usage(unknown=argv[0])
            return 1

        try:
            bound = self._bind(spec, argv[1:])
        except CommandError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        try:
            result = spec.func(**bound)
        except CommandError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        if isinstance(result, str):
            if result:
                print(result)
        elif isinstance(result, (dict, list)):
            print(json.dumps(result, indent=2, default=str))
        # None → print nothing
        return 0


REGISTRY: Optional[PublishedCommandRegistry] = None


def _registry() -> PublishedCommandRegistry:
    global REGISTRY
    if REGISTRY is None:
        REGISTRY = PublishedCommandRegistry.from_mapping(PUBLISHED_COMMANDS)
    return REGISTRY


# ---------------------------------------------------------------------------
# Published commands
# ---------------------------------------------------------------------------


def scan(path: str) -> str:
    """Scan a file or directory tree."""
    scan_path = Path(path).resolve()
    if not scan_path.exists():
        raise CommandError(f"path does not exist: {scan_path}")

    db = _open_db()
    repo_slug, root_path, branch_name = _get_repo_info(scan_path)
    repo_id = db.get_or_create_repo(repo_slug, root_path)
    branch_id = db.get_or_create_branch(repo_id, branch_name)

    extractors: List[LanguageExtractor] = [
        SqlSchemaExtractor(),
        PythonAstExtractor(),
    ]
    scanner = CodeScanner(db, branch_id, extractors, Path(root_path))
    scan_run_id, files_scanned, entities_observed, drifts_detected = scanner.scan(
        scan_path
    )
    db.close()

    return (
        f"Scan complete  "
        f"run={scan_run_id}  "
        f"files={files_scanned}  "
        f"entities={entities_observed}  "
        f"drifts={drifts_detected}"
    )


def map_repo() -> str:
    """Show current entity map."""
    db = _open_db(require_existing=True)
    rows = db.query(
        "SELECT entity_type, qualified_name, file_path, start_line "
        "FROM v_entity_current "
        "ORDER BY file_path, start_line, qualified_name"
    )
    db.close()

    if not rows:
        return "No active entities found."

    out: List[str] = []
    current_file: object = object()  # sentinel — intentionally unequal to any str
    for row in rows:
        fp = row["file_path"] or "(no file)"
        if fp != current_file:
            current_file = fp
            out.append(f"\n{fp}")
        line_tag = f":{row['start_line']}" if row["start_line"] is not None else ""
        out.append(f"  [{row['entity_type']}] {row['qualified_name']}{line_tag}")

    return "\n".join(out)


def find(name: str) -> str:
    """Find active entities by name (substring match)."""
    db = _open_db(require_existing=True)
    escaped = _like_escape(name)
    rows = db.query(
        "SELECT entity_type, qualified_name, file_path, start_line, end_line "
        "FROM v_entity_current "
        "WHERE qualified_name LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' "
        "ORDER BY file_path, start_line",
        (f"%{escaped}%", f"%{escaped}%"),
    )
    db.close()

    if not rows:
        return f"No active entities matching '{name}'."

    out: List[str] = []
    for row in rows:
        fp = row["file_path"] or ""
        loc = f"  {fp}:{row['start_line']}-{row['end_line']}" if fp else ""
        out.append(f"[{row['entity_type']}] {row['qualified_name']}{loc}")

    return "\n".join(out)


def status() -> str:
    """Show DB health and scan statistics."""
    db = _open_db(require_existing=True)

    def _meta(key: str) -> str:
        rows = db.query("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else "?"

    schema_v  = _meta("schema_version")
    scanner_v = _meta("scanner_version")

    out: List[str] = [
        f"DB path         : {db.db_path}",
        f"schema_version  : {schema_v}",
        f"scanner_version : {scanner_v}",
    ]

    repo_rows = db.query(
        "SELECT gr.repo_slug, gr.root_path, gb.branch_name "
        "FROM git_branch gb "
        "JOIN git_repo gr ON gr.id = gb.repo_id "
        "ORDER BY gr.repo_slug, gb.branch_name"
    )
    out.append(f"repos/branches  : {len(repo_rows)}")
    for row in repo_rows:
        out.append(
            f"  {row['repo_slug']} / {row['branch_name']}  ({row['root_path']})"
        )

    latest = db.query(
        "SELECT id, status, started_at, finished_at, "
        "       files_scanned, entities_observed, drifts_detected "
        "FROM scan_run "
        "ORDER BY id DESC LIMIT 1"
    )
    if latest:
        sr = latest[0]
        out.append(
            f"latest scan run : id={sr['id']}  status={sr['status']}  "
            f"started={sr['started_at']}  finished={sr['finished_at'] or '-'}  "
            f"files={sr['files_scanned']}  entities={sr['entities_observed']}  "
            f"drifts={sr['drifts_detected']}"
        )
    else:
        out.append("latest scan run : none")

    active_entities = db.query(
        "SELECT COUNT(*) AS n FROM code_entity WHERE lifecycle_status = 'active'"
    )[0]["n"]
    source_files = db.query("SELECT COUNT(*) AS n FROM source_file")[0]["n"]
    open_drifts = db.query(
        "SELECT COUNT(*) AS n FROM drift_event WHERE status = 'open'"
    )[0]["n"]
    failed_runs = db.query(
        "SELECT COUNT(*) AS n FROM scan_run WHERE status = 'failed'"
    )[0]["n"]

    sf_rows = db.get_source_files_for_stale()
    stale_count = 0
    for row in sf_rows:
        p = Path(row["root_path"]) / row["file_path"]
        if not p.exists() or _hash_file(p) != row["file_hash"]:
            stale_count += 1

    out.extend([
        f"active entities : {active_entities}",
        f"source files    : {source_files}",
        f"stale files     : {stale_count}",
        f"open drifts     : {open_drifts}",
        f"failed runs     : {failed_runs}",
    ])

    db.close()
    return "\n".join(out)


def orient(name: str) -> str:
    """
    Show rich orientation info for an entity (agent use).

    Agent-facing entity lookup. Returns rich orientation information for each
    active entity whose qualified_name or name matches the search term.
    """
    db = _open_db(require_existing=True)
    escaped = _like_escape(name)

    rows = db.query(
        """
        WITH
        matching_entities AS (
            SELECT
                entity_id,
                qualified_name,
                name,
                entity_type,
                file_path,
                start_line,
                end_line,
                language,
                detection_method
            FROM v_entity_current
            WHERE qualified_name LIKE ? ESCAPE '\\'
               OR name LIKE ? ESCAPE '\\'
        ),
        entity_text_pivot AS (
            SELECT
                entity_id,
                MAX(CASE WHEN text_kind = 'signature'
                         THEN text_body END) AS signature_text,
                MAX(CASE WHEN text_kind = 'docstring'
                         THEN text_body END) AS docstring_text
            FROM entity_text
            WHERE entity_id IN (
                SELECT entity_id
                FROM matching_entities
            )
            GROUP BY entity_id
        )
        SELECT
            m.entity_id,
            m.qualified_name,
            m.name,
            m.entity_type,
            m.file_path,
            m.start_line,
            m.end_line,
            m.language,
            m.detection_method,
            t.signature_text,
            t.docstring_text
        FROM matching_entities AS m
        LEFT JOIN entity_text_pivot AS t
          ON t.entity_id = m.entity_id
        ORDER BY m.file_path, m.start_line
        """,
        (f"%{escaped}%", f"%{escaped}%"),
    )
    db.close()

    if not rows:
        return f"No active entities matching '{name}'."

    parts: List[str] = []
    for row in rows:
        fp = row["file_path"] or ""
        loc = f"{fp}:{row['start_line']}-{row['end_line']}" if fp else ""
        block: List[str] = [f"[{row['entity_type']}] {row['qualified_name']}"]
        if loc:
            block.append(f"  location  : {loc}")
        block.append(f"  language  : {row['language'] or '-'}")
        block.append(f"  detection : {row['detection_method'] or '-'}")
        if row["signature_text"]:
            block.append(f"  signature : {row['signature_text']}")
        if row["docstring_text"]:
            first_para = row["docstring_text"].split("\n\n")[0].strip()
            block.append(f"  docstring : {first_para}")
        parts.append("\n".join(block))

    return "\n\n".join(parts)


def drifts(scope: str = "open") -> str:
    """List drift events by scope: open, latest, or all."""
    db = _open_db(require_existing=True)

    scope_norm = scope.strip().lower()
    if scope_norm == "open":
        rows = db.query(
            "SELECT drift_event_id, scan_run_id, drift_kind, "
            "       qualified_name, entity_type, file_path, start_line, "
            "       old_hash, new_hash, status "
            "FROM v_open_drifts "
            "ORDER BY drift_event_id"
        )
    elif scope_norm in ("latest", "all"):
        latest_scan_run_id: Optional[int] = None
        if scope_norm == "latest":
            latest = db.query("SELECT id FROM scan_run ORDER BY id DESC LIMIT 1")
            latest_scan_run_id = latest[0]["id"] if latest else None
            if latest_scan_run_id is None:
                db.close()
                return "No scan runs found."

        sql = (
            "SELECT de.id AS drift_event_id, de.scan_run_id, de.drift_kind, "
            "       e.qualified_name, e.entity_type, sf.file_path, el.start_line, "
            "       de.old_hash, de.new_hash, de.status "
            "FROM drift_event de "
            "JOIN code_entity e ON e.id = de.entity_id "
            "LEFT JOIN entity_location el "
            "  ON el.id = ("
            "      SELECT el2.id FROM entity_location el2 "
            "      WHERE el2.entity_id = e.id "
            "      ORDER BY el2.scan_run_id DESC, el2.id DESC "
            "      LIMIT 1"
            "  ) "
            "LEFT JOIN source_file sf ON sf.id = el.source_file_id "
        )
        params: Tuple[object, ...] = ()
        if latest_scan_run_id is not None:
            sql += "WHERE de.scan_run_id = ? "
            params = (latest_scan_run_id,)
        sql += "ORDER BY de.id"
        rows = db.query(sql, params)
    else:
        db.close()
        raise CommandError("scope must be one of: open, latest, all")

    db.close()

    if not rows:
        if scope_norm == "open":
            return "No open drift events."
        return f"No drift events for scope '{scope_norm}'."

    label = "Open drifts" if scope_norm == "open" else f"Drifts ({scope_norm})"
    out: List[str] = [f"{label} ({len(rows)}):"]
    for row in rows:
        fp = row["file_path"] or ""
        loc = (
            f"{fp}:{row['start_line']}"
            if fp and row["start_line"] is not None
            else fp
        )
        old_h = (row["old_hash"] or "")[:10] or "-"
        new_h = (row["new_hash"] or "")[:10] or "-"
        out.append(
            f"  [{row['drift_kind']:8}] id={row['drift_event_id']} "
            f"run={row['scan_run_id']} "
            f"{row['qualified_name']} ({row['entity_type']}) "
            f"@ {loc}  "
            f"old={old_h} new={new_h}"
        )

    return "\n".join(out)


def ack_drift(
    drift_event_id: int,
    by: str = "human",
    note: str = "",
    resolution: str = "acknowledged",
) -> str:
    """Acknowledge a drift event and record acknowledgement metadata."""
    db = _open_db(require_existing=True)
    if not db.drift_event_exists(drift_event_id):
        db.close()
        raise CommandError(f"drift event id not found: {drift_event_id}")
    db.update_drift_resolution(
        drift_event_id,
        status="acknowledged",
        by=by,
        note=note,
        resolution_kind=resolution,
    )
    db.commit()
    db.close()
    return f"Acknowledged drift id={drift_event_id}."


def ignore_drift(
    drift_event_id: int,
    by: str = "human",
    note: str = "",
) -> str:
    """Mark a drift event as ignored with acknowledgement metadata."""
    db = _open_db(require_existing=True)
    if not db.drift_event_exists(drift_event_id):
        db.close()
        raise CommandError(f"drift event id not found: {drift_event_id}")
    db.update_drift_resolution(
        drift_event_id,
        status="ignored",
        by=by,
        note=note,
        resolution_kind="ignored",
    )
    db.commit()
    db.close()
    return f"Ignored drift id={drift_event_id}."


def stale() -> str:
    """
    List source files modified since last scan.

    Report source files whose on-disk sha256 no longer matches the stored
    file_hash, meaning they have been modified since the last scan.
    Paths are stored repo-relative and resolved against the repo root.
    """
    db = _open_db(require_existing=True)
    rows = db.get_source_files_for_stale()
    db.close()

    stale_files: List[Tuple[str, str]] = []
    for row in rows:
        p = Path(row["root_path"]) / row["file_path"]
        if not p.exists():
            stale_files.append((row["file_path"], "missing"))
        elif _hash_file(p) != row["file_hash"]:
            stale_files.append((row["file_path"], "modified"))

    if not stale_files:
        return "All scanned source files are current."

    out: List[str] = [f"Stale ({len(stale_files)}):"]
    for file_path, reason in stale_files:
        out.append(f"  [{reason}] {file_path}")

    return "\n".join(out)


def commands() -> str:
    """List published commands and one-line descriptions."""
    out: List[str] = ["Available commands:"]
    for spec in _registry().specs():
        alias_hint = (
            f"  (alias: {', '.join(spec.aliases)})" if spec.aliases else ""
        )
        out.append(f"  {spec.name:<11} {spec.summary}{alias_hint}")
    return "\n".join(out)


def help_json() -> str:
    """Emit command registry metadata as JSON."""
    payload = [spec.describe() for spec in _registry().specs()]
    return json.dumps(payload, indent=2, default=str)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _extract_source_entity_snapshot(
    path: Path, repo_root: Path
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Run the appropriate LanguageExtractor for path and return
    (language, normalised_records).

    Each normalised record contains:
        entity_type, name, qualified_name, parent_qualified_name,
        start_line, end_line, detection_method, body_hash,
        signature_text, docstring_text
    """
    extractors: List[LanguageExtractor] = [
        SqlSchemaExtractor(),
        PythonAstExtractor(),
    ]
    extractor: Optional[LanguageExtractor] = None
    for ext in extractors:
        if ext.supports_path(path):
            extractor = ext
            break

    if extractor is None:
        raise CommandError(f"unsupported_source_type: {path.suffix!r}")

    raw = extractor.extract_file(path, repo_root)
    if raw is None:
        raise CommandError(f"extract_failed: could not parse {path}")

    normalised: List[Dict[str, Any]] = []
    for rec in raw:
        sig_text = ""
        doc_text = ""
        for text_kind, text_body in rec.get("texts", []):
            if text_kind == "signature":
                sig_text = text_body or ""
            elif text_kind == "docstring":
                doc_text = text_body or ""
        normalised.append(
            {
                "entity_type": rec["entity_type"],
                "name": rec["name"],
                "qualified_name": rec["qualified_name"],
                "parent_qualified_name": rec.get("parent_qualified_name"),
                "start_line": rec["start_line"],
                "end_line": rec["end_line"],
                "detection_method": rec["detection_method"],
                "body_hash": _hash_text(rec["body_text"]),
                "signature_text": sig_text,
                "docstring_text": doc_text,
            }
        )
    return extractor.language_name, normalised


def _load_db_entity_snapshot(
    db: CodeIndexDb, file_path: str
) -> Tuple[Optional[sqlite3.Row], List[Dict[str, Any]]]:
    """
    Load the source_file row and active entity records for file_path.
    Read-only; never modifies DB state.
    Returns (source_file_row_or_None, normalised_db_records).
    """
    sf_rows = db.query(
        "SELECT id, file_hash FROM source_file WHERE file_path = ?",
        (file_path,),
    )
    if not sf_rows:
        return None, []

    sf_row = sf_rows[0]
    sf_id = sf_row["id"]

    rows = db.query(
        """
        WITH ent AS (
            SELECT
                ec.id          AS entity_id,
                ec.qualified_name,
                ec.name,
                ec.entity_type,
                ec.parent_entity_id,
                ec.lifecycle_status,
                el.start_line,
                el.end_line,
                el.detection_method
            FROM code_entity ec
            JOIN entity_location el
              ON el.id = (
                  SELECT el2.id FROM entity_location el2
                  WHERE el2.entity_id = ec.id
                  ORDER BY el2.scan_run_id DESC, el2.id DESC
                  LIMIT 1
              )
            WHERE el.source_file_id = ?
              AND ec.lifecycle_status = 'active'
        ),
        txt AS (
            SELECT
                entity_id,
                MAX(CASE WHEN text_kind = 'signature'  THEN text_body END) AS sig,
                MAX(CASE WHEN text_kind = 'docstring'  THEN text_body END) AS doc
            FROM entity_text
            WHERE entity_id IN (SELECT entity_id FROM ent)
            GROUP BY entity_id
        ),
        hsh AS (
            SELECT entity_id, hash_value
            FROM entity_hash eh
            WHERE hash_kind = 'body_sha256'
              AND entity_id IN (SELECT entity_id FROM ent)
              AND scan_run_id = (
                  SELECT MAX(scan_run_id) FROM entity_hash eh2
                  WHERE eh2.entity_id = eh.entity_id
                    AND eh2.hash_kind  = 'body_sha256'
              )
        )
        SELECT
            e.entity_id,
            e.qualified_name,
            e.name,
            e.entity_type,
            e.start_line,
            e.end_line,
            e.detection_method,
            COALESCE(t.sig, '') AS signature_text,
            COALESCE(t.doc, '') AS docstring_text,
            COALESCE(h.hash_value, '') AS body_hash
        FROM ent e
        LEFT JOIN txt t ON t.entity_id = e.entity_id
        LEFT JOIN hsh h ON h.entity_id = e.entity_id
        ORDER BY e.start_line, e.qualified_name
        """,
        (sf_id,),
    )

    normalised: List[Dict[str, Any]] = [
        {
            "qualified_name": r["qualified_name"],
            "name": r["name"],
            "entity_type": r["entity_type"],
            "start_line": r["start_line"],
            "end_line": r["end_line"],
            "detection_method": r["detection_method"],
            "signature_text": r["signature_text"] or "",
            "docstring_text": r["docstring_text"] or "",
            "body_hash": r["body_hash"] or "",
        }
        for r in rows
    ]
    return sf_row, normalised


def _compare_source_and_db_snapshots(
    source_records: List[Dict[str, Any]],
    db_records: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Compare source and DB entity snapshots keyed by qualified_name.
    Returns a list of issue dicts (severity, kind, qualified_name, message,
    details).
    """
    src_by_qname: Dict[str, Dict] = {r["qualified_name"]: r for r in source_records}
    db_by_qname:  Dict[str, Dict] = {r["qualified_name"]: r for r in db_records}

    issues: List[Dict[str, Any]] = []

    for qname in sorted(src_by_qname):
        if qname not in db_by_qname:
            issues.append(
                {
                    "severity": "error",
                    "kind": "missing_in_db",
                    "qualified_name": qname,
                    "message": f"{qname!r} found in source but not in DB",
                    "details": None,
                }
            )

    for qname in sorted(db_by_qname):
        if qname not in src_by_qname:
            issues.append(
                {
                    "severity": "error",
                    "kind": "missing_in_source",
                    "qualified_name": qname,
                    "message": f"{qname!r} found in DB but not in source",
                    "details": None,
                }
            )

    for qname in sorted(src_by_qname):
        if qname not in db_by_qname:
            continue
        s = src_by_qname[qname]
        d = db_by_qname[qname]

        if s["body_hash"] and d["body_hash"] and s["body_hash"] != d["body_hash"]:
            issues.append(
                {
                    "severity": "warning",
                    "kind": "body_hash_mismatch",
                    "qualified_name": qname,
                    "message": f"{qname!r} body hash differs",
                    "details": {
                        "source": s["body_hash"][:12],
                        "db": d["body_hash"][:12],
                    },
                }
            )

        if (s["start_line"], s["end_line"]) != (d["start_line"], d["end_line"]):
            issues.append(
                {
                    "severity": "warning",
                    "kind": "line_range_mismatch",
                    "qualified_name": qname,
                    "message": (
                        f"{qname!r} line range differs: "
                        f"source {s['start_line']}-{s['end_line']} "
                        f"vs db {d['start_line']}-{d['end_line']}"
                    ),
                    "details": {
                        "source": [s["start_line"], s["end_line"]],
                        "db": [d["start_line"], d["end_line"]],
                    },
                }
            )

        s_sig = (s["signature_text"] or "").strip()
        d_sig = (d["signature_text"] or "").strip()
        if s_sig != d_sig:
            issues.append(
                {
                    "severity": "warning",
                    "kind": "signature_mismatch",
                    "qualified_name": qname,
                    "message": f"{qname!r} signature differs",
                    "details": {"source": s_sig[:120], "db": d_sig[:120]},
                }
            )

        s_doc = (s["docstring_text"] or "").strip()
        d_doc = (d["docstring_text"] or "").strip()
        if s_doc != d_doc:
            issues.append(
                {
                    "severity": "warning",
                    "kind": "docstring_mismatch",
                    "qualified_name": qname,
                    "message": f"{qname!r} docstring differs",
                    "details": {
                        "source": s_doc[:120],
                        "db": d_doc[:120],
                    },
                }
            )

    return issues


def _format_validation_report_text(report: Dict[str, Any]) -> str:
    """Render a validation report dict as a human/agent-readable text string."""
    ok_tag = "CLEAN" if report["ok"] else "DRIFT"
    lines: List[str] = [
        f"validate-source  [{ok_tag}]",
        f"  source       : {report['source_path']}",
        f"  db           : {report['db_path']}",
        f"  repo root    : {report['repo_root']}",
        f"  rel path     : {report['repo_relative_path']}",
    ]

    fhm = report["file_hash_match"]
    hash_tag = "match" if fhm else ("MISMATCH" if fhm is False else "n/a")
    lines.append(f"  file hash    : {hash_tag}")

    s = report["summary"]
    lines += [
        "",
        "  summary:",
        f"    source entities      : {s['source_entities']}",
        f"    db entities          : {s['db_entities']}",
        f"    missing in db        : {s['missing_in_db']}",
        f"    missing in source    : {s['missing_in_source']}",
        f"    line range mismatches: {s['line_range_mismatches']}",
        f"    signature mismatches : {s['signature_mismatches']}",
        f"    docstring mismatches : {s['docstring_mismatches']}",
        f"    body hash mismatches : {s['body_hash_mismatches']}",
    ]

    issues = report.get("issues", [])
    if issues:
        lines.append("")
        lines.append("  issues:")
        for iss in issues:
            qn = f"  [{iss['qualified_name']}]" if iss.get("qualified_name") else ""
            lines.append(
                f"    [{iss['severity'].upper():7}] {iss['kind']}{qn}: {iss['message']}"
            )
            if iss.get("details"):
                det = iss["details"]
                if isinstance(det, dict):
                    for k, v in det.items():
                        lines.append(f"              {k}: {v}")
    else:
        lines.append("")
        lines.append("  no issues found.")

    return "\n".join(lines)


def _format_validation_report_json(report: Dict[str, Any]) -> str:
    """Render a validation report dict as a JSON string."""
    return json.dumps(report, indent=2, default=str)


# ---------------------------------------------------------------------------
# validate_source — published command
# ---------------------------------------------------------------------------


def validate_source(
    path: str,
    *,
    db_path: str = str(DEFAULT_DB_PATH),
    format: str = "text",
) -> str:
    """
    Validate one source file against a codeintel database.

    Extract Python/SQL entities from path using the same extractor logic as
    scan, then compare them with the active DB representation for that file.
    Returns a text report by default, or JSON when format == 'json'.
    """
    if format not in ("text", "json"):
        raise CommandError("format must be text or json")

    source_path = Path(path).resolve()
    if not source_path.exists():
        raise CommandError(f"path does not exist: {source_path}")

    db_file = Path(db_path)
    if not db_file.exists():
        raise CommandError(
            f"no database found at {db_path}.  Run:  python codeintel.py scan <path>"
        )

    repo_slug, root_path_str, _branch = _get_repo_info(source_path)
    repo_root = Path(root_path_str)
    rel_path = _repo_relative_path(source_path, repo_root)

    # Build base report skeleton.
    report: Dict[str, Any] = {
        "ok": False,
        "db_path": str(db_file),
        "source_path": str(source_path),
        "repo_root": root_path_str,
        "repo_relative_path": rel_path,
        "file_hash_source": _hash_file(source_path),
        "file_hash_db": None,
        "file_hash_match": None,
        "summary": {
            "source_entities": 0,
            "db_entities": 0,
            "missing_in_db": 0,
            "missing_in_source": 0,
            "line_range_mismatches": 0,
            "signature_mismatches": 0,
            "docstring_mismatches": 0,
            "body_hash_mismatches": 0,
        },
        "issues": [],
    }

    # --- Extract source snapshot ----------------------------------------
    try:
        _lang, source_records = _extract_source_entity_snapshot(
            source_path, repo_root
        )
    except CommandError as exc:
        kind = str(exc).split(":")[0].strip()
        report["issues"].append(
            {
                "severity": "error",
                "kind": kind,
                "qualified_name": None,
                "message": str(exc),
                "details": None,
            }
        )
        if format == "json":
            return _format_validation_report_json(report)
        return _format_validation_report_text(report)

    report["summary"]["source_entities"] = len(source_records)

    # --- Load DB snapshot --------------------------------------------------
    db = CodeIndexDb(db_file)
    try:
        sf_row, db_records = _load_db_entity_snapshot(db, rel_path)
    finally:
        db.close()

    if sf_row is None:
        report["issues"].append(
            {
                "severity": "error",
                "kind": "db_file_not_found",
                "qualified_name": None,
                "message": (
                    f"{rel_path!r} not found in DB. "
                    "Run 'scan' first."
                ),
                "details": None,
            }
        )
        if format == "json":
            return _format_validation_report_json(report)
        return _format_validation_report_text(report)

    report["file_hash_db"] = sf_row["file_hash"]
    report["file_hash_match"] = (
        report["file_hash_source"] == report["file_hash_db"]
    )
    if not report["file_hash_match"]:
        report["issues"].append(
            {
                "severity": "warning",
                "kind": "file_hash_mismatch",
                "qualified_name": None,
                "message": "File has changed since last scan",
                "details": {
                    "source": report["file_hash_source"][:12],
                    "db": report["file_hash_db"][:12],
                },
            }
        )

    report["summary"]["db_entities"] = len(db_records)

    # --- Compare -----------------------------------------------------------
    issues = _compare_source_and_db_snapshots(source_records, db_records)
    report["issues"].extend(issues)

    # --- Tally summary counts --------------------------------------------
    def _count(kind: str) -> int:
        return sum(1 for i in report["issues"] if i["kind"] == kind)

    report["summary"]["missing_in_db"]         = _count("missing_in_db")
    report["summary"]["missing_in_source"]      = _count("missing_in_source")
    report["summary"]["line_range_mismatches"]  = _count("line_range_mismatch")
    report["summary"]["signature_mismatches"]   = _count("signature_mismatch")
    report["summary"]["docstring_mismatches"]   = _count("docstring_mismatch")
    report["summary"]["body_hash_mismatches"]   = _count("body_hash_mismatch")

    report["summary"]["issues"]   = len(report["issues"])
    report["summary"]["errors"]   = sum(
        1 for i in report["issues"] if i["severity"] == "error"
    )
    report["summary"]["warnings"] = sum(
        1 for i in report["issues"] if i["severity"] == "warning"
    )
    report["ok"] = report["summary"]["issues"] == 0

    if format == "json":
        return _format_validation_report_json(report)
    return _format_validation_report_text(report)


# ---------------------------------------------------------------------------
# Publication mapping — the explicit source of command truth
# ---------------------------------------------------------------------------

PUBLISHED_COMMANDS: Dict[str, Callable] = {
    "scan":             scan,
    "find":             find,
    "orient":           orient,
    "status":           status,
    "stale":            stale,
    "drifts":           drifts,
    "ack-drift":        ack_drift,
    "ack_drift":        ack_drift,
    "ignore-drift":     ignore_drift,
    "ignore_drift":     ignore_drift,
    "commands":         commands,
    "help-json":        help_json,
    "help_json":        help_json,
    "map":              map_repo,
    "map_repo":         map_repo,
    "validate-source":  validate_source,
    "validate_source":  validate_source,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_registry() -> PublishedCommandRegistry:
    """Create and populate the command registry from PUBLISHED_COMMANDS."""
    return _registry()


def main(argv: Optional[List[str]] = None) -> int:
    registry = build_registry()
    return registry.run(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
