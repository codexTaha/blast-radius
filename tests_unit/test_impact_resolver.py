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
