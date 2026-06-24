#!/usr/bin/env python3
"""
Tests for CodeIntelDbUnionConnection.

Covers:
    - constructor
    - execute returns self
    - fetchall
    - query
    - close
    - cursor raises NotImplementedError
    - SELECT accepted
    - WITH accepted
    - INSERT rejected
    - ATTACH rejected
    - multiple statements rejected
    - readonly URI generation
    - single DB query
    - two DB merge
    - repo metadata injection
    - object-based repos (attribute access)
    - codeintel_db_path alias for db_path
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from codeintel import CodeIntelDbUnionConnection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_db(rows: list[tuple]) -> str:
    """
    Create a temporary SQLite file with a 'things' table containing (id, val).
    Returns the file path as a string.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    path = tmp.name
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE things (id INTEGER PRIMARY KEY, val TEXT)")
    conn.executemany("INSERT INTO things(id, val) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    return path


class _RepoObject:
    """Minimal object-style repo for testing attribute access."""

    def __init__(self, name, root, db_path=None, codeintel_db_path=None):
        self.name = name
        self.root = root
        if db_path is not None:
            self.db_path = db_path
        if codeintel_db_path is not None:
            self.codeintel_db_path = codeintel_db_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConstructor(unittest.TestCase):
    def test_constructor_stores_repos(self):
        repos = [{"name": "r", "root": "/r", "db_path": "/r/.codeintel/x.sqlite"}]
        conn = CodeIntelDbUnionConnection(repos)
        self.assertIsInstance(conn, CodeIntelDbUnionConnection)

    def test_constructor_accepts_empty_repos(self):
        conn = CodeIntelDbUnionConnection([])
        self.assertIsInstance(conn, CodeIntelDbUnionConnection)


class TestPublicApiStubs(unittest.TestCase):
    def setUp(self):
        self.conn = CodeIntelDbUnionConnection([])

    def test_close_does_not_raise(self):
        self.conn.close()  # must not raise

    def test_cursor_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            self.conn.cursor()


class TestFetchall(unittest.TestCase):
    def test_fetchall_returns_empty_list_before_execute(self):
        conn = CodeIntelDbUnionConnection([])
        self.assertEqual(conn.fetchall(), [])


class TestAssertSupportedSql(unittest.TestCase):
    """Unit tests for _assert_supported_sql (called inside execute)."""

    def _ok(self, sql):
        CodeIntelDbUnionConnection._assert_supported_sql(sql)

    def _fail(self, sql):
        with self.assertRaises(ValueError):
            CodeIntelDbUnionConnection._assert_supported_sql(sql)

    # Accepted
    def test_select_accepted(self):
        self._ok("SELECT 1")

    def test_select_mixed_case_accepted(self):
        self._ok("select * from foo")

    def test_with_accepted(self):
        self._ok("WITH cte AS (SELECT 1) SELECT * FROM cte")

    def test_with_mixed_case_accepted(self):
        self._ok("with x as (select 1) select * from x")

    # Rejected single keywords
    def test_insert_rejected(self):
        self._fail("INSERT INTO t VALUES (1)")

    def test_update_rejected(self):
        self._fail("UPDATE t SET x = 1")

    def test_delete_rejected(self):
        self._fail("DELETE FROM t")

    def test_replace_rejected(self):
        self._fail("REPLACE INTO t VALUES (1)")

    def test_create_rejected(self):
        self._fail("CREATE TABLE t (x INT)")

    def test_drop_rejected(self):
        self._fail("DROP TABLE t")

    def test_alter_rejected(self):
        self._fail("ALTER TABLE t ADD COLUMN x INT")

    def test_vacuum_rejected(self):
        self._fail("VACUUM")

    def test_pragma_rejected(self):
        self._fail("PRAGMA table_info(foo)")

    def test_attach_rejected(self):
        self._fail("ATTACH DATABASE 'x.db' AS x")

    def test_detach_rejected(self):
        self._fail("DETACH DATABASE x")

    # Multiple statements
    def test_multiple_statements_rejected(self):
        self._fail("SELECT 1; SELECT 2")

    def test_multiple_statements_with_whitespace_rejected(self):
        self._fail("SELECT 1;  SELECT 2")

    def test_trailing_semicolon_only_accepted(self):
        # A trailing semicolon with no following statement is OK.
        self._ok("SELECT 1;")

    def test_empty_sql_rejected(self):
        self._fail("")

    def test_whitespace_only_rejected(self):
        self._fail("   ")


class TestReadonlyUri(unittest.TestCase):
    def test_unix_style_path(self):
        uri = CodeIntelDbUnionConnection._readonly_sqlite_uri("/data/mydb.sqlite")
        # URI must use the file:// scheme, have three leading slashes before
        # the absolute path component, and carry mode=ro.
        self.assertTrue(uri.startswith("file:///"), uri)
        self.assertIn("mydb.sqlite", uri)
        self.assertIn("mode=ro", uri)

    def test_windows_style_path(self):
        # Simulate what resolve().as_posix() returns on Windows
        raw = "C:/analytics/mydb.sqlite"
        uri = CodeIntelDbUnionConnection._readonly_sqlite_uri(raw)
        self.assertIn("mode=ro", uri)
        # URI must start with file:/// (three slashes before path/drive letter)
        self.assertTrue(uri.startswith("file:///"))

    def test_uri_ends_with_mode_ro(self):
        uri = CodeIntelDbUnionConnection._readonly_sqlite_uri("/some/path.db")
        self.assertTrue(uri.endswith("?mode=ro") or "mode=ro" in uri)


class TestSingleDbQuery(unittest.TestCase):
    def setUp(self):
        self.db_path = _make_test_db([(1, "alpha"), (2, "beta")])
        self.repos = [{"name": "testrepo", "root": "/tmp/testrepo", "db_path": self.db_path}]

    def tearDown(self):
        Path(self.db_path).unlink(missing_ok=True)

    def test_execute_returns_self(self):
        conn = CodeIntelDbUnionConnection(self.repos)
        result = conn.execute("SELECT id, val FROM things ORDER BY id")
        self.assertIs(result, conn)

    def test_fetchall_returns_dicts(self):
        conn = CodeIntelDbUnionConnection(self.repos)
        conn.execute("SELECT id, val FROM things ORDER BY id")
        rows = conn.fetchall()
        self.assertIsInstance(rows, list)
        self.assertEqual(len(rows), 2)
        self.assertIsInstance(rows[0], dict)
        self.assertEqual(rows[0]["val"], "alpha")
        self.assertEqual(rows[1]["val"], "beta")

    def test_query_shorthand(self):
        conn = CodeIntelDbUnionConnection(self.repos)
        rows = conn.query("SELECT id, val FROM things ORDER BY id")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], 1)

    def test_execute_then_fetchall_consistency(self):
        conn = CodeIntelDbUnionConnection(self.repos)
        conn.execute("SELECT val FROM things WHERE id = 1")
        rows = conn.fetchall()
        self.assertEqual(rows[0]["val"], "alpha")

    def test_empty_result(self):
        conn = CodeIntelDbUnionConnection(self.repos)
        rows = conn.query("SELECT * FROM things WHERE id = 999")
        self.assertEqual(rows, [])


