#!/usr/bin/env python3
"""
codeintel_confidence.py — Confidence harness for codeintel.py.

Verifies three rollout questions:
  1. Does the DB contain reliable line numbers for fast agent lookup?
  2. When fresh, what information can agents safely use without reading source?
  3. Are there repeatable tests for the scanner itself?

Usage:
    python codeintel_confidence.py

Exit code 0 = all checks passed.
Exit code 1 = one or more checks failed.
"""

import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CODEINTEL_PY = str(Path(__file__).resolve().parent / "codeintel.py")
PYTHON = sys.executable
DB_REL = Path(".codeintel") / "codeintel.sqlite"

# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

_results = []  # list of (name: str, passed: bool, detail: str)


def _record(name: str, passed: bool, detail: str = "") -> None:
    _results.append((name, passed, detail))
    status = "  PASS" if passed else "  FAIL"
    suffix = f"  — {detail}" if detail and not passed else ""
    print(f"{status}  {name}{suffix}")


def _assert(name: str, condition: bool, detail: str = "") -> None:
    _record(name, bool(condition), detail)


def _assert_eq(name: str, actual, expected) -> None:
    if actual == expected:
        _record(name, True)
    else:
        _record(name, False, f"expected {expected!r}, got {actual!r}")


# ---------------------------------------------------------------------------
# Subprocess / DB helpers
# ---------------------------------------------------------------------------


def run_ci(args: list, tmp: Path):
    """Run codeintel.py with the given args, cwd=tmp.  Returns CompletedProcess."""
    return subprocess.run(
        [PYTHON, CODEINTEL_PY] + [str(a) for a in args],
        cwd=str(tmp),
        capture_output=True,
        text=True,
    )


def q1(tmp: Path, sql: str, params: tuple = ()):
    """Return one row or None from the test DB."""
    conn = sqlite3.connect(str(tmp / DB_REL))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def qall(tmp: Path, sql: str, params: tuple = ()):
    """Return all rows from the test DB."""
    conn = sqlite3.connect(str(tmp / DB_REL))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# A. Python line numbers
# ---------------------------------------------------------------------------


def test_python_line_numbers() -> None:
    print("\nA. Python line numbers")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # Exact content (6 lines + trailing newline):
        #   1: def alpha():
        #   2:     pass
        #   3: (blank)
        #   4: class Beta:
        #   5:     def gamma(self):
        #   6:         pass
        (tmp / "lines.py").write_text(
            textwrap.dedent("""\
                def alpha():
                    pass

                class Beta:
                    def gamma(self):
                        pass
                """),
            encoding="utf-8",
        )

        r = run_ci(["scan", str(tmp)], tmp)
        _assert("A: scan exits 0", r.returncode == 0, r.stderr.strip())

        # Module
        row = q1(tmp,
            "SELECT start_line, end_line, file_path "
            "FROM v_entity_current WHERE qualified_name = 'lines'")
        _assert("A: module entity exists", row is not None)
        if row:
            _assert_eq("A: module start_line",        row["start_line"], 1)
            _assert_eq("A: module end_line",          row["end_line"],   6)
            _assert_eq("A: file_path is repo-relative", row["file_path"], "lines.py")

        # alpha
        row = q1(tmp,
            "SELECT start_line, end_line FROM v_entity_current "
            "WHERE qualified_name = 'lines.alpha'")
        _assert("A: alpha entity exists", row is not None)
        if row:
            _assert_eq("A: alpha start_line", row["start_line"], 1)
            _assert_eq("A: alpha end_line",   row["end_line"],   2)

        # Beta
        row = q1(tmp,
            "SELECT start_line, end_line FROM v_entity_current "
            "WHERE qualified_name = 'lines.Beta'")
        _assert("A: Beta entity exists", row is not None)
        if row:
            _assert_eq("A: Beta start_line", row["start_line"], 4)
            _assert_eq("A: Beta end_line",   row["end_line"],   6)

        # Beta.gamma
        row = q1(tmp,
            "SELECT start_line, end_line FROM v_entity_current "
            "WHERE qualified_name = 'lines.Beta.gamma'")
        _assert("A: Beta.gamma entity exists", row is not None)
        if row:
            _assert_eq("A: Beta.gamma start_line", row["start_line"], 5)
            _assert_eq("A: Beta.gamma end_line",   row["end_line"],   6)


# ---------------------------------------------------------------------------
# B. Decorator line numbers and drift
# ---------------------------------------------------------------------------


