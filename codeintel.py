#!/usr/bin/env python3
"""
codeintel.py — Code Intelligence DB v1.1.1

Single-file, standard-library-only implementation.

Usage:
    python codeintel.py scan <path>   — scan a file or directory tree
    python codeintel.py map           — show current entity map
    python codeintel.py find <name>   — find entities by name
    python codeintel.py stale         — list files modified since last scan

DB path: .codeintel/codeintel.sqlite  (relative to cwd)
"""

import ast
import datetime
import hashlib
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Version constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "1.1.1"
SCANNER_VERSION = "codeintel 1.1.1"
DEFAULT_DB_PATH = Path(".codeintel") / "codeintel.sqlite"

# ---------------------------------------------------------------------------
# Embedded schema — v1.1.1
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
    new_hash    TEXT
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
"""

# ---------------------------------------------------------------------------
# SQL constraint-starter keywords — disqualify a line from being a column def
# ---------------------------------------------------------------------------

_SQL_CONSTRAINT_STARTERS = frozenset(
    {"PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT"}
)

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
        self._seed()

    # ------------------------------------------------------------------
    # Schema application

    def _apply_schema(self) -> None:
        # executescript issues an implicit COMMIT before running, which is
        # fine here — we only apply DDL during init.
        self._conn.executescript(SCHEMA_SQL)

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
                "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
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
                "Not used in v1.1.1.  Reserved for future full-body "
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
    Extract sql_table, sql_view, sql_index, and sql_column entities from
    .sql files using a line-by-line state machine with regex matching on
    CREATE TABLE / VIEW / INDEX statements.

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

        # Recurse into the function body; nested items have context='function'
        self._walk(node.body, module_qname, qname, "function", lines, records)


# ---------------------------------------------------------------------------
# CodeScanner — orchestrator
# ---------------------------------------------------------------------------


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
# CLI helpers
# ---------------------------------------------------------------------------


def _open_db(require_existing: bool = False) -> CodeIndexDb:
    db_path = DEFAULT_DB_PATH
    if require_existing and not db_path.exists():
        print(
            "Error: no database found.  Run:  python codeintel.py scan <path>",
            file=sys.stderr,
        )
        sys.exit(1)
    return CodeIndexDb(db_path)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def cmd_scan(args: List[str]) -> None:
    if not args:
        print("Usage: codeintel.py scan <path>", file=sys.stderr)
        sys.exit(1)

    scan_path = Path(args[0]).resolve()
    if not scan_path.exists():
        print(f"Error: path does not exist: {scan_path}", file=sys.stderr)
        sys.exit(1)

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

    print(
        f"Scan complete  "
        f"run={scan_run_id}  "
        f"files={files_scanned}  "
        f"entities={entities_observed}  "
        f"drifts={drifts_detected}"
    )
    db.close()


def cmd_map(args: List[str]) -> None:
    db = _open_db(require_existing=True)
    rows = db.query(
        "SELECT entity_type, qualified_name, file_path, start_line "
        "FROM v_entity_current "
        "ORDER BY file_path, start_line, qualified_name"
    )
    if not rows:
        print("No active entities found.")
        db.close()
        return

    current_file: object = object()  # sentinel — intentionally unequal to any str
    for row in rows:
        fp = row["file_path"] or "(no file)"
        if fp != current_file:
            current_file = fp
            print(f"\n{fp}")
        line_tag = f":{row['start_line']}" if row["start_line"] is not None else ""
        print(f"  [{row['entity_type']}] {row['qualified_name']}{line_tag}")

    db.close()


def cmd_find(args: List[str]) -> None:
    if not args:
        print("Usage: codeintel.py find <name>", file=sys.stderr)
        sys.exit(1)

    name = args[0]
    db = _open_db(require_existing=True)
    escaped = _like_escape(name)
    rows = db.query(
        "SELECT entity_type, qualified_name, file_path, start_line, end_line "
        "FROM v_entity_current "
        "WHERE qualified_name LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' "
        "ORDER BY file_path, start_line",
        (f"%{escaped}%", f"%{escaped}%"),
    )
    if not rows:
        print(f"No active entities matching '{name}'.")
        db.close()
        return

    for row in rows:
        fp = row["file_path"] or ""
        loc = ""
        if fp:
            loc = f"  {fp}:{row['start_line']}-{row['end_line']}"
        print(f"[{row['entity_type']}] {row['qualified_name']}{loc}")

    db.close()


def cmd_status(args: List[str]) -> None:
    db = _open_db(require_existing=True)

    # Meta values
    def _meta(key: str) -> str:
        rows = db.query("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else "?"

    schema_v  = _meta("schema_version")
    scanner_v = _meta("scanner_version")

    print(f"DB path         : {db.db_path}")
    print(f"schema_version  : {schema_v}")
    print(f"scanner_version : {scanner_v}")

    # Repos and branches
    repo_rows = db.query(
        "SELECT gr.repo_slug, gr.root_path, gb.branch_name "
        "FROM git_branch gb "
        "JOIN git_repo gr ON gr.id = gb.repo_id "
        "ORDER BY gr.repo_slug, gb.branch_name"
    )
    print(f"repos/branches  : {len(repo_rows)}")
    for row in repo_rows:
        print(f"  {row['repo_slug']} / {row['branch_name']}  ({row['root_path']})")

    # Latest scan run
    latest = db.query(
        "SELECT id, status, started_at, finished_at, "
        "       files_scanned, entities_observed, drifts_detected "
        "FROM scan_run "
        "ORDER BY id DESC LIMIT 1"
    )
    if latest:
        sr = latest[0]
        print(
            f"latest scan run : id={sr['id']}  status={sr['status']}  "
            f"started={sr['started_at']}  finished={sr['finished_at'] or '-'}  "
            f"files={sr['files_scanned']}  entities={sr['entities_observed']}  "
            f"drifts={sr['drifts_detected']}"
        )
    else:
        print("latest scan run : none")

    # Counts
    active_entities = db.query(
        "SELECT COUNT(*) AS n FROM code_entity WHERE lifecycle_status = 'active'"
    )[0]["n"]
    source_files = db.query("SELECT COUNT(*) AS n FROM source_file")[0]["n"]
    open_drifts  = db.query(
        "SELECT COUNT(*) AS n FROM drift_event WHERE status = 'open'"
    )[0]["n"]
    failed_runs  = db.query(
        "SELECT COUNT(*) AS n FROM scan_run WHERE status = 'failed'"
    )[0]["n"]

    # Stale count reuses the same logic as cmd_stale
    sf_rows = db.get_source_files_for_stale()
    stale_count = 0
    for row in sf_rows:
        p = Path(row["root_path"]) / row["file_path"]
        if not p.exists() or _hash_file(p) != row["file_hash"]:
            stale_count += 1

    print(f"active entities : {active_entities}")
    print(f"source files    : {source_files}")
    print(f"stale files     : {stale_count}")
    print(f"open drifts     : {open_drifts}")
    print(f"failed runs     : {failed_runs}")

    db.close()


def cmd_orient(args: List[str]) -> None:
    """
    Agent-facing entity lookup.  Prints rich orientation information for
    each active entity whose qualified_name or name matches the search term.
    """
    if not args:
        print("Usage: codeintel.py orient <name>", file=sys.stderr)
        sys.exit(1)

    name = args[0]
    db = _open_db(require_existing=True)
    escaped = _like_escape(name)

    rows = db.query(
        "SELECT entity_id, qualified_name, entity_type, file_path, "
        "       start_line, end_line, language, detection_method "
        "FROM v_entity_current "
        "WHERE qualified_name LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' "
        "ORDER BY file_path, start_line",
        (f"%{escaped}%", f"%{escaped}%"),
    )

    if not rows:
        print(f"No active entities matching '{name}'.")
        db.close()
        return

    for row in rows:
        fp = row["file_path"] or ""
        loc = f"{fp}:{row['start_line']}-{row['end_line']}" if fp else ""
        print(f"[{row['entity_type']}] {row['qualified_name']}")
        if loc:
            print(f"  location  : {loc}")
        print(f"  language  : {row['language'] or '-'}")
        print(f"  detection : {row['detection_method'] or '-'}")

        # Signature text
        sig = db.query(
            "SELECT text_body FROM entity_text "
            "WHERE entity_id = ? AND text_kind = 'signature'",
            (row["entity_id"],),
        )
        if sig:
            print(f"  signature : {sig[0]['text_body']}")

        # Docstring — first paragraph only
        doc = db.query(
            "SELECT text_body FROM entity_text "
            "WHERE entity_id = ? AND text_kind = 'docstring'",
            (row["entity_id"],),
        )
        if doc:
            first_para = doc[0]["text_body"].split("\n\n")[0].strip()
            print(f"  docstring : {first_para}")

        print()

    db.close()


def cmd_drifts(args: List[str]) -> None:
    db = _open_db(require_existing=True)
    rows = db.query(
        "SELECT drift_event_id, scan_run_id, drift_kind, "
        "       qualified_name, entity_type, file_path, start_line, "
        "       old_hash, new_hash, status "
        "FROM v_open_drifts "
        "ORDER BY drift_event_id"
    )

    if not rows:
        print("No open drift events.")
        db.close()
        return

    print(f"Open drifts ({len(rows)}):")
    for row in rows:
        fp = row["file_path"] or ""
        loc = f"{fp}:{row['start_line']}" if fp and row["start_line"] is not None else fp
        old_h = (row["old_hash"] or "")[:10] or "-"
        new_h = (row["new_hash"] or "")[:10] or "-"
        print(
            f"  [{row['drift_kind']:8}] id={row['drift_event_id']} "
            f"run={row['scan_run_id']} "
            f"{row['qualified_name']} ({row['entity_type']}) "
            f"@ {loc}  "
            f"old={old_h} new={new_h}"
        )

    db.close()


def cmd_stale(args: List[str]) -> None:
    """
    Report source files whose on-disk sha256 no longer matches the stored
    file_hash, meaning they have been modified since the last scan.
    Paths are stored repo-relative and resolved against the repo root.
    """
    db = _open_db(require_existing=True)
    rows = db.get_source_files_for_stale()

    stale: List[Tuple[str, str]] = []
    for row in rows:
        p = Path(row["root_path"]) / row["file_path"]
        if not p.exists():
            stale.append((row["file_path"], "missing"))
        else:
            if _hash_file(p) != row["file_hash"]:
                stale.append((row["file_path"], "modified"))

    if not stale:
        print("All scanned source files are current.")
    else:
        print(f"Stale ({len(stale)}):")
        for file_path, reason in stale:
            print(f"  [{reason}] {file_path}")

    db.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_COMMANDS = {
    "scan":   cmd_scan,
    "map":    cmd_map,
    "find":   cmd_find,
    "stale":  cmd_stale,
    "status": cmd_status,
    "orient": cmd_orient,
    "drifts": cmd_drifts,
}


def main() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0] not in _COMMANDS:
        print("Usage: codeintel.py <command> [args]")
        print()
        print("  scan <path>   scan a file or directory tree")
        print("  map           show current entity map")
        print("  find <name>   find active entities by name (substring match)")
        print("  stale         list source files modified since last scan")
        print("  status        show DB health and scan statistics")
        print("  orient <name> show rich orientation info for an entity (agent use)")
        print("  drifts        list open drift events")
        sys.exit(0 if not argv else 1)

    _COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    main()