class TestTwoDbMerge(unittest.TestCase):
    def setUp(self):
        self.db1 = _make_test_db([(1, "from-repo1")])
        self.db2 = _make_test_db([(2, "from-repo2")])
        self.repos = [
            {"name": "repo1", "root": "/r1", "db_path": self.db1},
            {"name": "repo2", "root": "/r2", "db_path": self.db2},
        ]

    def tearDown(self):
        Path(self.db1).unlink(missing_ok=True)
        Path(self.db2).unlink(missing_ok=True)

    def test_rows_merged_from_both_repos(self):
        conn = CodeIntelDbUnionConnection(self.repos)
        rows = conn.query("SELECT id, val FROM things")
        vals = {r["val"] for r in rows}
        self.assertIn("from-repo1", vals)
        self.assertIn("from-repo2", vals)
        self.assertEqual(len(rows), 2)

    def test_execute_returns_self_multi_repo(self):
        conn = CodeIntelDbUnionConnection(self.repos)
        self.assertIs(conn.execute("SELECT 1 AS n"), conn)


class TestRepoMetadataInjection(unittest.TestCase):
    def setUp(self):
        self.db_path = _make_test_db([(1, "x")])
        self.repos = [
            {
                "name": "myrepo",
                "root": "/workspace/myrepo",
                "db_path": self.db_path,
            }
        ]

    def tearDown(self):
        Path(self.db_path).unlink(missing_ok=True)

    def test_repo_name_injected(self):
        conn = CodeIntelDbUnionConnection(self.repos)
        rows = conn.query("SELECT :__repo_name AS rn")
        self.assertEqual(rows[0]["rn"], "myrepo")

    def test_repo_root_injected(self):
        conn = CodeIntelDbUnionConnection(self.repos)
        rows = conn.query("SELECT :__repo_root AS rr")
        self.assertEqual(rows[0]["rr"], "/workspace/myrepo")

    def test_repo_db_path_injected(self):
        conn = CodeIntelDbUnionConnection(self.repos)
        rows = conn.query("SELECT :__repo_db_path AS rdp")
        self.assertEqual(rows[0]["rdp"], self.db_path)

    def test_user_dict_params_merged_with_repo_meta(self):
        conn = CodeIntelDbUnionConnection(self.repos)
        rows = conn.query(
            "SELECT :__repo_name AS rn, :myval AS mv",
            {"myval": "hello"},
        )
        self.assertEqual(rows[0]["rn"], "myrepo")
        self.assertEqual(rows[0]["mv"], "hello")

    def test_positional_params_passed_through(self):
        conn = CodeIntelDbUnionConnection(self.repos)
        rows = conn.query("SELECT id, val FROM things WHERE id = ?", (1,))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 1)