def test_decorator_lines_and_drift() -> None:
    print("\nB. Decorator line numbers and drift")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # Content (3 lines):
        #   1: @property
        #   2: def val():
        #   3:     return 1
        (tmp / "deco.py").write_text(
            textwrap.dedent("""\
                @property
                def val():
                    return 1
                """),
            encoding="utf-8",
        )

        run_ci(["scan", str(tmp)], tmp)

        row = q1(tmp,
            "SELECT start_line, end_line FROM v_entity_current "
            "WHERE qualified_name = 'deco.val'")
        _assert("B: val entity exists", row is not None)
        if row:
            _assert_eq("B: start_line at decorator (line 1)", row["start_line"], 1)
            _assert_eq("B: end_line",                         row["end_line"],   3)

        sig = q1(tmp,
            "SELECT et.text_body FROM entity_text et "
            "JOIN code_entity ce ON ce.id = et.entity_id "
            "WHERE ce.qualified_name = 'deco.val' AND et.text_kind = 'signature'")
        _assert("B: signature row exists", sig is not None)
        if sig:
            _assert("B: signature includes decorator",
                "@property" in sig["text_body"],
                repr(sig["text_body"]))

        # Change only the decorator; body is identical
        (tmp / "deco.py").write_text(
            textwrap.dedent("""\
                @staticmethod
                def val():
                    return 1
                """),
            encoding="utf-8",
        )
        run_ci(["scan", str(tmp)], tmp)

        drift = q1(tmp,
            "SELECT drift_kind FROM drift_event de "
            "JOIN code_entity ce ON ce.id = de.entity_id "
            "WHERE ce.qualified_name = 'deco.val' AND de.drift_kind = 'changed'")
        _assert("B: decorator-only change records drift_kind='changed'",
            drift is not None)


# ---------------------------------------------------------------------------
# C. Fresh DB orientation contract
# ---------------------------------------------------------------------------


def test_agent_orientation_contract() -> None:
    print("\nC. Fresh DB orientation contract")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        (tmp / "orient.py").write_text(
            textwrap.dedent("""\
                class Service:
                    def handle(self, req):
                        \"\"\"Handle a request.\"\"\"
                        return req
                """),
            encoding="utf-8",
        )

        run_ci(["scan", str(tmp)], tmp)

        row = q1(tmp,
            "SELECT qualified_name, entity_type, file_path, "
            "       start_line, end_line, detection_method, language "
            "FROM v_entity_current "
            "WHERE qualified_name = 'orient.Service.handle'")
        _assert("C: handle in v_entity_current", row is not None)

        if row:
            _assert("C: qualified_name present",    bool(row["qualified_name"]))
            _assert("C: entity_type present",       bool(row["entity_type"]))
            _assert("C: file_path present",         bool(row["file_path"]))
            _assert("C: start_line present",        row["start_line"] is not None)
            _assert("C: end_line present",          row["end_line"] is not None)
            _assert("C: detection_method present",  bool(row["detection_method"]))
            _assert("C: language present",          bool(row["language"]))

            print()
            print("  AGENT ORIENTATION CONTRACT")
            print("  From v_entity_current an agent can use without reading source:")
            print(f"    qualified_name   = {row['qualified_name']}")
            print(f"    entity_type      = {row['entity_type']}")
            print(f"    file_path        = {row['file_path']}")
            print(f"    start_line       = {row['start_line']}")
            print(f"    end_line         = {row['end_line']}")
            print(f"    detection_method = {row['detection_method']}")
            print(f"    language         = {row['language']}")


# ---------------------------------------------------------------------------
# D. Stale behaviour
# ---------------------------------------------------------------------------


def test_stale_behaviour() -> None:
    print("\nD. Stale behaviour")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        py_file = tmp / "thing.py"
        py_file.write_text("def f():\n    pass\n", encoding="utf-8")
        run_ci(["scan", str(tmp)], tmp)

        stored = q1(tmp,
            "SELECT file_hash FROM source_file WHERE file_path = 'thing.py'")
        _assert("D: source_file row exists after scan", stored is not None)
        original_hash = stored["file_hash"] if stored else None

        # Modify the file — do not rescan
        py_file.write_text("def f():\n    return 1\n", encoding="utf-8")

        r = run_ci(["stale"], tmp)
        _assert("D: stale exits 0", r.returncode == 0)
        _assert("D: stale reports modified", "modified" in r.stdout, r.stdout.strip())

        # The stored hash should still reflect the pre-modification scan
        stored2 = q1(tmp,
            "SELECT file_hash FROM source_file WHERE file_path = 'thing.py'")
        if stored2 and original_hash:
            _assert_eq("D: file_hash not refreshed by stale alone",
                stored2["file_hash"], original_hash)


