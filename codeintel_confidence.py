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
# Main
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