class TestDiagnose(unittest.TestCase):
    def setUp(self):
        self.db_path = _make_test_db([])

    def tearDown(self):
        Path(self.db_path).unlink(missing_ok=True)

    def test_diagnose_returns_dict_with_repos_key(self):
        conn = CodeIntelDbUnionConnection(
            [{"name": "r", "root": "/r", "db_path": self.db_path}]
        )
        d = conn.diagnose()
        self.assertIn("repos", d)
        self.assertEqual(len(d["repos"]), 1)

    def test_diagnose_db_exists_true_for_existing_db(self):
        conn = CodeIntelDbUnionConnection(
            [{"name": "r", "root": "/r", "db_path": self.db_path}]
        )
        entry = conn.diagnose()["repos"][0]
        self.assertTrue(entry["db_exists"])
        self.assertIsNotNone(entry["db_size"])

    def test_diagnose_db_exists_false_for_missing_db(self):
        conn = CodeIntelDbUnionConnection(
            [{"name": "r", "root": "/r", "db_path": "/nonexistent/path.sqlite"}]
        )
        entry = conn.diagnose()["repos"][0]
        self.assertFalse(entry["db_exists"])
        self.assertIsNone(entry["db_size"])


class TestExecuteRejectsUnsafeSql(unittest.TestCase):
    """Confirm that execute() (not just _assert_supported_sql) raises ValueError."""

    def setUp(self):
        self.conn = CodeIntelDbUnionConnection([])

    def test_execute_insert_raises(self):
        with self.assertRaises(ValueError):
            self.conn.execute("INSERT INTO t VALUES (1)")

    def test_execute_attach_raises(self):
        with self.assertRaises(ValueError):
            self.conn.execute("ATTACH DATABASE 'x.db' AS x")

    def test_execute_multiple_statements_raises(self):
        with self.assertRaises(ValueError):
            self.conn.execute("SELECT 1; SELECT 2")


# ---------------------------------------------------------------------------
# Object-based repo access
# ---------------------------------------------------------------------------


