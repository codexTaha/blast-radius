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


if __name__ == "__main__":
    unittest.main()
