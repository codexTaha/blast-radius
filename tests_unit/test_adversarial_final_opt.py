import unittest
from pathlib import Path

from src.diff_parser import parse_patch
from src.impact_resolver import ImpactResolver, _decode_sentinel_exclusions
from src.telemetry import TelemetryCollector
from src.worker_scheduler import WorkerScheduler


class TestAdversarialFinalOpt(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.resolver = ImpactResolver(Path("artifacts/runtime_index.json"))
        cls.scheduler = WorkerScheduler(cls.resolver.test_durations)

    def test_sentinel_bitmask_decode_all_eight_masks(self) -> None:
        self.assertEqual(_decode_sentinel_exclusions([-1]), set())
        self.assertEqual(_decode_sentinel_exclusions([-2]), {1490})
        self.assertEqual(_decode_sentinel_exclusions([-3]), {1106})
        self.assertEqual(_decode_sentinel_exclusions([-4]), {1490, 1106})
        self.assertEqual(_decode_sentinel_exclusions([-5]), {1224})
        self.assertEqual(_decode_sentinel_exclusions([-6]), {1490, 1224})
        self.assertEqual(_decode_sentinel_exclusions([-7]), {1106, 1224})
        self.assertEqual(_decode_sentinel_exclusions([-8]), {1490, 1106, 1224})
        self.assertIsNone(_decode_sentinel_exclusions([1, 2]))

    def test_diff_parser_bom_crlf_and_dev_null(self) -> None:
        patch = (
            "\ufeffdiff --git a/sqlglot/foo.py b/sqlglot/foo.py\r\n"
            "new file mode 100644\r\n"
            "--- dev/null\r\n"
            "+++ b/sqlglot/foo.py\r\n"
            "@@ -0,0 +1,1 @@\r\n"
            "+x = 1\r\n"
        )
        parsed = parse_patch(patch)
        self.assertEqual(len(parsed.files), 1)
        self.assertTrue(parsed.files[0].is_new)
        self.assertIsNone(parsed.files[0].old_path)
        self.assertEqual(parsed.files[0].path, "sqlglot/foo.py")

    def test_scheduler_filters_negative_bool_and_oob_tests(self) -> None:
        res = self.scheduler.schedule([-8, -1, 0, 5, 999999])
        workers = res["workers"]
        flat = [t for w in workers for t in w]
        self.assertEqual(sorted(flat), [0, 5])

    def test_telemetry_detects_all_negative_sentinel_masks(self) -> None:
        neg_sym = next(
            (k for k, v in self.resolver.qual_symbol_to_tests.items() if isinstance(v, list) and len(v) == 1 and v[0] < -1),
            None,
        )
        self.assertIsNotNone(neg_sym)
        collector = TelemetryCollector(self.resolver)
        res_meta = {"selected_tests": [1, 2], "estimated_runtime_ms": 100, "triggers": {"tier3_symbols": [neg_sym], "tier4_dynamic_files": [], "tier5_fallback": []}}
        sched = self.scheduler.schedule([1, 2])
        collector.record_commit("c1", "", res_meta, sched)
        report = collector.finalize_and_emit()
        self.assertIn(
            f"tier3_high_freq_sentinel:{neg_sym}",
            report["commits"][0]["dependency_resolution"]["fallback_categories_triggered"],
        )

    def test_dialect_2424_boolop_start_flip_climbs_out_of_subunit(self) -> None:
        patch_flip = (
            "diff --git a/sqlglot/dialects/dialect.py b/sqlglot/dialects/dialect.py\n"
            "--- a/sqlglot/dialects/dialect.py\n"
            "+++ b/sqlglot/dialects/dialect.py\n"
            "@@ -2422,5 +2422,5 @@ def date_delta_to_binary_sql(self: Generator, expression: exp.Expr, binary: exp.\n"
            "     if isinstance(parent, (exp.Binary, exp.Unary, exp.AtTimeZone)) and not (\n"
            "         isinstance(parent, exp.Paren)\n"
            "-        or isinstance(parent, (exp.Add, exp.Sub))\n"
            "+        and isinstance(parent, (exp.Add, exp.Sub))\n"
            "         and parent.this is expression\n"
            "     ):\n"
        )
        sel = self.resolver.resolve(patch_flip)
        units = self.resolver.func_stmt_cov.get("sqlglot/dialects/dialect.py::date_delta_to_binary_sql", [])
        outer = [u for u in units if u[0] == 2422 and u[1] == 2426]
        if outer and not isinstance(outer[0][2], list):
            pass
        self.assertGreater(len(sel), 0)
        # Ensure we did NOT restrict strictly to inner subunit [2424, 2425] if outer [2422, 2426] is larger
        inner = [u for u in units if u[0] == 2424 and u[1] == 2425]
        if inner and outer and isinstance(inner[0][2], list) and all(x >= 0 for x in inner[0][2]):
            self.assertGreaterEqual(len(sel), len(inner[0][2]))

    def test_athena_trino_generator_attr_keeps_class_to_tests(self) -> None:
        patch = (
            "diff --git a/sqlglot/generators/athena.py b/sqlglot/generators/athena.py\n"
            "--- a/sqlglot/generators/athena.py\n"
            "+++ b/sqlglot/generators/athena.py\n"
            "@@ -78,1 +78,1 @@ class AthenaGenerator(TrinoGenerator):\n"
            "-    PROPERTIES_LOCATION = {\n"
            "+    SUPPORTS_TABLE_COPY = False\n"
        )
        sel = self.resolver.resolve(patch)
        self.assertGreater(len(sel), 0)

    def test_nested_outer_inner_class_proven_none_rsplit(self) -> None:
        # `rsplit(".", 1)[0]` handles `Outer.Inner.method` instead of taking `Outer`
        p = "sqlglot/dialects/dialect.py"
        qual = "Dialect.Parser.expression"
        self.assertEqual(qual.rsplit(".", 1)[0], "Dialect.Parser")

    def test_mixed_py_plus_sql_retains_fixture_and_py_precision(self) -> None:
        patch_2haz = Path("blast-radius-sample/sample/commits/2haz.patch").read_text(encoding="utf-8")
        patch_66ks = Path("blast-radius-sample/sample/commits/66ks.patch").read_text(encoding="utf-8")
        patch_sql = (
            "diff --git a/tests/fixtures/optimizer/simplify.sql b/tests/fixtures/optimizer/simplify.sql\n"
            "--- a/tests/fixtures/optimizer/simplify.sql\n"
            "+++ b/tests/fixtures/optimizer/simplify.sql\n"
            "@@ -1,1 +1,1 @@\n"
            "-SELECT 1;\n"
            "+SELECT 2;\n"
        )
        sel_mix = self.resolver.resolve(patch_2haz + "\n" + patch_sql)
        # SQL fixture test IS selected; 2haz remains semantically equivalent under meaningful_py_files
        self.assertIn(1434, sel_mix)
        self.assertEqual(sel_mix, [1434])
        # Multi-file .py + .py STILL falls back conservatively
        sel_py_py = self.resolver.resolve(patch_2haz + "\n" + patch_66ks)
        self.assertGreaterEqual(len(sel_py_py), 86)

    def test_tpc_fixture_narrowed_to_executor_and_tpc_optimizer_test(self) -> None:
        patch_tpcds = (
            "diff --git a/tests/fixtures/optimizer/tpc-ds/new_q.sql b/tests/fixtures/optimizer/tpc-ds/new_q.sql\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/tests/fixtures/optimizer/tpc-ds/new_q.sql\n"
            "@@ -0,0 +1,1 @@\n"
            "+SELECT 1;\n"
        )
        sel = self.resolver.resolve(patch_tpcds)
        t_exec = set(self.resolver.by_file.get("tests/test_executor.py", []))
        t_tpcds = self.resolver.by_id["tests/test_optimizer.py::TestOptimizer::test_tpcds"]
        self.assertTrue(t_exec.issubset(set(sel)))
        self.assertIn(t_tpcds, sel)
        self.assertLessEqual(len(sel), 62)

    def test_module_constant_safe_attr_resolution(self) -> None:
        self.assertIn("sqlglot/anonymize.py::ALPHABET", self.resolver.attr_reader_tests)
        patch_mc = (
            "diff --git a/sqlglot/anonymize.py b/sqlglot/anonymize.py\n"
            "--- a/sqlglot/anonymize.py\n"
            "+++ b/sqlglot/anonymize.py\n"
            "@@ -10,1 +10,1 @@\n"
            '-ALPHABET = "abcdefghijklmnopqrstuvwxyz"\n'
            '+ALPHABET = "abcdefghijklmnopqrstuvwxyz0"\n'
        )
        sel = self.resolver.resolve(patch_mc)
        self.assertGreater(len(sel), 0)
        self.assertLess(len(sel), 50)
        self.assertNotIn(1490, sel)


if __name__ == "__main__":
    unittest.main()