# ---------------------------------------------------------------------------
# E. SyntaxError / failed extraction
# ---------------------------------------------------------------------------


def test_syntax_error_behaviour() -> None:
    print("\nE. SyntaxError / failed extraction")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        py_file = tmp / "syn.py"
        py_file.write_text("def good():\n    pass\n", encoding="utf-8")
        run_ci(["scan", str(tmp)], tmp)

        good_row = q1(tmp,
            "SELECT file_hash FROM source_file WHERE file_path = 'syn.py'")
        original_hash = good_row["file_hash"] if good_row else None

        # Introduce a SyntaxError
        py_file.write_text("def good(:\n    pass\n", encoding="utf-8")

        r2 = run_ci(["scan", str(tmp)], tmp)
        _assert("E: scan exits 0 despite SyntaxError", r2.returncode == 0)
        _assert("E: warning printed to stderr",
            "warning" in r2.stderr.lower(), r2.stderr.strip())
        _assert("E: stderr mentions extraction failed",
            "extraction failed" in r2.stderr.lower(), r2.stderr.strip())

        # Previous entity still active
        row = q1(tmp,
            "SELECT lifecycle_status FROM code_entity "
            "WHERE qualified_name = 'syn.good'")
        _assert("E: good entity still in DB", row is not None)
        if row:
            _assert_eq("E: good entity still active",
                row["lifecycle_status"], "active")

        # file_hash must not have been refreshed
        row2 = q1(tmp,
            "SELECT file_hash FROM source_file WHERE file_path = 'syn.py'")
        if row2 and original_hash:
            _assert_eq("E: file_hash not refreshed after SyntaxError",
                row2["file_hash"], original_hash)

        # stale must report the file as modified
        r3 = run_ci(["stale"], tmp)
        _assert("E: stale reports modified after SyntaxError",
            "modified" in r3.stdout, r3.stdout.strip())


# ---------------------------------------------------------------------------
# F. Removed and restored entity
# ---------------------------------------------------------------------------


def test_removed_and_restored() -> None:
    print("\nF. Removed and restored entity")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        py_file = tmp / "lifespan.py"
        content_both = "def keeper():\n    pass\n\ndef gone():\n    pass\n"
        content_one  = "def keeper():\n    pass\n"

        # Initial scan — both functions present
        py_file.write_text(content_both, encoding="utf-8")
        run_ci(["scan", str(tmp)], tmp)

        # Remove 'gone'
        py_file.write_text(content_one, encoding="utf-8")
        run_ci(["scan", str(tmp)], tmp)

        # gone should be lifecycle_status='removed' and absent from v_entity_current
        row = q1(tmp,
            "SELECT lifecycle_status FROM code_entity "
            "WHERE qualified_name = 'lifespan.gone'")
        _assert("F: gone entity in DB after removal", row is not None)
        if row:
            _assert_eq("F: gone lifecycle_status = removed",
                row["lifecycle_status"], "removed")

        in_view = q1(tmp,
            "SELECT qualified_name FROM v_entity_current "
            "WHERE qualified_name = 'lifespan.gone'")
        _assert("F: gone absent from v_entity_current", in_view is None)

        drift_removed = q1(tmp,
            "SELECT drift_kind FROM drift_event de "
            "JOIN code_entity ce ON ce.id = de.entity_id "
            "WHERE ce.qualified_name = 'lifespan.gone' "
            "  AND de.drift_kind = 'removed'")
        _assert("F: removed drift event recorded", drift_removed is not None)

        # Restore 'gone'
        py_file.write_text(content_both, encoding="utf-8")
        run_ci(["scan", str(tmp)], tmp)

        row2 = q1(tmp,
            "SELECT lifecycle_status FROM code_entity "
            "WHERE qualified_name = 'lifespan.gone'")
        if row2:
            _assert_eq("F: gone restored to active",
                row2["lifecycle_status"], "active")

        in_view2 = q1(tmp,
            "SELECT qualified_name FROM v_entity_current "
            "WHERE qualified_name = 'lifespan.gone'")
        _assert("F: gone reappears in v_entity_current", in_view2 is not None)

        drift_restored = q1(tmp,
            "SELECT drift_kind FROM drift_event de "
            "JOIN code_entity ce ON ce.id = de.entity_id "
            "WHERE ce.qualified_name = 'lifespan.gone' "
            "  AND de.drift_kind = 'restored'")
        _assert("F: restored drift event recorded", drift_restored is not None)


# ---------------------------------------------------------------------------
# G. SQL schema line numbers and qnames
# ---------------------------------------------------------------------------


