#!/usr/bin/env python3
"""Unit tests for src.impact_resolver."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.impact_resolver import ImpactResolver


class TestImpactResolver(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle_path = Path("artifacts/runtime_index.json")
        cls.resolver = ImpactResolver(cls.bundle_path)
        cls.answers = json.loads(Path("blast-radius-sample/sample/answers.json").read_text(encoding="utf-8"))["commits"]

    def test_empty_patch(self) -> None:
        res = self.resolver.resolve("")
        self.assertEqual(res, [])

    def test_comment_only_commits(self) -> None:
        for cid in ["9u5k", "bap8", "f4xa"]:
            patch = Path(f"blast-radius-sample/sample/commits/{cid}.patch").read_text(encoding="utf-8")
            res = self.resolver.resolve_with_metadata(patch)
            self.assertEqual(res["selected_tests"], [], f"Comment commit {cid} selected unexpected tests")
            self.assertEqual(res["reason"], "comment_only_non_sensitive")

    def test_fixture_commits(self) -> None:
        fixture_commits = ["2vdj", "6vvp", "egys", "f4bn", "gbhk", "mthh", "nwnx", "st8f"]
        for cid in fixture_commits:
            patch = Path(f"blast-radius-sample/sample/commits/{cid}.patch").read_text(encoding="utf-8")
            res = self.resolver.resolve(patch)
            expected = self.answers[cid]["failing"]
            self.assertEqual(res, expected, f"Fixture commit {cid} did not match exact expected test")

    def test_dialect_commit_535z(self) -> None:
        patch = Path("blast-radius-sample/sample/commits/535z.patch").read_text(encoding="utf-8")
        meta = self.resolver.resolve_with_metadata(patch)
        selected = meta["selected_tests"]
        expected_failing = self.answers["535z"]["failing"]  # [1434]
        for f in expected_failing:
            self.assertIn(f, selected, f"535z missed failing test {f}")
        self.assertLess(len(selected), 100, "535z selected unnecessarily large test set")

    def test_multi_file_commits(self) -> None:
        for cid in ["986c", "bvw2", "rszg"]:
            patch = Path(f"blast-radius-sample/sample/commits/{cid}.patch").read_text(encoding="utf-8")
            selected = set(self.resolver.resolve(patch))
            expected = set(self.answers[cid]["failing"])
            missed = expected - selected
            self.assertEqual(missed, set(), f"Multi commit {cid} missed tests: {missed}")

    def test_tokenizer_only_snowflake_4d63(self) -> None:
        patch = Path("blast-radius-sample/sample/commits/4d63.patch").read_text(encoding="utf-8")
        meta = self.resolver.resolve_with_metadata(patch)
        selected = set(meta["selected_tests"])
        self.assertIn("snowflake::tokenizer_only", meta["triggers"]["tier2_dialects"])

        # Must include test_snowflake.py (116 tests) and test_dialect.py (113 tests)
        snowflake_tests = set(self.resolver.by_file["tests/dialects/test_snowflake.py"])
        dialect_tests = set(self.resolver.by_file["tests/dialects/test_dialect.py"])
        expected_tests = snowflake_tests | dialect_tests
        self.assertEqual(selected, expected_tests)
        self.assertEqual(len(selected), 229)

        # Must NOT include optimizer fixture tests
        for opt_test in [1295, 1382, 1434, 1377, 1378]:
            self.assertNotIn(opt_test, selected, f"Optimizer test {opt_test} unexpectedly selected for tokenizer patch")

    def test_tokenizer_class_header_fallback(self) -> None:
        # Modifying the class definition line (131) must trigger conservative fallback
        patch = (
            "diff --git a/sqlglot/dialects/snowflake.py b/sqlglot/dialects/snowflake.py\n"
            "--- a/sqlglot/dialects/snowflake.py\n"
            "+++ b/sqlglot/dialects/snowflake.py\n"
            "@@ -131,1 +131,1 @@\n"
            "-    class Tokenizer(tokens.Tokenizer):\n"
            "+    class Tokenizer(custom.Tokenizer):\n"
        )
        meta = self.resolver.resolve_with_metadata(patch)
        self.assertNotIn("snowflake::tokenizer_only", meta["triggers"]["tier2_dialects"])
        self.assertIn("snowflake", meta["triggers"]["tier2_dialects"])
        # Should pull 258 tests (the full conservative set)
        self.assertGreater(len(meta["selected_tests"]), 200)

    def test_tokenizer_multi_hunk_mixed_fallback(self) -> None:
        # One hunk inside tokenizer, one hunk outside
        patch = (
            "diff --git a/sqlglot/dialects/snowflake.py b/sqlglot/dialects/snowflake.py\n"
            "--- a/sqlglot/dialects/snowflake.py\n"
            "+++ b/sqlglot/dialects/snowflake.py\n"
            "@@ -133,1 +133,1 @@\n"
            "-        NUMERIC_ESCAPES = {'x': (16, 2, 2, 0xFF)}\n"
            "+        NUMERIC_ESCAPES = {'x': (17, 2, 2, 0xFF)}\n"
            "@@ -195,1 +195,1 @@\n"
            "-    class Parser(parser.Parser):\n"
            "+    class Parser(custom.Parser):\n"
        )
        meta = self.resolver.resolve_with_metadata(patch)
        self.assertNotIn("snowflake::tokenizer_only", meta["triggers"]["tier2_dialects"])
        self.assertIn("snowflake", meta["triggers"]["tier2_dialects"])

    def test_tokenizer_deletion_inside_body(self) -> None:
        # Deletion of lines 133-135 strictly inside Snowflake Tokenizer
        patch = (
            "diff --git a/sqlglot/dialects/snowflake.py b/sqlglot/dialects/snowflake.py\n"
            "--- a/sqlglot/dialects/snowflake.py\n"
            "+++ b/sqlglot/dialects/snowflake.py\n"
            "@@ -133,3 +133,0 @@\n"
            "-        NUMERIC_ESCAPES = {'x': (16, 2, 2, 0xFF)}\n"
            "-        DROP_UNKNOWN_ESCAPES = True\n"
            "-        HEX_STRINGS = [('x\\'', '\\'')]\n"
        )
        meta = self.resolver.resolve_with_metadata(patch)
        self.assertIn("snowflake::tokenizer_only", meta["triggers"]["tier2_dialects"])
        self.assertEqual(len(meta["selected_tests"]), 229)
        # Verify optimizer fixtures bypassed
        for opt_test in [1295, 1382, 1434]:
            self.assertNotIn(opt_test, meta["selected_tests"])

    def test_tokenizer_deletion_crossing_boundary(self) -> None:
        # Deletion of lines 188-194 crossing Tokenizer boundary (line 190)
        patch = (
            "diff --git a/sqlglot/dialects/snowflake.py b/sqlglot/dialects/snowflake.py\n"
            "--- a/sqlglot/dialects/snowflake.py\n"
            "+++ b/sqlglot/dialects/snowflake.py\n"
            "@@ -188,7 +188,0 @@\n"
            "-        KEYWORDS = {}\n"
            "-        SINGLE_TOKENS = {}\n"
            "-        VAR_SINGLE_TOKENS = {}\n"
            "-    def some_method(self):\n"
            "-        pass\n"
            "-    x = 1\n"
            "-    y = 2\n"
        )
        meta = self.resolver.resolve_with_metadata(patch)
        self.assertNotIn("snowflake::tokenizer_only", meta["triggers"]["tier2_dialects"])
        self.assertIn("snowflake", meta["triggers"]["tier2_dialects"])
        self.assertEqual(len(meta["selected_tests"]), 258)

    def test_dialect_rename_pure(self) -> None:
        patch = (
            "diff --git a/sqlglot/dialects/snowflake.py b/sqlglot/dialects/snowflake_renamed.py\n"
            "similarity index 100%\n"
            "rename from sqlglot/dialects/snowflake.py\n"
            "rename to sqlglot/dialects/snowflake_renamed.py\n"
        )
        meta = self.resolver.resolve_with_metadata(patch)
        self.assertNotIn("snowflake::tokenizer_only", meta["triggers"]["tier2_dialects"])
        self.assertIn("snowflake", meta["triggers"]["tier2_dialects"])
        self.assertEqual(len(meta["selected_tests"]), 258)

    def test_dialect_rename_with_modification(self) -> None:
        patch = (
            "diff --git a/sqlglot/dialects/snowflake.py b/sqlglot/dialects/snowflake_renamed.py\n"
            "similarity index 98%\n"
            "rename from sqlglot/dialects/snowflake.py\n"
            "rename to sqlglot/dialects/snowflake_renamed.py\n"
            "--- a/sqlglot/dialects/snowflake.py\n"
            "+++ b/sqlglot/dialects/snowflake_renamed.py\n"
            "@@ -133,1 +133,1 @@\n"
            "-        NUMERIC_ESCAPES = {'x': (16, 2, 2, 0xFF)}\n"
            "+        NUMERIC_ESCAPES = {'x': (17, 2, 2, 0xFF)}\n"
        )
        meta = self.resolver.resolve_with_metadata(patch)
        self.assertNotIn("snowflake::tokenizer_only", meta["triggers"]["tier2_dialects"])
        self.assertIn("snowflake", meta["triggers"]["tier2_dialects"])
        self.assertEqual(len(meta["selected_tests"]), 258)

    def test_subquery_optimizer_filtering(self) -> None:
        """Synthetic test: eliminate_subqueries.py should pull subquery executor tests and bypass Test 1106."""
        patch = (
            "diff --git a/sqlglot/optimizer/eliminate_subqueries.py b/sqlglot/optimizer/eliminate_subqueries.py\n"
            "--- a/sqlglot/optimizer/eliminate_subqueries.py\n"
            "+++ b/sqlglot/optimizer/eliminate_subqueries.py\n"
            "@@ -100,2 +100,2 @@\n"
            "-    pass\n"
            "+    return None\n"
        )
        meta = self.resolver.resolve_with_metadata(patch)
        self.assertIn("tests/test_executor.py[subquery_scoped]", meta["triggers"]["tier4_dynamic_files"])
        self.assertNotIn("tests/test_executor.py", meta["triggers"]["tier4_dynamic_files"])
        self.assertNotIn(1106, meta["selected_tests"])  # Test 1106 (TPCDS) must be bypassed
        self.assertIn(1104, meta["selected_tests"])     # Subquery executor test must be selected

    def test_subquery_optimizer_ctes_filtering(self) -> None:
        """Synthetic test: eliminate_ctes.py should also safely bypass Test 1106."""
        patch = (
            "diff --git a/sqlglot/optimizer/eliminate_ctes.py b/sqlglot/optimizer/eliminate_ctes.py\n"
            "--- a/sqlglot/optimizer/eliminate_ctes.py\n"
            "+++ b/sqlglot/optimizer/eliminate_ctes.py\n"
            "@@ -50,2 +50,2 @@\n"
            "-    pass\n"
            "+    return None\n"
        )
        meta = self.resolver.resolve_with_metadata(patch)
        self.assertIn("tests/test_executor.py[subquery_scoped]", meta["triggers"]["tier4_dynamic_files"])
        self.assertNotIn(1106, meta["selected_tests"])

    def test_join_optimizer_conservative_fallback(self) -> None:
        """Synthetic test: eliminate_joins.py must select join executor tests including Test 1106."""
        patch = (
            "diff --git a/sqlglot/optimizer/eliminate_joins.py b/sqlglot/optimizer/eliminate_joins.py\n"
            "--- a/sqlglot/optimizer/eliminate_joins.py\n"
            "+++ b/sqlglot/optimizer/eliminate_joins.py\n"
            "@@ -50,2 +50,2 @@\n"
            "-    pass\n"
            "+    return None\n"
        )
        meta = self.resolver.resolve_with_metadata(patch)
        self.assertIn("tests/test_executor.py[join_scoped]", meta["triggers"]["tier4_dynamic_files"])
        self.assertIn(1106, meta["selected_tests"])

    def test_root_optimizer_conservative_fallback(self) -> None:
        """Synthetic test: root optimizer.py must select all executor tests including Test 1106."""
        patch = (
            "diff --git a/sqlglot/optimizer/optimizer.py b/sqlglot/optimizer/optimizer.py\n"
            "--- a/sqlglot/optimizer/optimizer.py\n"
            "+++ b/sqlglot/optimizer/optimizer.py\n"
            "@@ -50,2 +50,2 @@\n"
            "-    pass\n"
            "+    return None\n"
        )
        meta = self.resolver.resolve_with_metadata(patch)
        self.assertIn("tests/test_executor.py", meta["triggers"]["tier4_dynamic_files"])
        self.assertIn(1106, meta["selected_tests"])

    def test_unmapped_optimizer_conservative_fallback(self) -> None:
        """Synthetic test: qualify.py must retain full fallback including Test 1106."""
        patch = (
            "diff --git a/sqlglot/optimizer/qualify.py b/sqlglot/optimizer/qualify.py\n"
            "--- a/sqlglot/optimizer/qualify.py\n"
            "+++ b/sqlglot/optimizer/qualify.py\n"
            "@@ -50,2 +50,2 @@\n"
            "-    pass\n"
            "+    return None\n"
        )
        meta = self.resolver.resolve_with_metadata(patch)
        self.assertIn("tests/test_executor.py", meta["triggers"]["tier4_dynamic_files"])
        self.assertIn(1106, meta["selected_tests"])

    def test_documentation_zero_worker_bypass(self) -> None:
        """Synthetic test: documentation and metadata changes should safely return zero tests."""
        patch = (
            "diff --git a/README.md b/README.md\n"
            "--- a/README.md\n"
            "+++ b/README.md\n"
            "@@ -10,1 +10,1 @@\n"
            "-SQLGlot is a no-dependency SQL parser\n"
            "+SQLGlot is an ultra-fast no-dependency SQL parser\n"
        )
        meta = self.resolver.resolve_with_metadata(patch)
        self.assertEqual(meta["selected_tests"], [])
        self.assertEqual(meta["reason"], "non_code_documentation_only")

    def test_generator_only_clickhouse_2qn5(self) -> None:
        patch = Path("blast-radius-sample/sample/commits/2qn5.patch").read_text(encoding="utf-8")
        meta = self.resolver.resolve_with_metadata(patch)
        selected = set(meta["selected_tests"])
        self.assertIn("clickhouse::generator_only", meta["triggers"]["tier2_dialects"])

        # Optimizer fixture tests must NOT be selected
        for opt_test in [1295, 1377, 1382, 1434]:
            self.assertNotIn(opt_test, selected)

        # ClickHouse dialect tests must be selected
        ch_tests = set(self.resolver.by_file["tests/dialects/test_clickhouse.py"])
        self.assertTrue(ch_tests.issubset(selected))

        # Cross-cutting symbol tests (e.g. Test 85 in test_bigquery.py) must be selected
        self.assertIn(85, selected)

    def test_generator_only_clickhouse_bj7g(self) -> None:
        patch = Path("blast-radius-sample/sample/commits/bj7g.patch").read_text(encoding="utf-8")
        meta = self.resolver.resolve_with_metadata(patch)
        selected = set(meta["selected_tests"])
        self.assertIn("clickhouse::generator_only", meta["triggers"]["tier2_dialects"])

        # Must catch failing Test 94
        self.assertIn(94, selected)

        # Optimizer fixture tests must NOT be selected
        for opt_test in [1295, 1377, 1382, 1434]:
            self.assertNotIn(opt_test, selected)

    def test_generator_mixed_patch_conservative_fallback(self) -> None:
        """Mixed patch modifying generator and parser must conservatively retain optimizer fixtures."""
        patch = (
            "diff --git a/sqlglot/generators/clickhouse.py b/sqlglot/generators/clickhouse.py\n"
            "--- a/sqlglot/generators/clickhouse.py\n"
            "+++ b/sqlglot/generators/clickhouse.py\n"
            "@@ -50,1 +50,1 @@\n"
            "-    pass\n"
            "+    return None\n"
            "diff --git a/sqlglot/parsers/clickhouse.py b/sqlglot/parsers/clickhouse.py\n"
            "--- a/sqlglot/parsers/clickhouse.py\n"
            "+++ b/sqlglot/parsers/clickhouse.py\n"
            "@@ -50,1 +50,1 @@\n"
            "-    pass\n"
            "+    return None\n"
        )
        meta = self.resolver.resolve_with_metadata(patch)
        self.assertNotIn("clickhouse::generator_only", meta["triggers"]["tier2_dialects"])
        self.assertIn("clickhouse", meta["triggers"]["tier2_dialects"])
        # Full optimizer fixtures must be retained in mixed patch
        for opt_test in [1295, 1377, 1382, 1434]:
            self.assertIn(opt_test, meta["selected_tests"])

    def test_generator_dialect_root_mixed_conservative_fallback(self) -> None:
        """Mixed patch modifying generator and dialect file must trigger conservative fallback."""
        patch = (
            "diff --git a/sqlglot/generators/clickhouse.py b/sqlglot/generators/clickhouse.py\n"
            "--- a/sqlglot/generators/clickhouse.py\n"
            "+++ b/sqlglot/generators/clickhouse.py\n"
            "@@ -50,1 +50,1 @@\n"
            "-    pass\n"
            "+    return None\n"
            "diff --git a/sqlglot/dialects/clickhouse.py b/sqlglot/dialects/clickhouse.py\n"
            "--- a/sqlglot/dialects/clickhouse.py\n"
            "+++ b/sqlglot/dialects/clickhouse.py\n"
            "@@ -50,1 +50,1 @@\n"
            "-    pass\n"
            "+    return None\n"
        )
        meta = self.resolver.resolve_with_metadata(patch)
        self.assertNotIn("clickhouse::generator_only", meta["triggers"]["tier2_dialects"])
        self.assertIn("clickhouse", meta["triggers"]["tier2_dialects"])
        for opt_test in [1295, 1377, 1382, 1434]:
            self.assertIn(opt_test, meta["selected_tests"])

    def test_generator_new_file_fallback(self) -> None:
        """New generator file must trigger conservative fallback."""
        patch = (
            "diff --git a/sqlglot/generators/new_gen.py b/sqlglot/generators/new_gen.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/sqlglot/generators/new_gen.py\n"
            "@@ -0,0 +1,5 @@\n"
            "+class NewGen:\n"
            "+    pass\n"
        )
        meta = self.resolver.resolve_with_metadata(patch)
        self.assertNotIn("new_gen::generator_only", meta["triggers"]["tier2_dialects"])

    def test_ast_expression_class_ktz3(self) -> None:
        patch = Path("blast-radius-sample/sample/commits/ktz3.patch").read_text(encoding="utf-8")
        meta = self.resolver.resolve_with_metadata(patch)
        selected = set(meta["selected_tests"])

        # Must select tests/test_expressions.py and generator dispatch
        self.assertIn("tests/test_expressions.py", meta["triggers"]["tier3_symbols"])
        self.assertIn("sqlglot/generator.py::triggerproperties_sql", meta["triggers"]["tier3_symbols"])

        # Must select 82 tests, duration < 1000ms
        self.assertEqual(len(selected), 82)
        self.assertLess(meta["estimated_runtime_ms"], 1000)

        # Must NOT fall back to all 128 tests of ddl.py
        self.assertNotIn("sqlglot/expressions/ddl.py", meta["triggers"]["tier4_dynamic_files"])

    def test_ast_expression_unmapped_fallback(self) -> None:
        """Synthetic test: modification in an unmapped symbol in expressions should fall back to file coverage."""
        patch = (
            "diff --git a/sqlglot/expressions/ddl.py b/sqlglot/expressions/ddl.py\n"
            "--- a/sqlglot/expressions/ddl.py\n"
            "+++ b/sqlglot/expressions/ddl.py\n"
            "@@ -999,1 +999,1 @@\n"
            "-UNMAPPED_CONST = 1\n"
            "+UNMAPPED_CONST = 2\n"
        )
        meta = self.resolver.resolve_with_metadata(patch)
        self.assertIn("sqlglot/expressions/ddl.py", meta["triggers"]["tier4_dynamic_files"])

    def test_optimizer_executor_scoping(self) -> None:
        """Verify that SUBQUERY_OPTIMIZER_FILES and JOIN_OPTIMIZER_FILES exclude blanket executor tests
        while retaining targeted subquery and TPC executor integration tests."""
        # Commit 5y72 modifies qualify.py (SUBQUERY_OPTIMIZER_FILES)
        patch_5y72 = Path("blast-radius-sample/sample/commits/5y72.patch").read_text(encoding="utf-8")
        meta_5y72 = self.resolver.resolve_with_metadata(patch_5y72)
        selected_5y72 = set(meta_5y72["selected_tests"])

        # Must retain targeted subquery tests (e.g. test_execute_subqueries)
        if self.resolver.executor_subquery_tests:
            for t in self.resolver.executor_subquery_tests:
                self.assertIn(t, selected_5y72)

        # Must NOT select all tests from test_executor.py
        all_executor_tests = set(self.resolver.by_file.get("tests/test_executor.py", []))
        self.assertLess(len(selected_5y72 & all_executor_tests), len(all_executor_tests))
        self.assertEqual(len(selected_5y72), 50)

        # Commit 2jrq modifies eliminate_joins.py (JOIN_OPTIMIZER_FILES)
        patch_2jrq = Path("blast-radius-sample/sample/commits/2jrq.patch").read_text(encoding="utf-8")
        meta_2jrq = self.resolver.resolve_with_metadata(patch_2jrq)
        selected_2jrq = set(meta_2jrq["selected_tests"])
        expected_2jrq = set(self.answers["2jrq"]["failing"])
        self.assertTrue(expected_2jrq.issubset(selected_2jrq))
        self.assertEqual(len(selected_2jrq), 59)

    def test_all_sample_commits_100_percent_recall(self) -> None:
        total_misses = 0
        total_failing = 0

        for cid, info in self.answers.items():
            patch = Path(f"blast-radius-sample/sample/commits/{cid}.patch").read_text(encoding="utf-8")
            selected = set(self.resolver.resolve(patch))
            expected = set(info["failing"])
            missed = expected - selected

            total_failing += len(expected)
            total_misses += len(missed)
            self.assertEqual(missed, set(), f"Commit {cid} missed tests {missed}")

        self.assertEqual(total_misses, 0, f"Total missed tests: {total_misses}")


if __name__ == "__main__":
    unittest.main()
