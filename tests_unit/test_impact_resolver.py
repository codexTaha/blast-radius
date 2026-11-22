#!/usr/bin/env python3
"""Unit and regression tests for src.impact_resolver."""

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
        self.assertLess(len(selected), 250, "535z selected unnecessarily large test set")

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
        self.assertIn(
            "sqlglot/dialects/snowflake.py::Snowflake.Tokenizer.NUMERIC_ESCAPES[s:u]",
            meta["triggers"]["tier3_symbols"],
        )
        # Key "u" in Snowflake.Tokenizer.NUMERIC_ESCAPES is never exercised by any test
        self.assertEqual(selected, set())

    def test_stage1_per_key_add_delete_rename_and_attr(self) -> None:
        # 1. Per-key modification in FUNCTIONS (6ze8 -> exact failing test #302)
        patch_6ze8 = Path("blast-radius-sample/sample/commits/6ze8.patch").read_text(encoding="utf-8")
        meta_6ze8 = self.resolver.resolve_with_metadata(patch_6ze8)
        self.assertEqual(meta_6ze8["selected_tests"], [302])
        self.assertIn(
            "sqlglot/parsers/dremio.py::DremioParser.FUNCTIONS[s:REGEXP_SPLIT]",
            meta_6ze8["triggers"]["tier3_symbols"],
        )

        # 2. Per-key addition in FUNCTIONS
        patch_add_fn = (
            "diff --git a/sqlglot/parsers/dremio.py b/sqlglot/parsers/dremio.py\n"
            "--- a/sqlglot/parsers/dremio.py\n"
            "+++ b/sqlglot/parsers/dremio.py\n"
            "@@ -106,0 +107,1 @@\n"
            '+        "TOTALLY_UNUSED_CUSTOM_FN": exp.Anonymous,\n'
        )
        meta_add = self.resolver.resolve_with_metadata(patch_add_fn)
        self.assertIn(
            "sqlglot/parsers/dremio.py::DremioParser.FUNCTIONS[s:TOTALLY_UNUSED_CUSTOM_FN]",
            meta_add["triggers"]["tier3_symbols"],
        )
        self.assertEqual(meta_add["selected_tests"], [])

        # 3. Per-key rename in FUNCTIONS ("REGEXP_MATCHES" -> "REGEXP_MATCHES_V2")
        patch_ren_fn = (
            "diff --git a/sqlglot/parsers/dremio.py b/sqlglot/parsers/dremio.py\n"
            "--- a/sqlglot/parsers/dremio.py\n"
            "+++ b/sqlglot/parsers/dremio.py\n"
            "@@ -109,1 +109,1 @@\n"
            '-        "REGEXP_MATCHES": exp.RegexpLike.from_arg_list,\n'
            '+        "REGEXP_MATCHES_V2": exp.RegexpLike.from_arg_list,\n'
        )
        meta_ren = self.resolver.resolve_with_metadata(patch_ren_fn)
        self.assertIn(
            "sqlglot/parsers/dremio.py::DremioParser.FUNCTIONS[s:REGEXP_MATCHES]",
            meta_ren["triggers"]["tier3_symbols"],
        )
        self.assertIn(
            "sqlglot/parsers/dremio.py::DremioParser.FUNCTIONS[s:REGEXP_MATCHES_V2]",
            meta_ren["triggers"]["tier3_symbols"],
        )
        self.assertGreater(len(meta_ren["selected_tests"]), 0)
        self.assertLess(len(meta_ren["selected_tests"]), 10)

        # 4. Per-attribute safe runtime tracking (535z -> TSQL.WEEK_OFFSET catches #1434, excludes #1224)
        patch_535z = Path("blast-radius-sample/sample/commits/535z.patch").read_text(encoding="utf-8")
        meta_535z = self.resolver.resolve_with_metadata(patch_535z)
        self.assertIn(1434, meta_535z["selected_tests"])
        self.assertNotIn(1224, meta_535z["selected_tests"])
        self.assertLessEqual(len(meta_535z["selected_tests"]), 15)

    def test_tokenizer_class_header_fallback(self) -> None:
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
        self.assertEqual(len(meta["selected_tests"]), 358)

    def test_tokenizer_multi_hunk_mixed_fallback(self) -> None:
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
        self.assertEqual(len(meta["selected_tests"]), 355)

    def test_tokenizer_deletion_crossing_boundary(self) -> None:
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
        self.assertEqual(len(meta["selected_tests"]), 363)

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
        self.assertEqual(len(meta["selected_tests"]), 363)

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
        self.assertEqual(len(meta["selected_tests"]), 363)

    def test_star_unpack_never_misclassified_as_comment(self) -> None:
        patch = (
            "diff --git a/sqlglot/dialects/spark.py b/sqlglot/dialects/spark.py\n"
            "--- a/sqlglot/dialects/spark.py\n"
            "+++ b/sqlglot/dialects/spark.py\n"
            "@@ -48,1 +48,1 @@\n"
            "-            **Spark2.Tokenizer.KEYWORDS,\n"
            "+            **Hive.Tokenizer.KEYWORDS,\n"
        )
        sel = self.resolver.resolve(patch)
        self.assertGreater(len(sel), 100, "**unpack Python line must never be skipped as a comment")

    def test_sql_fixture_hash_comment_never_skipped(self) -> None:
        patch = (
            "diff --git a/tests/fixtures/optimizer/simplify.sql b/tests/fixtures/optimizer/simplify.sql\n"
            "--- a/tests/fixtures/optimizer/simplify.sql\n"
            "+++ b/tests/fixtures/optimizer/simplify.sql\n"
            "@@ -10,0 +11,1 @@\n"
            "+# Malformed metadata comment without colon\n"
        )
        sel = self.resolver.resolve(patch)
        self.assertEqual(sel, [1434])

    def test_hash_inside_multiline_docstring_never_skipped(self) -> None:
        patch = (
            "diff --git a/sqlglot/transforms.py b/sqlglot/transforms.py\n"
            "--- a/sqlglot/transforms.py\n"
            "+++ b/sqlglot/transforms.py\n"
            "@@ -913,0 +914,1 @@\n"
            "+    # inside multiline string\n"
        )
        sel = self.resolver.resolve(patch)
        self.assertGreater(len(sel), 0)

    def test_expression_subclass_mro_coverage_dml(self) -> None:
        patch = (
            "diff --git a/sqlglot/expressions/dml.py b/sqlglot/expressions/dml.py\n"
            "--- a/sqlglot/expressions/dml.py\n"
            "+++ b/sqlglot/expressions/dml.py\n"
            "@@ -20,1 +20,1 @@\n"
            "-        'this': False,\n"
            "+        'this': True,\n"
        )
        sel = self.resolver.resolve(patch)
        self.assertGreater(len(sel), 50)

    def test_package_init_and_helpers_coverage(self) -> None:
        for rel in ("sqlglot/expressions/__init__.py", "tests/helpers.py", "sqlglotc/setup.py"):
            patch = (
                f"diff --git a/{rel} b/{rel}\n"
                f"--- a/{rel}\n"
                f"+++ b/{rel}\n"
                "@@ -1,1 +1,1 @@\n"
                "-x = 1\n"
                "+x = 2\n"
            )
            sel = self.resolver.resolve(patch)
            self.assertGreater(len(sel), 0, f"{rel} selected 0 tests")

    def test_all_sample_commits_100_percent_recall(self) -> None:
        total_misses = 0
        for cid, info in self.answers.items():
            patch = Path(f"blast-radius-sample/sample/commits/{cid}.patch").read_text(encoding="utf-8")
            selected = set(self.resolver.resolve(patch))
            expected = set(info["failing"])
            missed = expected - selected
            total_misses += len(missed)
            self.assertEqual(missed, set(), f"Commit {cid} missed tests {missed}")

        self.assertEqual(total_misses, 0)


if __name__ == "__main__":
    unittest.main()