def test_sql_qnames() -> None:
    print("\nG. SQL schema line numbers and file-path-aware qnames")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # Two SQL files both define a table named 'users'.
        # The scanner must produce distinct qualified names.
        #
        # schema_a.sql:
        #   1: CREATE TABLE users (
        #   2:     id   INTEGER PRIMARY KEY,
        #   3:     name TEXT NOT NULL
        #   4: );
        (tmp / "schema_a.sql").write_text(
            textwrap.dedent("""\
                CREATE TABLE users (
                    id   INTEGER PRIMARY KEY,
                    name TEXT NOT NULL
                );
                """),
            encoding="utf-8",
        )

        # schema_b.sql:
        #   1: CREATE TABLE users (
        #   2:     id    INTEGER PRIMARY KEY,
        #   3:     email TEXT NOT NULL
        #   4: );
        (tmp / "schema_b.sql").write_text(
            textwrap.dedent("""\
                CREATE TABLE users (
                    id    INTEGER PRIMARY KEY,
                    email TEXT NOT NULL
                );
                """),
            encoding="utf-8",
        )

        run_ci(["scan", str(tmp)], tmp)

        # Both tables must exist under distinct prefixes
        row_a = q1(tmp,
            "SELECT start_line FROM v_entity_current "
            "WHERE qualified_name = 'schema_a.users'")
        row_b = q1(tmp,
            "SELECT start_line FROM v_entity_current "
            "WHERE qualified_name = 'schema_b.users'")
        _assert("G: schema_a.users exists", row_a is not None)
        _assert("G: schema_b.users exists (no qname collision)", row_b is not None)
        if row_a:
            _assert_eq("G: schema_a.users start_line", row_a["start_line"], 1)
        if row_b:
            _assert_eq("G: schema_b.users start_line", row_b["start_line"], 1)

        # Column qnames: sql_file_prefix.table.column
        col_a_id = q1(tmp,
            "SELECT start_line FROM v_entity_current "
            "WHERE qualified_name = 'schema_a.users.id'")
        col_b_id = q1(tmp,
            "SELECT start_line FROM v_entity_current "
            "WHERE qualified_name = 'schema_b.users.id'")
        col_a_name = q1(tmp,
            "SELECT start_line FROM v_entity_current "
            "WHERE qualified_name = 'schema_a.users.name'")
        col_b_email = q1(tmp,
            "SELECT start_line FROM v_entity_current "
            "WHERE qualified_name = 'schema_b.users.email'")

        _assert("G: schema_a.users.id exists",              col_a_id is not None)
        _assert("G: schema_b.users.id exists (distinct)",   col_b_id is not None)
        _assert("G: schema_a.users.name exists",            col_a_name is not None)
        _assert("G: schema_b.users.email exists (distinct)", col_b_email is not None)

        if col_a_id:
            _assert_eq("G: schema_a.users.id start_line",   col_a_id["start_line"],   2)
        if col_b_id:
            _assert_eq("G: schema_b.users.id start_line",   col_b_id["start_line"],   2)
        if col_a_name:
            _assert_eq("G: schema_a.users.name start_line", col_a_name["start_line"], 3)
        if col_b_email:
            _assert_eq("G: schema_b.users.email start_line", col_b_email["start_line"], 3)


# ---------------------------------------------------------------------------
# H. New agent commands: status, orient, drifts
# ---------------------------------------------------------------------------


def test_new_commands() -> None:
    print("\nH. New agent commands: status, orient, drifts")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # Create a module with a documented function so orient has something
        # rich to print.
        (tmp / "greet.py").write_text(
            'def hello(name):\n    """Say hello."""\n    return f"Hello, {name}"\n',
            encoding="utf-8",
        )
        run_ci(["scan", str(tmp)], tmp)

        # status
        r_status = run_ci(["status"], tmp)
        _assert("H: status exits 0", r_status.returncode == 0, r_status.stderr.strip())
        _assert("H: status shows schema_version",
            "schema_version" in r_status.stdout, r_status.stdout[:200])
        _assert("H: status shows active entities count",
            "active entities" in r_status.stdout, r_status.stdout[:200])
        _assert("H: status shows latest scan run",
            "latest scan run" in r_status.stdout, r_status.stdout[:200])

        # orient — look up a known function
        r_orient = run_ci(["orient", "hello"], tmp)
        _assert("H: orient exits 0", r_orient.returncode == 0, r_orient.stderr.strip())
        _assert("H: orient shows qualified_name",
            "greet.hello" in r_orient.stdout, r_orient.stdout.strip())
        _assert("H: orient shows file_path",
            "greet.py" in r_orient.stdout, r_orient.stdout.strip())
        _assert("H: orient shows start_line",
            ":1-" in r_orient.stdout, r_orient.stdout.strip())
        _assert("H: orient shows language",
            "python" in r_orient.stdout, r_orient.stdout.strip())
        _assert("H: orient shows detection method",
            "python_ast" in r_orient.stdout, r_orient.stdout.strip())

        # drifts — immediately after a fresh scan every entity is 'added'
        r_drifts = run_ci(["drifts"], tmp)
        _assert("H: drifts exits 0", r_drifts.returncode == 0, r_drifts.stderr.strip())
        _assert("H: drifts shows open drift events",
            "added" in r_drifts.stdout or "Open drifts" in r_drifts.stdout,
            r_drifts.stdout.strip())


