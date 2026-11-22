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

    def test_stage2_line_coverage_and_closures(self) -> None:
        # 1. Early-return line exclusion: 8y7j (_unix_to_time_sql line 55 is never reached -> 0 tests)
        patch_8y7j = Path("blast-radius-sample/sample/commits/8y7j.patch").read_text(encoding="utf-8")
        meta_8y7j = self.resolver.resolve_with_metadata(patch_8y7j)
        self.assertEqual(meta_8y7j["selected_tests"], [])

        # 2. Exact statement-unit line scoping on ary6 (10/10 exact oracle) and j6uz (2/2 exact oracle)
        for cid, expected_cnt in (("ary6", 10), ("j6uz", 2)):
            p = Path(f"blast-radius-sample/sample/commits/{cid}.patch").read_text(encoding="utf-8")
            sel = self.resolver.resolve(p)
            self.assertEqual(sel, sorted(self.answers[cid]["failing"]))
            self.assertEqual(len(sel), expected_cnt)

        # 3. Closure co_qualname + line scoping on fryt (build_date_delta_with_cast_interval.<locals>._builder)
        patch_fryt = Path("blast-radius-sample/sample/commits/fryt.patch").read_text(encoding="utf-8")
        meta_fryt = self.resolver.resolve_with_metadata(patch_fryt)
        self.assertIn(
            "sqlglot/parsers/dremio.py::build_date_delta_with_cast_interval.<locals>._builder",
            meta_fryt["triggers"]["tier3_symbols"],
        )
        self.assertEqual(len(meta_fryt["selected_tests"]), 3)

        # 4. Multi-line boolean short-circuit safety on rszg (catches all 3 failing tests including #1355)
        patch_rszg = Path("blast-radius-sample/sample/commits/rszg.patch").read_text(encoding="utf-8")
        sel_rszg = set(self.resolver.resolve(patch_rszg))
        self.assertTrue(set(self.answers["rszg"]["failing"]).issubset(sel_rszg))
        self.assertLessEqual(len(sel_rszg), 6)

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
        self.assertEqual(
            set(meta["selected_tests"]),
            set(self.resolver.class_to_tests["sqlglot/dialects/snowflake.py::Snowflake.Tokenizer"]),
        )
        self.assertEqual(len(meta["selected_tests"]), 273)

    def test_stage3_fallback_and_mro_cleanup(self) -> None:
        # 1. TriggerProperties class fallback selects only exact 3 TriggerProperties tests, no serde/expressions pollution
        patch_trig_nullable = (
            "diff --git a/sqlglot/expressions/ddl.py b/sqlglot/expressions/ddl.py\n"
            "--- a/sqlglot/expressions/ddl.py\n"
            "+++ b/sqlglot/expressions/ddl.py\n"
            "@@ -83,1 +83,1 @@\n"
            '-        "referenced_table": False,\n'
            '+        "referenced_table": True,\n'
        )
        self.assertEqual(self.resolver.resolve(patch_trig_nullable), [336, 573, 587])

        # 2. Generator class-body edit excludes test_generator_memory (#1224) unless SELECT_KINDS touched
        patch_gen_attr = (
            "diff --git a/sqlglot/generators/clickhouse.py b/sqlglot/generators/clickhouse.py\n"
            "--- a/sqlglot/generators/clickhouse.py\n"
            "+++ b/sqlglot/generators/clickhouse.py\n"
            "@@ -140,1 +140,1 @@\n"
            "-    TZ_TO_WITH_TIME_ZONE = True\n"
            "+    TZ_TO_WITH_TIME_ZONE = False\n"
        )
        sel_gen = self.resolver.resolve(patch_gen_attr)
        self.assertNotIn(1224, sel_gen)

        # 3. SELECT_KINDS edit DOES retain test_generator_memory (#1224)
        patch_select_kinds = (
            "diff --git a/sqlglot/parsers/bigquery.py b/sqlglot/parsers/bigquery.py\n"
            "--- a/sqlglot/parsers/bigquery.py\n"
            "+++ b/sqlglot/parsers/bigquery.py\n"
            "@@ -285,1 +285,1 @@\n"
            '-    SELECT_KINDS = ("SELECT", "ALL", "DISTINCT", "AS STRUCT", "AS VALUE")\n'
            '+    SELECT_KINDS = ("SELECT", "ALL", "DISTINCT")\n'
        )
        sel_sk = self.resolver.resolve(patch_select_kinds)
        self.assertGreater(len(sel_sk), 0)

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

    def test_exp_step2_semantic_equivalence_and_counterexamples(self) -> None:
        # 1. Proven equivalences: 2haz, 66ks, 5y72
        for cid in ("2haz", "66ks", "5y72"):
            patch = Path(f"blast-radius-sample/sample/commits/{cid}.patch").read_text(encoding="utf-8")
            self.assertEqual(self.resolver.resolve(patch), [], f"{cid} should resolve to 0 tests")

        # 2. Counterexample A: reverse_key in sqlglot/executor/env.py overrides __eq__ unsafely for None
        patch_env = (
            "diff --git a/sqlglot/executor/env.py b/sqlglot/executor/env.py\n"
            "--- a/sqlglot/executor/env.py\n"
            "+++ b/sqlglot/executor/env.py\n"
            "@@ -74,1 +74,1 @@\n"
            "-        filtered = tuple(v for v in values if v is not None)\n"
            "+        filtered = tuple(v for v in values if v != None)\n"
        )
        self.assertGreater(len(self.resolver.resolve(patch_env)), 0, "env.py must not equivalence-skip None comparison")

        # 3. Counterexample B: copy=False -> copy=True on an existing variable (not a fresh inline AST constructor)
        patch_non_fresh_copy = (
            "diff --git a/sqlglot/optimizer/eliminate_subqueries.py b/sqlglot/optimizer/eliminate_subqueries.py\n"
            "--- a/sqlglot/optimizer/eliminate_subqueries.py\n"
            "+++ b/sqlglot/optimizer/eliminate_subqueries.py\n"
            "@@ -176,1 +176,1 @@\n"
            "-                new_table = exp.alias_(table, alias=table.alias_or_name, copy=False)\n"
            "+                new_table = exp.alias_(table, alias=table.alias_or_name, copy=True)\n"
        )
        self.assertGreater(len(self.resolver.resolve(patch_non_fresh_copy)), 0, "non-fresh variable copy must not be skipped")

        # 4. Counterexample C: unverified variable compared to (str, AutoName) enum (could be raw str at runtime)
        patch_unverified_enum_lhs = (
            "diff --git a/sqlglot/dialects/dialect.py b/sqlglot/dialects/dialect.py\n"
            "--- a/sqlglot/dialects/dialect.py\n"
            "+++ b/sqlglot/dialects/dialect.py\n"
            "@@ -1280,1 +1280,1 @@\n"
            "-            if text is NormalizationStrategy.UPPERCASE\n"
            "+            if text == NormalizationStrategy.UPPERCASE\n"
        )
        self.assertGreater(len(self.resolver.resolve(patch_unverified_enum_lhs)), 0, "unverified str-enum LHS must not be skipped")

    def test_fix1_tokentype_intenum_not_treated_as_pure_enum(self) -> None:
        # TokenType is an IntEnum (subclass of int), so an int value equals TokenType via `==` but not `is`.
        from enum import IntEnum

        class _DemoTokenType(IntEnum):
            L_PAREN = 1

        raw_val: object = 1
        self.assertTrue(raw_val == _DemoTokenType.L_PAREN)
        self.assertFalse(raw_val is _DemoTokenType.L_PAREN)

        # Consequently, `is TokenType.X` -> `== TokenType.X` must NOT be treated as an unconditional pure-Enum equivalence:
        patch_tokentype = (
            "diff --git a/sqlglot/parser.py b/sqlglot/parser.py\n"
            "--- a/sqlglot/parser.py\n"
            "+++ b/sqlglot/parser.py\n"
            "@@ -10276,1 +10276,1 @@\n"
            "-            if result == TokenType.VAR:\n"
            "+            if result is TokenType.VAR:\n"
        )
        self.assertGreater(len(self.resolver.resolve(patch_tokentype)), 0, "IntEnum TokenType must not be skipped as pure Enum")

        # Pure Enum (TrieResult) and verified (str, AutoName) NormalizationStrategy (2haz) remain active:
        patch_trieresult = (
            "diff --git a/sqlglot/parser.py b/sqlglot/parser.py\n"
            "--- a/sqlglot/parser.py\n"
            "+++ b/sqlglot/parser.py\n"
            "@@ -10276,1 +10276,1 @@\n"
            "-            if result == TrieResult.FAILED:\n"
            "+            if result is TrieResult.FAILED:\n"
        )
        self.assertEqual(self.resolver.resolve(patch_trieresult), [], "Pure Enum TrieResult equivalence must remain active")
        patch_2haz = Path("blast-radius-sample/sample/commits/2haz.patch").read_text(encoding="utf-8")
        self.assertEqual(self.resolver.resolve(patch_2haz), [], "2haz NormalizationStrategy optimization must remain active")

    def test_fix2_narrow_semantic_equivalence_guards(self) -> None:
        # 1. Single-file, single-hunk patch: semantic equivalence applies (0 tests)
        patch_66ks = Path("blast-radius-sample/sample/commits/66ks.patch").read_text(encoding="utf-8")
        patch_2haz = Path("blast-radius-sample/sample/commits/2haz.patch").read_text(encoding="utf-8")
        self.assertEqual(self.resolver.resolve(patch_66ks), [])

        # 2. Single-file, multiple-hunk patch (even if both hunks individually look like `is None` <-> `== None`):
        # must fall back to normal conservative test selection!
        patch_single_file_multi_hunk = (
            "diff --git a/sqlglot/executor/context.py b/sqlglot/executor/context.py\n"
            "--- a/sqlglot/executor/context.py\n"
            "+++ b/sqlglot/executor/context.py\n"
            "@@ -53,1 +53,1 @@ class Context:\n"
            "-        if self._table is None:\n"
            "+        if self._table == None:\n"
            "@@ -92,1 +92,1 @@ class Context:\n"
            "-            return tuple((t is None, t) for t in self.eval_tuple(key))\n"
            "+            return tuple((t == None, t) for t in self.eval_tuple(key))\n"
        )
        sel_multi_hunk = self.resolver.resolve(patch_single_file_multi_hunk)
        self.assertGreaterEqual(len(sel_multi_hunk), 51, "Single-file multi-hunk patch must not skip via semantic equivalence")
        self.assertIn(1106, sel_multi_hunk)

        # 3. Multiple-file patch combining two individually equivalent patches (66ks + 2haz):
        # must fall back to normal conservative test selection for both files!
        patch_multi_file = patch_66ks + "\n" + patch_2haz
        sel_multi_file = self.resolver.resolve(patch_multi_file)
        self.assertGreaterEqual(len(sel_multi_file), 86, "Multi-file patch must fall back to conservative test selection")
        self.assertIn(1106, sel_multi_file)

        # 4. A patch that changes `__eq__` alongside an `is None` comparison (both multi-hunk and same-hunk):
        patch_eq_and_is_none_multihunk = (
            "diff --git a/sqlglot/dialects/dialect.py b/sqlglot/dialects/dialect.py\n"
            "--- a/sqlglot/dialects/dialect.py\n"
            "+++ b/sqlglot/dialects/dialect.py\n"
            "@@ -1170,1 +1170,1 @@ class Dialect(metaclass=_Dialect):\n"
            "-        if normalization_strategy is None:\n"
            "+        if normalization_strategy == None:\n"
            "@@ -1182,1 +1182,1 @@ class Dialect(metaclass=_Dialect):\n"
            "-        return type(self) == other\n"
            "+        return other.normalization_strategy == self.normalization_strategy\n"
        )
        sel_eq_mh = self.resolver.resolve(patch_eq_and_is_none_multihunk)
        self.assertGreater(len(sel_eq_mh), 50, "Patch changing __eq__ alongside is None must select conservative tests")

        patch_same_hunk_multiline = (
            "diff --git a/sqlglot/executor/context.py b/sqlglot/executor/context.py\n"
            "--- a/sqlglot/executor/context.py\n"
            "+++ b/sqlglot/executor/context.py\n"
            "@@ -53,2 +53,2 @@ class Context:\n"
            "-        if self._table is None:\n"
            "-            self._table = list(self.tables.values())[0]\n"
            "+        if self._table == None:\n"
            "+            self._table = None\n"
        )
        self.assertGreaterEqual(len(self.resolver.resolve(patch_same_hunk_multiline)), 51)

    def test_exp_step4a_trie_prefix_shadowing_and_counterexamples(self) -> None:
        # 1. gbav modifies "GROUPS FOR USER" in SingleStoreParser.SHOW_PARSERS, which is shadowed by "GROUPS" in SHOW_TRIE
        patch_gbav = Path("blast-radius-sample/sample/commits/gbav.patch").read_text(encoding="utf-8")
        self.assertEqual(self.resolver.resolve(patch_gbav), [], "gbav shadowed SHOW_PARSERS key should resolve to 0 tests")

        # 2. Counterexample A: modifying the unshadowed prefix key "GROUPS" itself must select test_show (#670)
        patch_unshadowed_groups = (
            "diff --git a/sqlglot/parsers/singlestore.py b/sqlglot/parsers/singlestore.py\n"
            "--- a/sqlglot/parsers/singlestore.py\n"
            "+++ b/sqlglot/parsers/singlestore.py\n"
            "@@ -211,1 +211,1 @@\n"
            '-        "GROUPS": _show_parser("GROUPS"),\n'
            '+        "GROUPS": _show_parser("GROUPS", target=True),\n'
        )
        self.assertIn(670, self.resolver.resolve(patch_unshadowed_groups), "unshadowed prefix key GROUPS must select #670")

        # 3. Counterexample B: multi-word key without a registered prefix key ("DATABASE STATUS") must select test_show (#670)
        patch_unshadowed_multiword = (
            "diff --git a/sqlglot/parsers/singlestore.py b/sqlglot/parsers/singlestore.py\n"
            "--- a/sqlglot/parsers/singlestore.py\n"
            "+++ b/sqlglot/parsers/singlestore.py\n"
            "@@ -203,1 +203,1 @@\n"
            '-        "DATABASE STATUS": _show_parser("DATABASE STATUS"),\n'
            '+        "DATABASE STATUS": _show_parser("DATABASE STATUS", target=True),\n'
        )
        self.assertIn(670, self.resolver.resolve(patch_unshadowed_multiword), "unshadowed multi-word key must select #670")

    def test_exp_step4b_arg_types_non_null_invariance_and_counterexamples(self) -> None:
        # 1. ktz3 modifies "constraint": False -> True in TriggerProperties.arg_types (provably non-null bool at all call sites)
        patch_ktz3 = Path("blast-radius-sample/sample/commits/ktz3.patch").read_text(encoding="utf-8")
        self.assertEqual(self.resolver.resolve(patch_ktz3), [], "ktz3 invariant arg_types key should resolve to 0 tests")

        # 2. Counterexample A: modifying a nullable key ("referenced_table") on TriggerProperties.arg_types must select tests
        patch_nullable_key = (
            "diff --git a/sqlglot/expressions/ddl.py b/sqlglot/expressions/ddl.py\n"
            "--- a/sqlglot/expressions/ddl.py\n"
            "+++ b/sqlglot/expressions/ddl.py\n"
            "@@ -83,1 +83,1 @@\n"
            '-        "referenced_table": False,\n'
            '+        "referenced_table": True,\n'
        )
        self.assertEqual(self.resolver.resolve(patch_nullable_key), [336, 573, 587], "nullable arg_types key must select class tests")

        # 3. Counterexample B: renaming or deleting an arg_types key must select tests
        patch_key_rename = (
            "diff --git a/sqlglot/expressions/ddl.py b/sqlglot/expressions/ddl.py\n"
            "--- a/sqlglot/expressions/ddl.py\n"
            "+++ b/sqlglot/expressions/ddl.py\n"
            "@@ -82,1 +82,1 @@\n"
            '-        "constraint": False,\n'
            '+        "constraint_renamed": False,\n'
        )
        self.assertEqual(self.resolver.resolve(patch_key_rename), [336, 573, 587], "arg_types key rename must select class tests")

    def test_fix3_arg_types_single_hunk_and_single_file_guards(self) -> None:
        patch_ktz3 = Path("blast-radius-sample/sample/commits/ktz3.patch").read_text(encoding="utf-8")
        self.assertEqual(self.resolver.resolve(patch_ktz3), [])

        # 1. Single-file, multi-hunk patch touching two invariant arg_types keys in ddl.py (TriggerProperties + Clone):
        # must fall back to conservative class test selection!
        patch_ddl_multi_hunk = (
            "diff --git a/sqlglot/expressions/ddl.py b/sqlglot/expressions/ddl.py\n"
            "--- a/sqlglot/expressions/ddl.py\n"
            "+++ b/sqlglot/expressions/ddl.py\n"
            "@@ -82,1 +82,1 @@ class TriggerProperties(Expression):\n"
            '-        "constraint": False,\n'
            '+        "constraint": True,\n'
            "@@ -305,1 +305,1 @@ class Clone(Expression):\n"
            '-    arg_types = {"this": True, "shallow": False, "copy": False}\n'
            '+    arg_types = {"this": True, "shallow": False, "copy": True}\n'
        )
        sel_mh = self.resolver.resolve(patch_ddl_multi_hunk)
        for tnum in (336, 573, 587):
            self.assertIn(tnum, sel_mh, "Multi-hunk arg_types patch must retain conservative TriggerProperties tests")

        # 2. Multi-file patch touching TriggerProperties.arg_types + another file:
        # must retain conservative TriggerProperties class tests!
        patch_66ks = Path("blast-radius-sample/sample/commits/66ks.patch").read_text(encoding="utf-8")
        sel_mf = self.resolver.resolve(patch_ktz3 + "\n" + patch_66ks)
        for tnum in (336, 573, 587, 1106):
            self.assertIn(tnum, sel_mf, "Multi-file arg_types patch must retain conservative tests for both files")

    def test_audit_a_is_none_proven_safe_types_and_rejections(self) -> None:
        # Positive 1: `self._table is None` -> `self._table == None` on `Context` (explicitly typed `t.Optional[Table]`)
        patch_66ks = Path("blast-radius-sample/sample/commits/66ks.patch").read_text(encoding="utf-8")
        self.assertEqual(self.resolver.resolve(patch_66ks), [])

        # Positive 2: `self._table is not None` -> `self._table != None` on `Context`
        patch_context_is_not_none = (
            "diff --git a/sqlglot/executor/context.py b/sqlglot/executor/context.py\n"
            "--- a/sqlglot/executor/context.py\n"
            "+++ b/sqlglot/executor/context.py\n"
            "@@ -53,1 +53,1 @@ class Context:\n"
            "-        if self._table is not None:\n"
            "+        if self._table != None:\n"
        )
        self.assertEqual(self.resolver.resolve(patch_context_is_not_none), [])

        # Negative 1: Untyped local variable `t is None` -> `t == None` in `Context.sort` (same file & class!)
        # `t` is an element of `self.eval_tuple(key)` which can hold runtime row values with custom `__eq__`.
        patch_untyped_local_in_context = (
            "diff --git a/sqlglot/executor/context.py b/sqlglot/executor/context.py\n"
            "--- a/sqlglot/executor/context.py\n"
            "+++ b/sqlglot/executor/context.py\n"
            "@@ -92,1 +92,1 @@ class Context:\n"
            "-            return tuple((t is None, t) for t in self.eval_tuple(key))\n"
            "+            return tuple((t == None, t) for t in self.eval_tuple(key))\n"
        )
        sel_untyped_local = self.resolver.resolve(patch_untyped_local_in_context)
        self.assertIn(1106, sel_untyped_local, "Untyped local variable `t is None` in Context.sort must not be skipped")
        self.assertEqual(len(sel_untyped_local), 24)

        # Negative 2: Container-typed attribute `self.env` (`dict`) on `Context`
        patch_container_attr_in_context = (
            "diff --git a/sqlglot/executor/context.py b/sqlglot/executor/context.py\n"
            "--- a/sqlglot/executor/context.py\n"
            "+++ b/sqlglot/executor/context.py\n"
            "@@ -53,1 +53,1 @@ class Context:\n"
            "-        if self.env is None:\n"
            "+        if self.env == None:\n"
        )
        sel_container_attr = self.resolver.resolve(patch_container_attr_in_context)
        self.assertIn(1106, sel_container_attr, "Container-typed `self.env` on Context must not be skipped")
        self.assertEqual(len(sel_container_attr), 51)

        # Negative 3: `t.Any`-annotated attribute `self.table` on `RangeReader` (`sqlglot/executor/table.py`)
        # Even though `sqlglot/expressions/` defines `class Any(Expression)`, `t.Any` must be rejected!
        self.assertNotIn("sqlglot/executor/table.py::RangeReader", self.resolver.proven_safe_none_attrs)
        patch_t_any_attr = (
            "diff --git a/sqlglot/executor/table.py b/sqlglot/executor/table.py\n"
            "--- a/sqlglot/executor/table.py\n"
            "+++ b/sqlglot/executor/table.py\n"
            "@@ -174,1 +174,1 @@ class RangeReader:\n"
            "-        return len(self.range) if self.table is None else 0\n"
            "+        return len(self.range) if self.table == None else 0\n"
        )
        sel_t_any = self.resolver.resolve(patch_t_any_attr)
        self.assertIn(1106, sel_t_any, "`t.Any`-annotated `self.table` on RangeReader must not be skipped")

        # Negative 4: Untyped parameter / local variable `val is None` in a function or method
        patch_untyped_var_parser = (
            "diff --git a/sqlglot/parser.py b/sqlglot/parser.py\n"
            "--- a/sqlglot/parser.py\n"
            "+++ b/sqlglot/parser.py\n"
            "@@ -10276,1 +10276,1 @@\n"
            "-            if result is None:\n"
            "+            if result == None:\n"
        )
        self.assertGreater(
            len(self.resolver.resolve(patch_untyped_var_parser)),
            0,
            "Untyped local variable `result is None` in parser.py must not be skipped",
        )

    def test_audit_b_multiline_ast_context_and_adversarial_rejections(self) -> None:
        # 1. Accepted parenthesized multiline conditional expression with full enclosing context (`2haz.patch`)
        patch_2haz = Path("blast-radius-sample/sample/commits/2haz.patch").read_text(encoding="utf-8")
        self.assertEqual(self.resolver.resolve(patch_2haz), [])

        # 2. Rejected: Truncated multiline conditional expression missing enclosing `unsafe = (` and `)` in hunk context
        patch_truncated_ternary = (
            "diff --git a/sqlglot/dialects/dialect.py b/sqlglot/dialects/dialect.py\n"
            "--- a/sqlglot/dialects/dialect.py\n"
            "+++ b/sqlglot/dialects/dialect.py\n"
            "@@ -1280,1 +1280,1 @@ class Dialect(metaclass=_Dialect):\n"
            "-            if self.normalization_strategy is NormalizationStrategy.UPPERCASE\n"
            "+            if self.normalization_strategy == NormalizationStrategy.UPPERCASE\n"
        )
        sel_trunc_ternary = self.resolver.resolve(patch_truncated_ternary)
        self.assertEqual(len(sel_trunc_ternary), 86, "Truncated multiline ternary without context must be rejected")

        # 3. Rejected: Chained comparison continuation line (`== False`) on the next line inside parenthesized ternary
        patch_chained_continuation = (
            "diff --git a/sqlglot/dialects/dialect.py b/sqlglot/dialects/dialect.py\n"
            "--- a/sqlglot/dialects/dialect.py\n"
            "+++ b/sqlglot/dialects/dialect.py\n"
            "@@ -1277,7 +1277,7 @@ class Dialect(metaclass=_Dialect):\n"
            " \n"
            "         unsafe = (\n"
            "             str.islower\n"
            "-            if self.normalization_strategy is NormalizationStrategy.UPPERCASE\n"
            "+            if self.normalization_strategy == NormalizationStrategy.UPPERCASE\n"
            "             == False\n"
            "             else str.isupper\n"
            "         )\n"
        )
        sel_chained = self.resolver.resolve(patch_chained_continuation)
        self.assertEqual(len(sel_chained), 86, "Chained comparison continuation line must be rejected")

        # 4. Rejected: Backslash `\` line continuation on modified or context line
        patch_backslash_cont = (
            "diff --git a/sqlglot/executor/context.py b/sqlglot/executor/context.py\n"
            "--- a/sqlglot/executor/context.py\n"
            "+++ b/sqlglot/executor/context.py\n"
            "@@ -52,3 +52,3 @@ class Context:\n"
            "     def table(self) -> Table:\n"
            "-        if self._table is None \\\n"
            "+        if self._table == None \\\n"
            "             and True:\n"
        )
        sel_backslash = self.resolver.resolve(patch_backslash_cont)
        self.assertIn(1106, sel_backslash, "Backslash continuation line must be rejected")
        self.assertEqual(len(sel_backslash), 51)

        # 5. Rejected: Single-line and multiline nested calls wrapping `exp.alias_(..., copy=False)`
        patch_nested_single_line = (
            "diff --git a/sqlglot/optimizer/eliminate_subqueries.py b/sqlglot/optimizer/eliminate_subqueries.py\n"
            "--- a/sqlglot/optimizer/eliminate_subqueries.py\n"
            "+++ b/sqlglot/optimizer/eliminate_subqueries.py\n"
            "@@ -176,1 +176,1 @@ def _eliminate_cte(\n"
            "-                new_table = outer_fn(exp.alias_(exp.table_(name), alias=table.alias_or_name, copy=False))\n"
            "+                new_table = outer_fn(exp.alias_(exp.table_(name), alias=table.alias_or_name, copy=True))\n"
        )
        self.assertEqual(
            len(self.resolver.resolve(patch_nested_single_line)),
            3,
            "Single-line nested call wrapping exp.alias_ must be rejected",
        )

        patch_nested_multiline_arg = (
            "diff --git a/sqlglot/optimizer/eliminate_subqueries.py b/sqlglot/optimizer/eliminate_subqueries.py\n"
            "--- a/sqlglot/optimizer/eliminate_subqueries.py\n"
            "+++ b/sqlglot/optimizer/eliminate_subqueries.py\n"
            "@@ -175,3 +175,3 @@ def _eliminate_cte(\n"
            "                 new_table = outer_fn(\n"
            "-                    exp.alias_(exp.table_(name), alias=table.alias_or_name, copy=False)\n"
            "+                    exp.alias_(exp.table_(name), alias=table.alias_or_name, copy=True)\n"
            "                 )\n"
        )
        self.assertEqual(
            len(self.resolver.resolve(patch_nested_multiline_arg)),
            3,
            "Multiline argument line inside outer_fn(...) must be rejected",
        )

        # 6. Rejected: Indented keyword argument `flag = self._table is None` inside an open call parenthesis
        patch_kwarg_inside_call = (
            "diff --git a/sqlglot/executor/context.py b/sqlglot/executor/context.py\n"
            "--- a/sqlglot/executor/context.py\n"
            "+++ b/sqlglot/executor/context.py\n"
            "@@ -52,3 +52,3 @@ class Context:\n"
            "         res = some_call(\n"
            "-            flag = self._table is None\n"
            "+            flag = self._table == None\n"
            "         )\n"
        )
        sel_kwarg_call = self.resolver.resolve(patch_kwarg_inside_call)
        self.assertIn(1106, sel_kwarg_call, "Keyword argument inside multiline call must never be misparsed as Assign")
        self.assertEqual(len(sel_kwarg_call), 51)

        # 7. Accepted vs Rejected: AST constructors spanning multiple lines
        patch_multiline_fresh_ctor_accepted = (
            "diff --git a/sqlglot/optimizer/eliminate_subqueries.py b/sqlglot/optimizer/eliminate_subqueries.py\n"
            "--- a/sqlglot/optimizer/eliminate_subqueries.py\n"
            "+++ b/sqlglot/optimizer/eliminate_subqueries.py\n"
            "@@ -173,5 +173,5 @@ def _eliminate_cte(\n"
            "                 new_table = exp.alias_(\n"
            "                     exp.table_(name),\n"
            "                     alias=table.alias_or_name,\n"
            "-                    copy=False,\n"
            "+                    copy=True,\n"
            "                 )\n"
        )
        self.assertEqual(
            self.resolver.resolve(patch_multiline_fresh_ctor_accepted),
            [],
            "Complete multiline fresh AST constructor assignment in hunk context should be accepted",
        )

        patch_multiline_non_fresh_ctor_rejected = (
            "diff --git a/sqlglot/optimizer/eliminate_subqueries.py b/sqlglot/optimizer/eliminate_subqueries.py\n"
            "--- a/sqlglot/optimizer/eliminate_subqueries.py\n"
            "+++ b/sqlglot/optimizer/eliminate_subqueries.py\n"
            "@@ -173,5 +173,5 @@ def _eliminate_cte(\n"
            "                 new_table = exp.alias_(\n"
            "                     table,\n"
            "                     alias=table.alias_or_name,\n"
            "-                    copy=False,\n"
            "+                    copy=True,\n"
            "                 )\n"
        )
        self.assertEqual(
            len(self.resolver.resolve(patch_multiline_non_fresh_ctor_rejected)),
            3,
            "Multiline exp.alias_ with non-fresh arg0 `table` on context line must be rejected",
        )

        patch_truncated_kwarg_line_rejected = (
            "diff --git a/sqlglot/optimizer/eliminate_subqueries.py b/sqlglot/optimizer/eliminate_subqueries.py\n"
            "--- a/sqlglot/optimizer/eliminate_subqueries.py\n"
            "+++ b/sqlglot/optimizer/eliminate_subqueries.py\n"
            "@@ -176,1 +176,1 @@ def _eliminate_cte(\n"
            "-                    copy=False\n"
            "+                    copy=True\n"
        )
        self.assertEqual(
            len(self.resolver.resolve(patch_truncated_kwarg_line_rejected)),
            3,
            "Isolated keyword-argument line `copy=False` without enclosing call context must be rejected",
        )

    def test_audit_c_conservative_fallback_guarantees(self) -> None:
        # Verify that whenever any semantic-equivalence proof fails on `66ks`, `2haz`, or `5y72`,
        # the resolver falls back to the exact conservative baseline statement-unit test set:
        # - `66ks` (`sqlglot/executor/context.py::Context.table` line 53): 51 tests (including #1106)
        # - `2haz` (`sqlglot/dialects/dialect.py::Dialect.normalize_identifier` line 1280): 86 tests (including #1106)
        # - `5y72` (`sqlglot/optimizer/eliminate_subqueries.py::_eliminate_cte` line 176): 3 tests ([336, 1344, 1435])
        baseline_66ks_patch = (
            "diff --git a/sqlglot/executor/context.py b/sqlglot/executor/context.py\n"
            "--- a/sqlglot/executor/context.py\n"
            "+++ b/sqlglot/executor/context.py\n"
            "@@ -53,1 +53,1 @@ class Context:\n"
            "-        if self._table is None:\n"
            "+        if self._table is not None:\n"
        )
        baseline_2haz_patch = (
            "diff --git a/sqlglot/dialects/dialect.py b/sqlglot/dialects/dialect.py\n"
            "--- a/sqlglot/dialects/dialect.py\n"
            "+++ b/sqlglot/dialects/dialect.py\n"
            "@@ -1277,7 +1277,7 @@ class Dialect(metaclass=_Dialect):\n"
            " \n"
            "         unsafe = (\n"
            "             str.islower\n"
            "-            if self.normalization_strategy is NormalizationStrategy.UPPERCASE\n"
            "+            if self.normalization_strategy is NormalizationStrategy.LOWERCASE\n"
            "             else str.isupper\n"
            "         )\n"
        )
        baseline_5y72_patch = (
            "diff --git a/sqlglot/optimizer/eliminate_subqueries.py b/sqlglot/optimizer/eliminate_subqueries.py\n"
            "--- a/sqlglot/optimizer/eliminate_subqueries.py\n"
            "+++ b/sqlglot/optimizer/eliminate_subqueries.py\n"
            "@@ -176,1 +176,1 @@ def _eliminate_cte(\n"
            "-                new_table = exp.alias_(exp.table_(name), alias=table.alias_or_name, copy=False)\n"
            "+                new_table = exp.alias_(exp.table_(name), alias=name, copy=False)\n"
        )
        expected_66ks_tests = self.resolver.resolve(baseline_66ks_patch)
        expected_2haz_tests = self.resolver.resolve(baseline_2haz_patch)
        expected_5y72_tests = self.resolver.resolve(baseline_5y72_patch)

        self.assertEqual(len(expected_66ks_tests), 51)
        self.assertIn(1106, expected_66ks_tests)
        self.assertEqual(len(expected_2haz_tests), 86)
        self.assertIn(1106, expected_2haz_tests)
        self.assertEqual(len(expected_5y72_tests), 3)

        # Any rejected candidate at each site must select the exact same 51, 86, or 3 tests (never fewer!)
        rejected_66ks = (
            "diff --git a/sqlglot/executor/context.py b/sqlglot/executor/context.py\n"
            "--- a/sqlglot/executor/context.py\n"
            "+++ b/sqlglot/executor/context.py\n"
            "@@ -53,1 +53,1 @@ class Context:\n"
            "-        if self.tables is None:\n"
            "+        if self.tables == None:\n"
        )
        rejected_2haz = (
            "diff --git a/sqlglot/dialects/dialect.py b/sqlglot/dialects/dialect.py\n"
            "--- a/sqlglot/dialects/dialect.py\n"
            "+++ b/sqlglot/dialects/dialect.py\n"
            "@@ -1280,1 +1280,1 @@ class Dialect(metaclass=_Dialect):\n"
            "-            if self.normalization_strategy is NormalizationStrategy.UPPERCASE\n"
            "+            if self.normalization_strategy == NormalizationStrategy.UPPERCASE\n"
        )
        rejected_5y72 = (
            "diff --git a/sqlglot/optimizer/eliminate_subqueries.py b/sqlglot/optimizer/eliminate_subqueries.py\n"
            "--- a/sqlglot/optimizer/eliminate_subqueries.py\n"
            "+++ b/sqlglot/optimizer/eliminate_subqueries.py\n"
            "@@ -176,1 +176,1 @@ def _eliminate_cte(\n"
            "-                new_table = exp.alias_(table, alias=table.alias_or_name, copy=False)\n"
            "+                new_table = exp.alias_(table, alias=table.alias_or_name, copy=True)\n"
        )
        self.assertEqual(self.resolver.resolve(rejected_66ks), expected_66ks_tests)
        self.assertEqual(self.resolver.resolve(rejected_2haz), expected_2haz_tests)
        self.assertEqual(self.resolver.resolve(rejected_5y72), expected_5y72_tests)

    def test_phase9_opt1_multifile_closure_and_span_guards(self) -> None:
        # 1. Per-file accumulator isolation in `_add_file_closure`:
        # When File 2 (`sqlglot/parsers/sqlite.py`) falls back to `_add_file_closure`, and its 76 tests are already
        # a subset of File 1 (`sqlglot/dialects/sqlite.py`, 116 tests), `core_safety_net` must NOT falsely trigger!
        patch_multifile_subset = (
            "diff --git a/sqlglot/dialects/sqlite.py b/sqlglot/dialects/sqlite.py\n"
            "--- a/sqlglot/dialects/sqlite.py\n"
            "+++ b/sqlglot/dialects/sqlite.py\n"
            "@@ -27,1 +27,1 @@ class SQLite(Dialect):\n"
            "-    TYPED_DIVISION = True\n"
            "+    TYPED_DIVISION = False\n"
            "diff --git a/sqlglot/parsers/sqlite.py b/sqlglot/parsers/sqlite.py\n"
            "--- a/sqlglot/parsers/sqlite.py\n"
            "+++ b/sqlglot/parsers/sqlite.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-from __future__ import annotations\n"
            "+from __future__ import annotations\n"
            "+_EXTRA_TOP_LEVEL = 1\n"
        )
        meta_mf = self.resolver.resolve_with_metadata(patch_multifile_subset)
        self.assertNotIn("core_safety_net", meta_mf["triggers"]["tier5_fallback"])
        self.assertEqual(len(meta_mf["selected_tests"]), 116)

        # 2. Function signature edit (`s_start <= h_s < s_body`):
        # Valid undecorated signature edit on `explode_to_unnest_sql` resolves to `func_tests` (7 tests),
        # whereas adding a decorator `@custom_dec` or renaming the function falls back to full file closure (1,589 tests).
        patch_sig_valid = (
            "diff --git a/sqlglot/dialects/dialect.py b/sqlglot/dialects/dialect.py\n"
            "--- a/sqlglot/dialects/dialect.py\n"
            "+++ b/sqlglot/dialects/dialect.py\n"
            "@@ -2912,4 +2912,4 @@ def groupconcat_sql(\n"
            "-def explode_to_unnest_sql(\n"
            "-    self: Generator, expression: exp.Lateral,ingle_arg: bool = True\n"
            "-) -> str:\n"
            "+def explode_to_unnest_sql(self: Generator, expression: exp.Lateral) -> str:\n"
            "     alias = expression.args.get(\"alias\")\n"
        )
        self.assertEqual(len(self.resolver.resolve(patch_sig_valid)), 7)

        patch_sig_decorated_rejected = (
            "diff --git a/sqlglot/dialects/dialect.py b/sqlglot/dialects/dialect.py\n"
            "--- a/sqlglot/dialects/dialect.py\n"
            "+++ b/sqlglot/dialects/dialect.py\n"
            "@@ -2912,4 +2912,5 @@ def groupconcat_sql(\n"
            "-def explode_to_unnest_sql(\n"
            "-    self: Generator, expression: exp.Lateral\n"
            "-) -> str:\n"
            "+@custom_dec\n"
            "+def explode_to_unnest_sql(self: Generator, expression: exp.Lateral) -> str:\n"
            "     alias = expression.args.get(\"alias\")\n"
        )
        self.assertGreater(len(self.resolver.resolve(patch_sig_decorated_rejected)), 1000)

        # 3. Guard 2B (`else:` inside `_resolve_func_body_hunk`):
        # Editing a complete `if ...: ... else: ...` block resolves to statement units (11 tests),
        # whereas deleting `else:` alone without its matching `if ` header falls back to `func_tests` (95 tests).
        patch_if_else_complete = (
            "diff --git a/sqlglot/optimizer/optimize_joins.py b/sqlglot/optimizer/optimize_joins.py\n"
            "--- a/sqlglot/optimizer/optimize_joins.py\n"
            "+++ b/sqlglot/optimizer/optimize_joins.py\n"
            "@@ -107,6 +107,1 @@ def normalize(\n"
            "-            # comment\n"
            "-            on = join.args.get(\"on\")\n"
            "-            if join.args.get(\"using\") or (on and on != exp.true()):\n"
            "-                join.set(\"kind\", None)\n"
            "-            else:\n"
            "-                join.set(\"on\", None)\n"
            "+            join.set(\"on\", None)\n"
        )
        self.assertEqual(len(self.resolver.resolve(patch_if_else_complete)), 11)

        patch_else_only_rejected = (
            "diff --git a/sqlglot/optimizer/optimize_joins.py b/sqlglot/optimizer/optimize_joins.py\n"
            "--- a/sqlglot/optimizer/optimize_joins.py\n"
            "+++ b/sqlglot/optimizer/optimize_joins.py\n"
            "@@ -111,2 +111,1 @@ def normalize(\n"
            "-        else:\n"
            "-            join.set(\"kind\", None)\n"
            "+        join.set(\"kind\", None)\n"
        )
        self.assertEqual(len(self.resolver.resolve(patch_else_only_rejected)), 95)

        # 4. Guard 2C (Docstring interior edit vs triple-quote code injection):
        patch_docstring_interior = (
            "diff --git a/sqlglot/optimizer/unnest_subqueries.py b/sqlglot/optimizer/unnest_subqueries.py\n"
            "--- a/sqlglot/optimizer/unnest_subqueries.py\n"
            "+++ b/sqlglot/optimizer/unnest_subqueries.py\n"
            "@@ -18,2 +18,2 @@ def unnest_subqueries(expression: exp.Expr) -> exp.Expr:\n"
            "-        SELECT * FROM x AS x\n"
            "+        SELECT * FROM x AS y\n"
        )
        self.assertEqual(self.resolver.resolve(patch_docstring_interior), [])

        patch_docstring_injection_rejected = (
            "diff --git a/sqlglot/optimizer/unnest_subqueries.py b/sqlglot/optimizer/unnest_subqueries.py\n"
            "--- a/sqlglot/optimizer/unnest_subqueries.py\n"
            "+++ b/sqlglot/optimizer/unnest_subqueries.py\n"
            "@@ -18,1 +18,3 @@ def unnest_subqueries(expression: exp.Expr) -> exp.Expr:\n"
            "-        SELECT * FROM x AS x\n"
            "+        \"\"\"\n"
            "+        raise RuntimeError()\n"
            "+        \"\"\"\n"
        )
        self.assertEqual(len(self.resolver.resolve(patch_docstring_injection_rejected)), 93)

    def test_phase9_opt2_stmt_attr_readers_and_boolop_subunits(self) -> None:
        # 1. Statement-unit attribute reader resolution (`AT_TIME_ZONE_READS_SESSION_ZONE` -> 17 tests, `535z` -> 7 tests)
        self.assertIn("AT_TIME_ZONE_READS_SESSION_ZONE", self.resolver.attr_reader_tests)
        self.assertEqual(len(self.resolver.attr_reader_tests["AT_TIME_ZONE_READS_SESSION_ZONE"]), 17)
        patch_535z = Path("blast-radius-sample/sample/commits/535z.patch").read_text(encoding="utf-8")
        sel_535z = self.resolver.resolve(patch_535z)
        self.assertEqual(len(sel_535z), 7)
        self.assertIn(1434, sel_535z)

        # 2. Guard 4A-1: Bare `ast.Name(ctx=Load)` class-body references (`ALTERABLES`, `TIMES`, `JODA_TIME_MAPPING`)
        # must be classified as unsafe_attrs and excluded from `attr_reader_tests`.
        for unsafe_name in ("ALTERABLES", "TIMES", "JODA_TIME_MAPPING"):
            self.assertIn(unsafe_name, self.resolver.unsafe_attrs)
            self.assertNotIn(unsafe_name, self.resolver.attr_reader_tests)

        # 3. Guard 4B: `TIME_MAPPING` transitive `cross_attr_deps` + reader pool (including #1568 in `test_time.py`)
        self.assertIn("TIME_MAPPING", self.resolver.attr_reader_tests)
        self.assertIn(1568, self.resolver.attr_reader_tests["TIME_MAPPING"])
        patch_hive_tm = (
            "diff --git a/sqlglot/dialects/hive.py b/sqlglot/dialects/hive.py\n"
            "--- a/sqlglot/dialects/hive.py\n"
            "+++ b/sqlglot/dialects/hive.py\n"
            "@@ -76,1 +76,0 @@ class Hive(Dialect):\n"
            '-        "SSS": "%f_three",\n'
        )
        sel_hive_tm = self.resolver.resolve(patch_hive_tm)
        self.assertEqual(len(sel_hive_tm), 36)
        self.assertIn(818, sel_hive_tm)

        # 4. Guard 3B-1: Pure leaf-class deletion (`class Pos(Unary):`) unions `class_to_tests[Pos]` with `attr_reader_tests["Pos"]`
        patch_del_pos = (
            "diff --git a/sqlglot/expressions/core.py b/sqlglot/expressions/core.py\n"
            "--- a/sqlglot/expressions/core.py\n"
            "+++ b/sqlglot/expressions/core.py\n"
            "@@ -2297,4 +2297,0 @@ class Neg(Unary):\n"
            "-class Pos(Unary):\n"
            "-    pass\n"
            "-\n"
            "-\n"
        )
        sel_del_pos = self.resolver.resolve(patch_del_pos)
        self.assertEqual(len(sel_del_pos), 34)
        for tnum in (862, 863, 1434, 1106):
            self.assertIn(tnum, sel_del_pos)

        # 5. Parenthesized nested `BoolOp` sub-unit scoping (`2jrq` -> 10 tests with 8/8 recall; flat `rszg` -> 4 tests with 3/3 recall)
        patch_2jrq = Path("blast-radius-sample/sample/commits/2jrq.patch").read_text(encoding="utf-8")
        sel_2jrq = self.resolver.resolve(patch_2jrq)
        self.assertEqual(len(sel_2jrq), 10)
        for gt_t in self.answers["2jrq"]["failing"]:
            self.assertIn(gt_t, sel_2jrq)

        patch_rszg = Path("blast-radius-sample/sample/commits/rszg.patch").read_text(encoding="utf-8")
        sel_rszg = self.resolver.resolve(patch_rszg)
        self.assertEqual(len(sel_rszg), 4)
        for gt_t in self.answers["rszg"]["failing"]:
            self.assertIn(gt_t, sel_rszg)

    def test_phase10_generalization_optimizations_and_adversarial_guards(self) -> None:
        # 1. Exact read-line attribute reader lookup before statement-unit fallback:
        # `SUPPORTS_MATCH_CONDITION` is read on short-circuited continuation line 3053 of `Generator.join_sql`
        # (where line 3052 executes in 159 tests including `#1106`), so its reader pool narrows to 6 tests and excludes `#1106`.
        self.assertEqual(
            self.resolver.attr_reader_tests.get("SUPPORTS_MATCH_CONDITION"),
            [687, 773, 1341, 1368, 1377, 1434],
        )
        self.assertNotIn(1106, self.resolver.attr_reader_tests.get("SUPPORTS_MATCH_CONDITION", []))

        # 2. DictComp-unwrapped TRANSFORMS per-key resolution (`Spark2Generator.TRANSFORMS[e:RegexpReplace]` -> 10 tests vs 272)
        patch_spark2_dictcomp = (
            "diff --git a/sqlglot/generators/spark2.py b/sqlglot/generators/spark2.py\n"
            "--- a/sqlglot/generators/spark2.py\n"
            "+++ b/sqlglot/generators/spark2.py\n"
            "@@ -204,1 +204,1 @@ class Spark2Generator(HiveGenerator):\n"
            '-                e.args.get("replacement") or exp.Literal.string(""),\n'
            '+                e.args["replacement"],\n'
        )
        sel_spark2_dc = self.resolver.resolve(patch_spark2_dictcomp)
        self.assertEqual(len(sel_spark2_dc), 10)
        self.assertIn(818, sel_spark2_dc)

        # 3. Callable-returning factory function resolution (`kflag == 1`) via recorded `.<locals>.*` closure coverage:
        # Editing inside `build_date_delta_with_interval` resolves to 24 tests (direct + `.<locals>._builder`),
        # whereas dedenting out to module level falls back to full `dialect.py` closure (1,589 tests).
        patch_factory_valid = (
            "diff --git a/sqlglot/dialects/dialect.py b/sqlglot/dialects/dialect.py\n"
            "--- a/sqlglot/dialects/dialect.py\n"
            "+++ b/sqlglot/dialects/dialect.py\n"
            "@@ -1950,2 +1950,2 @@ def build_date_delta_with_interval(\n"
            "-        if len(args) < 2:\n"
            "-            return expression_class(this=seq_get(args, 0))\n"
            "+        if len(args) < 2:\n"
            "+            return expression_class(this=seq_get(args, 0), allow_null=True)\n"
        )
        sel_fac_valid = self.resolver.resolve(patch_factory_valid)
        self.assertEqual(len(sel_fac_valid), 24)
        self.assertIn(179, sel_fac_valid)

        patch_factory_dedent = (
            "diff --git a/sqlglot/dialects/dialect.py b/sqlglot/dialects/dialect.py\n"
            "--- a/sqlglot/dialects/dialect.py\n"
            "+++ b/sqlglot/dialects/dialect.py\n"
            "@@ -1950,2 +1950,3 @@ def build_date_delta_with_interval(\n"
            "-        if len(args) < 2:\n"
            "-            return expression_class(this=seq_get(args, 0))\n"
            "+        if len(args) < 2:\n"
            "+            return expression_class(this=seq_get(args, 0))\n"
            "+_MODULE_ESCAPE = 1\n"
        )
        sel_fac_dedent = self.resolver.resolve(patch_factory_dedent)
        self.assertGreater(len(sel_fac_dedent), 1500)

        # 4. Pure `if <pure_cond>:\n <body>` guard deletion scoping:
        # Deleting pure `if prev_table and current is not None:\n self._raise_error(...)` in `Parser._parse_table_parts_fast`
        # narrows to the `<body>` unit (`[1473]`), whereas a stateful condition `self._match(...)` falls back to all 771 tests.
        patch_pure_if_del = (
            "diff --git a/sqlglot/parser.py b/sqlglot/parser.py\n"
            "--- a/sqlglot/parser.py\n"
            "+++ b/sqlglot/parser.py\n"
            "@@ -5099,7 +5099,5 @@ class Parser:\n"
            "             prev_table = current\n"
            "             current = self._parse_object()\n"
            "-            if prev_table and current is not None:\n"
            '-                self._raise_error("Expected table name, got object")\n'
            " \n"
            "             if not current:\n"
        )
        self.assertEqual(self.resolver.resolve(patch_pure_if_del), [1473])

        patch_stateful_if_del = (
            "diff --git a/sqlglot/parser.py b/sqlglot/parser.py\n"
            "--- a/sqlglot/parser.py\n"
            "+++ b/sqlglot/parser.py\n"
            "@@ -5099,7 +5099,5 @@ class Parser:\n"
            "             prev_table = current\n"
            "             current = self._parse_object()\n"
            "-            if self._match(TokenType.COMMA) and current is not None:\n"
            '-                self._raise_error("Expected table name, got object")\n'
            " \n"
            "             if not current:\n"
        )
        self.assertEqual(len(self.resolver.resolve(patch_stateful_if_del)), 771)

        # 5. Subclass safe-attribute pure addition (`+ SUPPORTS_MULTI_PART_INTERVAL_ADD = True` on `Spark2Parser` -> 18 tests),
        # whereas adding an unknown/unsafe attribute falls back to the entire `Spark2Parser` class (195 tests).
        patch_subclass_safe_attr = (
            "diff --git a/sqlglot/parsers/spark2.py b/sqlglot/parsers/spark2.py\n"
            "--- a/sqlglot/parsers/spark2.py\n"
            "+++ b/sqlglot/parsers/spark2.py\n"
            "@@ -41,2 +41,3 @@ class Spark2Parser(HiveParser):\n"
            '     PIVOT_COLUMN_NAMING = "agg_name_if_multiple"\n'
            "+    SUPPORTS_MULTI_PART_INTERVAL_ADD = True\n"
            " \n"
        )
        sel_sub_attr = self.resolver.resolve(patch_subclass_safe_attr)
        self.assertEqual(len(sel_sub_attr), 18)
        self.assertIn(818, sel_sub_attr)

        patch_subclass_unsafe_attr = (
            "diff --git a/sqlglot/parsers/spark2.py b/sqlglot/parsers/spark2.py\n"
            "--- a/sqlglot/parsers/spark2.py\n"
            "+++ b/sqlglot/parsers/spark2.py\n"
            "@@ -41,2 +41,3 @@ class Spark2Parser(HiveParser):\n"
            '     PIVOT_COLUMN_NAMING = "agg_name_if_multiple"\n'
            "+    UNKNOWN_CUSTOM_ATTR = True\n"
            " \n"
        )
        self.assertEqual(len(self.resolver.resolve(patch_subclass_unsafe_attr)), 195)


if __name__ == "__main__":
    unittest.main()