class TestObjectBasedRepos(unittest.TestCase):
    """_repo_value() must support attribute-style access on repo objects."""

    def setUp(self):
        self.db_path = _make_test_db([(1, "obj-row")])

    def tearDown(self):
        Path(self.db_path).unlink(missing_ok=True)

    def test_query_via_repo_object_with_db_path(self):
        repo = _RepoObject(name="objrepo", root="/obj/root", db_path=self.db_path)
        conn = CodeIntelDbUnionConnection([repo])
        rows = conn.query("SELECT id, val FROM things")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["val"], "obj-row")

    def test_repo_name_injected_from_object(self):
        repo = _RepoObject(name="objrepo", root="/obj/root", db_path=self.db_path)
        conn = CodeIntelDbUnionConnection([repo])
        rows = conn.query("SELECT :__repo_name AS rn")
        self.assertEqual(rows[0]["rn"], "objrepo")

    def test_repo_root_injected_from_object(self):
        repo = _RepoObject(name="objrepo", root="/obj/root", db_path=self.db_path)
        conn = CodeIntelDbUnionConnection([repo])
        rows = conn.query("SELECT :__repo_root AS rr")
        self.assertEqual(rows[0]["rr"], "/obj/root")

    def test_execute_returns_self_for_object_repo(self):
        repo = _RepoObject(name="objrepo", root="/obj/root", db_path=self.db_path)
        conn = CodeIntelDbUnionConnection([repo])
        self.assertIs(conn.execute("SELECT 1 AS n"), conn)

    def test_two_object_repos_merged(self):
        db2 = _make_test_db([(2, "obj-row2")])
        try:
            repo1 = _RepoObject(name="r1", root="/r1", db_path=self.db_path)
            repo2 = _RepoObject(name="r2", root="/r2", db_path=db2)
            conn = CodeIntelDbUnionConnection([repo1, repo2])
            rows = conn.query("SELECT val FROM things")
            vals = {r["val"] for r in rows}
            self.assertIn("obj-row", vals)
            self.assertIn("obj-row2", vals)
        finally:
            Path(db2).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# codeintel_db_path alias
# ---------------------------------------------------------------------------


class TestCodeintelDbPathAlias(unittest.TestCase):
    """db_path must fall back to codeintel_db_path when db_path is absent."""

    def setUp(self):
        self.db_path = _make_test_db([(1, "alias-row")])

    def tearDown(self):
        Path(self.db_path).unlink(missing_ok=True)

    def test_dict_with_codeintel_db_path(self):
        repo = {"name": "r", "root": "/r", "codeintel_db_path": self.db_path}
        conn = CodeIntelDbUnionConnection([repo])
        rows = conn.query("SELECT val FROM things")
        self.assertEqual(rows[0]["val"], "alias-row")

    def test_object_with_codeintel_db_path(self):
        repo = _RepoObject(
            name="r", root="/r", codeintel_db_path=self.db_path
        )
        conn = CodeIntelDbUnionConnection([repo])
        rows = conn.query("SELECT val FROM things")
        self.assertEqual(rows[0]["val"], "alias-row")

    def test_db_path_takes_priority_over_codeintel_db_path(self):
        db2 = _make_test_db([(2, "secondary")])
        try:
            # db_path wins; codeintel_db_path is ignored when db_path present
            repo = {
                "name": "r",
                "root": "/r",
                "db_path": self.db_path,
                "codeintel_db_path": db2,
            }
            conn = CodeIntelDbUnionConnection([repo])
            rows = conn.query("SELECT val FROM things")
            self.assertEqual(rows[0]["val"], "alias-row")
        finally:
            Path(db2).unlink(missing_ok=True)

    def test_repo_db_path_meta_param_resolved_via_alias(self):
        repo = {"name": "r", "root": "/r", "codeintel_db_path": self.db_path}
        conn = CodeIntelDbUnionConnection([repo])
        rows = conn.query("SELECT :__repo_db_path AS rdp")
        self.assertEqual(rows[0]["rdp"], self.db_path)


if __name__ == "__main__":
    unittest.main()