# ---------------------------------------------------------------------------
# I. Published command registry: commands, help-json
# ---------------------------------------------------------------------------


def test_published_command_registry() -> None:
    print("\nI. Published command registry: commands, help-json")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # A simple module so status/orient/stale have something to report.
        (tmp / "greet.py").write_text(
            'def hello(name):\n    """Say hello."""\n    return f"Hello, {name}"\n',
            encoding="utf-8",
        )
        run_ci(["scan", str(tmp)], tmp)

        # commands
        r_cmds = run_ci(["commands"], tmp)
        _assert("I: commands exits 0", r_cmds.returncode == 0, r_cmds.stderr.strip())
        for token in ("scan", "status", "orient", "drifts"):
            _assert(f"I: commands output includes {token}",
                token in r_cmds.stdout, r_cmds.stdout.strip())

        # help-json
        r_help = run_ci(["help-json"], tmp)
        _assert("I: help-json exits 0", r_help.returncode == 0, r_help.stderr.strip())
        parsed = None
        try:
            parsed = json.loads(r_help.stdout)
            _assert("I: help-json parses as JSON", True)
        except json.JSONDecodeError as exc:
            _assert("I: help-json parses as JSON", False, str(exc))
        if parsed is not None:
            names = {entry.get("command") for entry in parsed}
            _assert("I: help-json describes published commands",
                {"scan", "status", "orient", "drifts"}.issubset(names),
                sorted(names))

            # summary derived from docstrings
            scan_entry = next((e for e in parsed if e.get("command") == "scan"), None)
            _assert("I: help-json scan entry has non-empty summary",
                scan_entry is not None and bool(scan_entry.get("summary")),
                str(scan_entry))

            # long_description field present (may be empty string for simple commands)
            _assert("I: help-json scan entry has long_description field",
                scan_entry is not None and "long_description" in scan_entry,
                str(scan_entry))

            # help-json entry includes help_json alias
            help_entry = next((e for e in parsed if e.get("command") == "help-json"), None)
            _assert("I: help-json entry exists for help-json command",
                help_entry is not None,
                str([e.get("command") for e in parsed]))
            if help_entry is not None:
                aliases = help_entry.get("aliases", [])
                _assert("I: help-json entry includes help_json alias",
                    "help_json" in aliases,
                    str(aliases))

            # help-json long_description present
            _assert("I: help-json entry has long_description field",
                help_entry is not None and "long_description" in (help_entry or {}),
                str(help_entry))

        # runpy compatibility: module-level code must not blow up
        runpy_script = textwrap.dedent(f"""\
            import runpy, sys
            sys.argv = ['codeintel.py']
            runpy.run_path({CODEINTEL_PY!r}, run_name='__main__')
        """)
        r_runpy = subprocess.run(
            [PYTHON, "-c", runpy_script],
            cwd=str(tmp),
            capture_output=True,
            text=True,
        )
        _assert("I: runpy compatibility (no-args exits 0)",
            r_runpy.returncode == 0, r_runpy.stderr.strip())

        # Existing commands still work via the registry.
        r_status = run_ci(["status"], tmp)
        _assert("I: status still works", r_status.returncode == 0, r_status.stderr.strip())

        r_orient = run_ci(["orient", "hello"], tmp)
        _assert("I: orient still works", r_orient.returncode == 0, r_orient.stderr.strip())
        _assert("I: orient still finds entity",
            "greet.hello" in r_orient.stdout, r_orient.stdout.strip())

        r_stale = run_ci(["stale"], tmp)
        _assert("I: stale still works", r_stale.returncode == 0, r_stale.stderr.strip())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
# J. Path / repo-root semantics
# ---------------------------------------------------------------------------


