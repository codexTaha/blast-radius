"""Unit tests for Stage 10 Optional Diagnostic Telemetry (`src/telemetry.py`)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.impact_resolver import ImpactResolver
from src.telemetry import TelemetryCollector, is_telemetry_enabled
from src.worker_scheduler import WorkerScheduler


class TestTelemetry(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parent.parent
        cls.resolver = ImpactResolver(cls.repo_root / "artifacts" / "runtime_index.json")
        cls.scheduler = WorkerScheduler(cls.resolver.test_durations)
        cls.sample_dir = cls.repo_root / "blast-radius-sample" / "sample"

    def test_disabled_by_default(self) -> None:
        self.assertFalse(is_telemetry_enabled())
        self.assertTrue(is_telemetry_enabled(cli_enabled=True))
        self.assertTrue(is_telemetry_enabled(cli_out_path="/tmp/test_telemetry.json"))

    def test_telemetry_schema_and_fail_safe_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = Path(tmpdir) / "telemetry.json"
            collector = TelemetryCollector(self.resolver, out_path=out_file)

            for p in sorted((self.sample_dir / "commits").glob("*.patch"))[:5]:
                patch_text = p.read_text(encoding="utf-8")
                res_meta = self.resolver.resolve_with_metadata(patch_text)
                sched = self.scheduler.schedule(res_meta["selected_tests"])
                collector.record_commit(p.stem, patch_text, res_meta, sched)

            report = collector.finalize_and_emit()
            self.assertIsNotNone(report)
            self.assertTrue(out_file.exists())
            loaded = json.loads(out_file.read_text(encoding="utf-8"))
            self.assertIn("aggregate_report", loaded)
            self.assertIn("commits", loaded)
            self.assertEqual(loaded["aggregate_report"]["processed_commits"], 5)
            for rec in loaded["commits"]:
                self.assertIn("commit_processing", rec)
                self.assertIn("dependency_resolution", rec)
                self.assertIn("test_selection", rec)
                self.assertIn("scheduling", rec)

        # Verify fail-safe exception isolation on an unwritable path
        bad_collector = TelemetryCollector(self.resolver, out_path="/proc/1/unwritable_telemetry.json")
        bad_collector.record_commit("dummy", "", {"selected_tests": []}, {"workers": [], "k": 0})
        # Must not raise
        bad_collector.finalize_and_emit()

    def test_selector_stdout_parity_with_and_without_telemetry(self) -> None:
        # 1. Default run (telemetry disabled)
        proc_default = subprocess.run(
            [sys.executable, str(self.repo_root / "selector.py"), "--data", str(self.sample_dir)],
            capture_output=True,
            check=True,
        )
        sha_default = hashlib.sha256(proc_default.stdout).hexdigest()

        # 2. Telemetry-enabled run (--telemetry and --telemetry-out)
        with tempfile.TemporaryDirectory() as tmpdir:
            tel_out = Path(tmpdir) / "run_telemetry.json"
            proc_tel = subprocess.run(
                [
                    sys.executable,
                    str(self.repo_root / "selector.py"),
                    "--data",
                    str(self.sample_dir),
                    "--telemetry",
                    "--telemetry-out",
                    str(tel_out),
                ],
                capture_output=True,
                check=True,
            )
            sha_tel = hashlib.sha256(proc_tel.stdout).hexdigest()
            self.assertEqual(
                sha_default,
                sha_tel,
                "Enabling telemetry must produce 100% bit-identical stdout JSONL",
            )
            self.assertTrue(tel_out.exists())
            rep = json.loads(tel_out.read_text(encoding="utf-8"))
            self.assertEqual(rep["aggregate_report"]["processed_commits"], 30)


if __name__ == "__main__":
    unittest.main()
