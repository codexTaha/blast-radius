#!/usr/bin/env python3
"""Unit tests for synthetic patch adversarial edge cases."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.stress_test_synthetic import generate_synthetic_patches
from src.diff_parser import parse_patch
from src.impact_resolver import ImpactResolver
from src.worker_scheduler import WorkerScheduler


class TestSyntheticStress(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        bundle_path = Path("artifacts/runtime_index.json")
        cls.bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        cls.resolver = ImpactResolver(cls.bundle)
        cls.scheduler = WorkerScheduler(cls.resolver.test_durations)
        cls.patches = generate_synthetic_patches()

    def test_all_synthetic_patches_execute_cleanly(self) -> None:
        for name, patch_text in self.patches.items():
            parsed = parse_patch(patch_text)
            self.assertIsNotNone(parsed, f"Patch {name} parsed to None")

            meta = self.resolver.resolve_with_metadata(patch_text)
            selected = meta["selected_tests"]
            self.assertIsInstance(selected, list, f"Patch {name} did not return list of tests")

            sched = self.scheduler.schedule(selected)
            workers = sched["workers"]
            k = sched["k"]

            self.assertTrue(0 <= k <= 8, f"Patch {name} chose invalid k={k}")
            self.assertEqual(len(workers), k, f"Patch {name} worker count mismatch")

            # Check workers are disjoint
            flattened = [t for w in workers for t in w]
            self.assertEqual(len(flattened), len(set(flattened)), f"Patch {name} has overlapping worker partitions")

    def test_empty_and_docs_patches_select_zero(self) -> None:
        for name in ["syn_01_empty", "syn_03_non_code_docs"]:
            patch_text = self.patches[name]
            meta = self.resolver.resolve_with_metadata(patch_text)
            self.assertEqual(meta["selected_tests"], [], f"{name} selected non-empty tests")

    def test_corrupted_patch_does_not_crash(self) -> None:
        corrupted = self.patches["syn_09_corrupted_patch"]
        meta = self.resolver.resolve_with_metadata(corrupted)
        self.assertIsInstance(meta["selected_tests"], list)

    def test_new_optimizer_file_triggers_bridge(self) -> None:
        patch = self.patches["syn_12_new_optimizer_file"]
        meta = self.resolver.resolve_with_metadata(patch)
        self.assertIn("tests/test_optimizer.py", meta["triggers"]["tier4_dynamic_files"])
        self.assertIn("tests/test_executor.py", meta["triggers"]["tier4_dynamic_files"])

    def test_direct_test_edit_triggers_tier0(self) -> None:
        patch = self.patches["syn_13_edit_test_file"]
        meta = self.resolver.resolve_with_metadata(patch)
        self.assertIn("tests/dialects/test_postgres.py", meta["triggers"]["tier0_test_files"])

    def test_tokenizer_only_synthetic_isolation(self) -> None:
        # syn_14: duckdb tokenizer body only -> duckdb::tokenizer_only trigger
        meta14 = self.resolver.resolve_with_metadata(self.patches["syn_14_tokenizer_only_duckdb"])
        self.assertIn("duckdb::tokenizer_only", meta14["triggers"]["tier2_dialects"])
        self.assertNotIn("duckdb", meta14["triggers"]["tier2_dialects"])

        # syn_15: duckdb tokenizer class header modification -> conservative fallback
        meta15 = self.resolver.resolve_with_metadata(self.patches["syn_15_tokenizer_class_header"])
        self.assertNotIn("duckdb::tokenizer_only", meta15["triggers"]["tier2_dialects"])
        self.assertIn("duckdb", meta15["triggers"]["tier2_dialects"])

        # syn_16: multi-hunk mixed (tokenizer + column_alias_precedence) -> conservative fallback
        meta16 = self.resolver.resolve_with_metadata(self.patches["syn_16_tokenizer_multi_hunk_mixed"])
        self.assertNotIn("duckdb::tokenizer_only", meta16["triggers"]["tier2_dialects"])
        self.assertIn("duckdb", meta16["triggers"]["tier2_dialects"])

        # syn_17: multi-hunk all inside tokenizer -> duckdb::tokenizer_only trigger
        meta17 = self.resolver.resolve_with_metadata(self.patches["syn_17_tokenizer_multi_hunk_all_tok"])
        self.assertIn("duckdb::tokenizer_only", meta17["triggers"]["tier2_dialects"])
        self.assertNotIn("duckdb", meta17["triggers"]["tier2_dialects"])

        # syn_18: dialect file deletion -> conservative fallback
        meta18 = self.resolver.resolve_with_metadata(self.patches["syn_18_dialect_file_deletion"])
        self.assertNotIn("duckdb::tokenizer_only", meta18["triggers"]["tier2_dialects"])
        self.assertIn("duckdb", meta18["triggers"]["tier2_dialects"])

        # syn_19: dialect deletion inside Tokenizer -> tokenizer_only isolation (with full runtime MRO callers)
        meta19 = self.resolver.resolve_with_metadata(self.patches["syn_19_tokenizer_deletion_inside"])
        self.assertIn("snowflake::tokenizer_only", meta19["triggers"]["tier2_dialects"])
        self.assertEqual(len(meta19["selected_tests"]), 273)

        # syn_20: dialect deletion crossing Tokenizer boundary -> conservative fallback
        meta20 = self.resolver.resolve_with_metadata(self.patches["syn_20_tokenizer_deletion_cross"])
        self.assertNotIn("snowflake::tokenizer_only", meta20["triggers"]["tier2_dialects"])
        self.assertIn("snowflake", meta20["triggers"]["tier2_dialects"])
        self.assertEqual(len(meta20["selected_tests"]), 363)

        # syn_21: dialect file pure rename -> conservative fallback
        meta21 = self.resolver.resolve_with_metadata(self.patches["syn_21_dialect_rename_pure"])
        self.assertNotIn("snowflake::tokenizer_only", meta21["triggers"]["tier2_dialects"])
        self.assertIn("snowflake", meta21["triggers"]["tier2_dialects"])
        self.assertEqual(len(meta21["selected_tests"]), 363)

        # syn_22: dialect file rename with mod -> conservative fallback
        meta22 = self.resolver.resolve_with_metadata(self.patches["syn_22_dialect_rename_mod"])
        self.assertNotIn("snowflake::tokenizer_only", meta22["triggers"]["tier2_dialects"])
        self.assertIn("snowflake", meta22["triggers"]["tier2_dialects"])
        self.assertEqual(len(meta22["selected_tests"]), 363)


if __name__ == "__main__":
    unittest.main()