def test_path_root_semantics() -> None:
    print("\nJ. Path / repo-root semantics")

    # J1 — whole-repo git scan stores paths relative to the git root.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / ".git").mkdir()
        (tmp / "main.py").write_text("def foo(): pass\n", encoding="utf-8")
        run_ci(["scan", str(tmp)], tmp)
        row = q1(tmp,
            "SELECT file_path FROM source_file WHERE file_path = 'main.py'")
        _assert("J1: whole-repo scan stores bare repo-relative path",
            row is not None, "expected file_path='main.py'")

    # J2 — subdirectory scan stores paths relative to the git root,
    #       not relative to the subdirectory.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / ".git").mkdir()
        sub = tmp / "sub"
        sub.mkdir()
        (sub / "util.py").write_text("def bar(): pass\n", encoding="utf-8")
        (tmp / "root_mod.py").write_text("def baz(): pass\n", encoding="utf-8")
        # Scan only the subdirectory.
        run_ci(["scan", str(sub)], tmp)
        row = q1(tmp,
            "SELECT file_path FROM source_file WHERE file_path = 'sub/util.py'")
        _assert("J2: subdirectory scan stores path relative to git root",
            row is not None, "expected file_path='sub/util.py'")

    # J3 — single-file scan stores the correct repo-relative path.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / ".git").mkdir()
        pkg = tmp / "pkg"
        pkg.mkdir()
        (pkg / "mod.py").write_text("def func(): pass\n", encoding="utf-8")
        run_ci(["scan", str(pkg / "mod.py")], tmp)
        row = q1(tmp,
            "SELECT file_path FROM source_file WHERE file_path = 'pkg/mod.py'")
        _assert("J3: single-file scan stores repo-relative path",
            row is not None, "expected file_path='pkg/mod.py'")

    # J4 — non-git directory scan uses the scan root itself; paths are
    #       relative to that root, not to cwd.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # No .git intentionally.
        (tmp / "foo.py").write_text("def alpha(): pass\n", encoding="utf-8")
        run_ci(["scan", str(tmp)], tmp)
        row = q1(tmp,
            "SELECT file_path FROM source_file WHERE file_path = 'foo.py'")
        _assert("J4: non-git scan stores bare filename as file_path",
            row is not None, "expected file_path='foo.py'")
        # Sanity: root_path stored in git_repo should match the scan root.
        repo_row = q1(tmp, "SELECT root_path FROM git_repo LIMIT 1")
        _assert("J4: non-git scan stores scan dir as root_path",
            repo_row is not None and
            Path(repo_row["root_path"]).resolve() == tmp.resolve(),
            f"got root_path={repo_row['root_path'] if repo_row else None}")

    # J5 — stale detects a file modified after a scan.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / ".git").mkdir()
        f = tmp / "hello.py"
        f.write_text("def hi(): pass\n", encoding="utf-8")
        run_ci(["scan", str(tmp)], tmp)
        f.write_text("def hi(): return 1\n", encoding="utf-8")
        r = run_ci(["stale"], tmp)
        _assert("J5: stale exits 0 after file modification",
            r.returncode == 0, r.stderr.strip())
        _assert("J5: stale reports modified file",
            "hello.py" in r.stdout, r.stdout.strip())

    # J6 — directory scan detects a previously scanned file that was deleted.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / ".git").mkdir()
        (tmp / "alive.py").write_text("def stay(): pass\n", encoding="utf-8")
        dead = tmp / "dead.py"
        dead.write_text("def gone(): pass\n", encoding="utf-8")
        run_ci(["scan", str(tmp)], tmp)
        dead.unlink()
        run_ci(["scan", str(tmp)], tmp)
        row = q1(tmp,
            "SELECT lifecycle_status FROM code_entity "
            "WHERE qualified_name = 'dead.gone'")
        _assert("J6: deleted file's entity marked removed",
            row is not None and row["lifecycle_status"] == "removed",
            f"got {dict(row) if row else None}")

    # J7 — subdirectory scan does NOT mark files outside that subdirectory
    #       as vanished.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / ".git").mkdir()
        inner = tmp / "inner"
        inner.mkdir()
        (tmp / "outer.py").write_text("def outside(): pass\n", encoding="utf-8")
        (inner / "inner.py").write_text("def inside(): pass\n", encoding="utf-8")
        # Full-repo scan first — both files registered.
        run_ci(["scan", str(tmp)], tmp)
        # Subdirectory-only rescan — must not vanish outer.py.
        run_ci(["scan", str(inner)], tmp)
        row = q1(tmp,
            "SELECT lifecycle_status FROM code_entity "
            "WHERE qualified_name = 'outer.outside'")
        _assert("J7: subdir rescan does not vanish entities outside subdir",
            row is not None and row["lifecycle_status"] == "active",
            f"got {dict(row) if row else None}")


