#!/usr/bin/env python3
"""Unit tests for src.diff_parser."""

from __future__ import annotations

import glob
import os
import unittest
from pathlib import Path

from src.diff_parser import parse_patch, _compress_intervals


class TestDiffParser(unittest.TestCase):
    def test_interval_compression(self) -> None:
        self.assertEqual(_compress_intervals([]), [])
        self.assertEqual(_compress_intervals([5]), [(5, 5)])
        self.assertEqual(_compress_intervals([1, 2, 3]), [(1, 3)])
        self.assertEqual(_compress_intervals([1, 2, 4, 5, 7]), [(1, 2), (4, 5), (7, 7)])
        self.assertEqual(_compress_intervals([10, 10, 11, 12]), [(10, 12)])

    def test_empty_patch(self) -> None:
        res = parse_patch("")
        self.assertTrue(res.is_empty)
        self.assertEqual(len(res.files), 0)

        res2 = parse_patch("   \n\n  ")
        self.assertTrue(res2.is_empty)

    def test_single_hunk_modification(self) -> None:
        patch = """diff --git a/sqlglot/dialects/tsql.py b/sqlglot/dialects/tsql.py
index 4daed29..81cf535 100644
--- a/sqlglot/dialects/tsql.py
+++ b/sqlglot/dialects/tsql.py
@@ -14,7 +14,7 @@ from sqlglot.typing.tsql import EXPRESSION_METADATA
 
 class TSQL(Dialect):
     # Week truncation follows @@DATEFIRST, which defaults to 7 (Sunday)
-    WEEK_OFFSET = -1
+    WEEK_OFFSET = -2
 
     LOG_BASE_FIRST = False
     TYPED_DIVISION = True
"""
        parsed = parse_patch(patch)
        self.assertFalse(parsed.is_empty)
        self.assertEqual(len(parsed.files), 1)

        f = parsed.files[0]
        self.assertEqual(f.path, "sqlglot/dialects/tsql.py")
        self.assertEqual(f.old_path, "sqlglot/dialects/tsql.py")
        self.assertEqual(f.new_path, "sqlglot/dialects/tsql.py")
        self.assertFalse(f.is_new)
        self.assertFalse(f.is_deleted)
        self.assertFalse(f.is_comment_only)
        self.assertEqual(f.modified_lines, [17])
        self.assertEqual(f.modified_ranges, [(17, 17)])

    def test_comment_only_detection(self) -> None:
        patch = """diff --git a/sqlglot/generators/sqlite.py b/sqlglot/generators/sqlite.py
index b46c7ae..14987a0 100644
--- a/sqlglot/generators/sqlite.py
+++ b/sqlglot/generators/sqlite.py
@@ -260,6 +260,7 @@ class SQLiteGenerator(generator.Generator):
         return arrow_json_extract_sql(self, expression)
 
     def jsonextractscalar_sql(self, expression: exp.JSONExtractScalar) -> str:
+        # Cheap check first
         if expression.args.get("json_subtype"):
"""
        parsed = parse_patch(patch)
        self.assertTrue(parsed.is_comment_only)
        self.assertFalse(parsed.is_whitespace_only)
        self.assertEqual(len(parsed.files), 1)
        self.assertTrue(parsed.files[0].is_comment_only)
        self.assertEqual(parsed.files[0].modified_lines, [263])

    def test_whitespace_only_detection(self) -> None:
        patch = """diff --git a/sqlglot/tokens.py b/sqlglot/tokens.py
--- a/sqlglot/tokens.py
+++ b/sqlglot/tokens.py
@@ -10,3 +10,3 @@
 def foo():
-    x = 1
+    x = 1   
     return x
"""
        parsed = parse_patch(patch)
        self.assertFalse(parsed.is_empty)
        self.assertTrue(parsed.is_whitespace_only)

    def test_new_and_deleted_files(self) -> None:
        new_patch = """diff --git a/dev/null b/sqlglot/new_dialect.py
new file mode 100644
--- /dev/null
+++ b/sqlglot/new_dialect.py
@@ -0,0 +1,3 @@
+from sqlglot.dialects.dialect import Dialect
+class NewDialect(Dialect):
+    pass
"""
        parsed_new = parse_patch(new_patch)
        self.assertEqual(len(parsed_new.files), 1)
        self.assertTrue(parsed_new.files[0].is_new)
        self.assertFalse(parsed_new.files[0].is_deleted)
        self.assertEqual(parsed_new.files[0].path, "sqlglot/new_dialect.py")
        self.assertIsNone(parsed_new.files[0].old_path)
        self.assertEqual(parsed_new.files[0].modified_ranges, [(1, 3)])

        del_patch = """diff --git a/sqlglot/old.py b/dev/null
deleted file mode 100644
--- a/sqlglot/old.py
+++ /dev/null
@@ -1,3 +0,0 @@
-def old():
-    pass
"""
        parsed_del = parse_patch(del_patch)
        self.assertEqual(len(parsed_del.files), 1)
        self.assertTrue(parsed_del.files[0].is_deleted)
        self.assertEqual(parsed_del.files[0].path, "sqlglot/old.py")
        self.assertIsNone(parsed_del.files[0].new_path)

    def test_multi_file_patch(self) -> None:
        patch = """diff --git a/sqlglot/optimizer/scope.py b/sqlglot/optimizer/scope.py
--- a/sqlglot/optimizer/scope.py
+++ b/sqlglot/optimizer/scope.py
@@ -7,3 +7,4 @@
 from sqlglot import exp
+# Comment
 from sqlglot.errors import OptimizeError
diff --git a/tests/fixtures/optimizer/qualify.sql b/tests/fixtures/optimizer/qualify.sql
--- a/tests/fixtures/optimizer/qualify.sql
+++ b/tests/fixtures/optimizer/qualify.sql
@@ -10,3 +10,3 @@
-SELECT a FROM x;
+SELECT b FROM x;
"""
        parsed = parse_patch(patch)
        self.assertEqual(len(parsed.files), 2)
        self.assertFalse(parsed.is_comment_only)
        self.assertTrue(parsed.files[0].is_comment_only)
        self.assertFalse(parsed.files[1].is_comment_only)

    def test_all_sample_commits(self) -> None:
        sample_patches = sorted(glob.glob("blast-radius-sample/sample/commits/*.patch"))
        self.assertEqual(len(sample_patches), 30)

        for p in sample_patches:
            cid = os.path.basename(p).replace(".patch", "")
            with open(p, "r", encoding="utf-8") as f:
                raw = f.read()
            parsed = parse_patch(raw)

            self.assertFalse(parsed.is_empty, f"Patch {cid} parsed as empty")
            self.assertGreater(len(parsed.files), 0, f"Patch {cid} has 0 files")

            for file_diff in parsed.files:
                self.assertTrue(bool(file_diff.path), f"File in {cid} has empty path")
                self.assertGreater(len(file_diff.modified_lines), 0, f"File {file_diff.path} in {cid} has 0 modified lines")

        # Verify comment commits specifically
        for cid in ["9u5k", "bap8", "f4xa"]:
            with open(f"blast-radius-sample/sample/commits/{cid}.patch", "r", encoding="utf-8") as f:
                raw = f.read()
            parsed = parse_patch(raw)
            self.assertTrue(parsed.is_comment_only, f"{cid} expected comment_only=True")


if __name__ == "__main__":
    unittest.main()