# ---------------------------------------------------------------------------
# K. Python AST transparent containers
# ---------------------------------------------------------------------------


def test_ast_transparent_containers() -> None:
    print("\nK. Python AST transparent containers")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        (tmp / "containers.py").write_text(textwrap.dedent("""\
            import contextlib

            if True:
                def inside_if(): pass

            if True:
                class InsideIfClass: pass

            if True:
                async def async_inside_if(): pass

            if False:
                pass
            else:
                def inside_else(): pass

            try:
                def inside_try(): pass
            except Exception:
                pass

            try:
                pass
            except Exception:
                def inside_except(): pass

            try:
                pass
            finally:
                def inside_finally(): pass

            for _ in []:
                def inside_for(): pass

            while False:
                def inside_while(): pass

            with contextlib.suppress(Exception):
                def inside_with(): pass

            def outer():
                if True:
                    def inner(): pass
        """), encoding="utf-8")

        run_ci(["scan", str(tmp)], tmp)

        def _exists(qname: str) -> bool:
            return q1(tmp,
                "SELECT qualified_name FROM v_entity_current "
                "WHERE qualified_name = ?",
                (qname,)) is not None

        _assert("K1:  function inside module-level if",
            _exists("containers.inside_if"))
        _assert("K2:  class inside module-level if",
            _exists("containers.InsideIfClass"))
        _assert("K3:  async function inside module-level if",
            _exists("containers.async_inside_if"))
        _assert("K4:  function inside else block",
            _exists("containers.inside_else"))
        _assert("K5:  function inside try body",
            _exists("containers.inside_try"))
        _assert("K6:  function inside except handler",
            _exists("containers.inside_except"))
        _assert("K7:  function inside finally",
            _exists("containers.inside_finally"))
        _assert("K8:  function inside for body",
            _exists("containers.inside_for"))
        _assert("K9:  function inside while body",
            _exists("containers.inside_while"))
        _assert("K10: function inside with body",
            _exists("containers.inside_with"))
        _assert("K11: nested function inside function-level if",
            _exists("containers.outer.inner"))

        # Parent of inner must be outer, not the if-node.
        inner_row = q1(tmp,
            """
            SELECT ce_parent.qualified_name AS parent_qname
            FROM code_entity ce
            JOIN code_entity ce_parent ON ce_parent.id = ce.parent_entity_id
            WHERE ce.qualified_name = 'containers.outer.inner'
            """)
        _assert("K11: outer.inner parent qname is containers.outer",
            inner_row is not None
            and inner_row["parent_qname"] == "containers.outer",
            f"got {dict(inner_row) if inner_row else None}")

        # No duplicate entity rows for any containers.* entity.
        dups = qall(tmp,
            "SELECT qualified_name, COUNT(*) AS n FROM code_entity "
            "WHERE qualified_name LIKE 'containers.%' "
            "GROUP BY qualified_name HAVING n > 1")
        _assert("K:   no duplicate entity rows",
            len(dups) == 0,
            f"duplicates: {[dict(r) for r in dups]}")

    # match/case — guarded: only run on Python 3.10+
    if sys.version_info >= (3, 10):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "matchmod.py").write_text(textwrap.dedent("""\
                def check(x):
                    match x:
                        case 1:
                            def inside_match(): pass
            """), encoding="utf-8")
            run_ci(["scan", str(tmp)], tmp)
            _assert("K12: function inside match/case (Python 3.10+)",
                q1(tmp,
                    "SELECT qualified_name FROM v_entity_current "
                    "WHERE qualified_name = 'matchmod.check.inside_match'"
                ) is not None)


# ---------------------------------------------------------------------------
# L. Python call observations
# ---------------------------------------------------------------------------


def test_call_observations() -> None:
    print("\nL. Python call observations")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        src = textwrap.dedent("""\
            # calls.py — test module for call observation extraction

            def direct_name():
                load_config()

            def attribute_call(service):
                service.handle()

            class Service:
                def run(self):
                    self.save()

            class Child(Base):
                def run(self):
                    super().run()

            def outer():
                setup()
                def inner():
                    work()

            def run_with_flag(flag):
                if flag:
                    do_it()
        """)
        (tmp / "calls.py").write_text(src)
        res = run_ci(["scan", "calls.py"], tmp)
        _assert("L: scan exits 0", res.returncode == 0, res.stderr)

        # ------------------------------------------------------------------
        # L1 — direct name call
        row = q1(
            tmp,
            "SELECT call_kind FROM v_call_observation "
            "WHERE caller_qualified_name LIKE '%.direct_name' "
            "AND call_text = 'load_config'",
        )
        _assert("L1: name_call present", row is not None, "load_config not found")
        if row:
            _assert_eq("L1: name_call kind", row["call_kind"], "name_call")

        # ------------------------------------------------------------------
        # L2 — attribute call
        row = q1(
            tmp,
            "SELECT call_kind FROM v_call_observation "
            "WHERE caller_qualified_name LIKE '%.attribute_call' "
            "AND call_text = 'service.handle'",
        )
        _assert("L2: attribute_call present", row is not None, "service.handle not found")
        if row:
            _assert_eq("L2: attribute_call kind", row["call_kind"], "attribute_call")

        # ------------------------------------------------------------------
        # L3 — self call
        row = q1(
            tmp,
            "SELECT call_kind FROM v_call_observation "
            "WHERE caller_qualified_name LIKE '%.Service.run' "
            "AND call_text = 'self.save'",
        )
        _assert("L3: self_call present", row is not None, "self.save not found")
        if row:
            _assert_eq("L3: self_call kind", row["call_kind"], "self_call")

        # ------------------------------------------------------------------
        # L4 — super call
        row = q1(
            tmp,
            "SELECT call_kind FROM v_call_observation "
            "WHERE caller_qualified_name LIKE '%.Child.run' "
            "AND call_text = 'super.run'",
        )
        _assert("L4: super_call present", row is not None, "super.run not found")
        if row:
            _assert_eq("L4: super_call kind", row["call_kind"], "super_call")

        # ------------------------------------------------------------------
        # L5 — nested function ownership: setup under outer, work under inner
        row_setup = q1(
            tmp,
            "SELECT 1 FROM v_call_observation "
            "WHERE caller_qualified_name LIKE '%.outer' "
            "AND call_text = 'setup'",
        )
        _assert("L5: setup under outer", row_setup is not None)

        row_work_inner = q1(
            tmp,
            "SELECT 1 FROM v_call_observation "
            "WHERE caller_qualified_name LIKE '%.outer.inner' "
            "AND call_text = 'work'",
        )
        _assert("L5: work under outer.inner", row_work_inner is not None)

        # work must NOT appear under outer
        row_work_outer = q1(
            tmp,
            "SELECT 1 FROM v_call_observation "
            "WHERE caller_qualified_name LIKE '%.outer' "
            "AND call_text = 'work'",
        )
        _assert("L5: work NOT double-counted under outer", row_work_outer is None)

        # ------------------------------------------------------------------
        # L6 — transparent container: do_it attributed to run_with_flag
        row = q1(
            tmp,
            "SELECT 1 FROM v_call_observation "
            "WHERE caller_qualified_name LIKE '%.run_with_flag' "
            "AND call_text = 'do_it'",
        )
        _assert("L6: call inside if attributed to enclosing function", row is not None)

        # ------------------------------------------------------------------
        # L7 — module and class body calls are NOT in scope for this bundle.
        # Only python_function / python_method / python_nested_function
        # entities produce call observations.
        #
        # Verify entity types that have at least one call are function-like.
        rows = qall(
            tmp,
            "SELECT DISTINCT caller_entity_type FROM v_call_observation",
        )
        non_function_types = [
            r["caller_entity_type"]
            for r in rows
            if r["caller_entity_type"] not in (
                "python_function",
                "python_method",
                "python_nested_function",
            )
        ]
        _assert(
            "L7: only function-like entities emit call observations",
            non_function_types == [],
            f"unexpected types: {non_function_types}",
        )


# ---------------------------------------------------------------------------


def main() -> None:
    print("=" * 60)
    print("CodeIntel confidence harness")
    print("=" * 60)

    test_python_line_numbers()
    test_decorator_lines_and_drift()
    test_agent_orientation_contract()
    test_stale_behaviour()
    test_syntax_error_behaviour()
    test_removed_and_restored()
    test_sql_qnames()
    test_new_commands()
    test_published_command_registry()
    test_path_root_semantics()
    test_ast_transparent_containers()
    test_call_observations()

    total  = len(_results)
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = total - passed

    print()
    print("=" * 60)

    if failed == 0:
        print(f"All {total} checks passed.")
        print()
        print("CodeIntel confidence harness passed.")
        print("Agents may use the DB for orientation and line lookup")
        print("  when stale reports current.")
        print("Agents should still read source before editing or")
        print("  when stale reports modified/missing.")
    else:
        print(f"{passed}/{total} checks passed.  {failed} FAILED.")
        print()
        for name, ok, detail in _results:
            if not ok:
                print(f"  FAIL  {name}  — {detail}")
        print()
        print("CodeIntel confidence harness FAILED.")

    print("=" * 60)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
