#!/usr/bin/env python3
"""Stage 2.5 — Test-to-Source Dynamic Coverage Collection.

Runs Pytest with sys.monitoring (Python 3.12+ PEP 669) across all 58 test files
with SKIP_INTEGRATION=0 to capture ground-truth execution coverage:
- file_to_tests: {source_file: [test_numbers]}
- test_to_files: {test_number: [source_files]}
- symbol_to_tests: {"source_file::function_name": [test_numbers]}

Produces artifacts/test_coverage.json.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

# Verify Python 3.12+ sys.monitoring
if not hasattr(sys, "monitoring"):
    raise RuntimeError("sys.monitoring is required for high-speed coverage collection (Python 3.12+).")

import pytest

TOOL_ID = sys.monitoring.COVERAGE_ID


class DynamicCoverageCollector:
    def __init__(self, repo_prefix: str, id_to_number: dict[str, int]):
        self.repo_prefix = repo_prefix
        self.id_to_number = id_to_number

        self.current_test_number: int | None = None
        self.current_files: set[str] = set()
        self.current_symbols: set[str] = set()

        self.test_to_files: dict[int, list[str]] = {}
        self.test_to_symbols: dict[int, list[str]] = {}

        self.passed_tests: list[int] = []
        self.failed_tests: list[int] = []

        # Caching file lookups for sub-microsecond event handling
        self._file_cache: dict[str, str | None] = {}

        sys.monitoring.use_tool_id(TOOL_ID, "blast_radius_coverage")

    def on_py_start(self, code: Any, instruction_offset: int) -> None:
        fn = code.co_filename
        if fn in self._file_cache:
            rel = self._file_cache[fn]
            if rel is not None:
                self.current_files.add(rel)
                self.current_symbols.add(f"{rel}::{code.co_name}")
            return

        if fn.startswith(self.repo_prefix):
            rel = os.path.relpath(fn, self.repo_prefix)
            if rel.startswith("sqlglot/"):
                self._file_cache[fn] = rel
                self.current_files.add(rel)
                self.current_symbols.add(f"{rel}::{code.co_name}")
            else:
                self._file_cache[fn] = None
        else:
            self._file_cache[fn] = None

    def pytest_runtest_setup(self, item: pytest.Item) -> None:
        self.current_test_number = self.id_to_number.get(item.nodeid)
        self.current_files = set()
        self.current_symbols = set()

        if self.current_test_number is not None:
            sys.monitoring.register_callback(TOOL_ID, sys.monitoring.events.PY_START, self.on_py_start)
            sys.monitoring.set_events(TOOL_ID, sys.monitoring.events.PY_START)

    def pytest_runtest_teardown(self, item: pytest.Item) -> None:
        sys.monitoring.set_events(TOOL_ID, sys.monitoring.events.NO_EVENTS)

        if self.current_test_number is not None:
            self.test_to_files[self.current_test_number] = sorted(self.current_files)
            self.test_to_symbols[self.current_test_number] = sorted(self.current_symbols)

        self.current_test_number = None

    def pytest_runtest_logreport(self, report: Any) -> None:
        if report.when == "call":
            num = self.id_to_number.get(report.nodeid)
            if num is not None:
                if report.passed:
                    self.passed_tests.append(num)
                elif report.failed:
                    self.failed_tests.append(num)


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect test-to-source coverage using sys.monitoring.")
    parser.add_argument("--repo", type=Path, default=Path("/workspace/sqlglot"), help="SQLGlot repository root")
    parser.add_argument("--test-index", type=Path, default=Path("/workspace/artifacts/test_index.json"))
    parser.add_argument("--out", type=Path, default=Path("/workspace/artifacts/test_coverage.json"))
    args = parser.parse_args()

    print(f"Loading test index from {args.test_index} ...", file=sys.stderr)
    test_index = json.loads(args.test_index.read_text(encoding="utf-8"))
    id_to_number = test_index["by_id"]

    repo_prefix = str(args.repo.resolve())
    collector = DynamicCoverageCollector(repo_prefix, id_to_number)

    test_dirs = [str(args.repo / "tests")]
    print(f"Running test suite in {test_dirs} with sys.monitoring coverage ...", file=sys.stderr)
    t0 = time.time()

    pytest_args = [
        "-q",
        "--tb=short",
        "-o", "filterwarnings=ignore",
        *test_dirs,
    ]

    exit_code = pytest.main(pytest_args, plugins=[collector])
    elapsed = time.time() - t0

    print(f"Pytest finished in {elapsed:.2f} s with exit code {exit_code}", file=sys.stderr)
    print(f"  Tracked tests: {len(collector.test_to_files)} / {len(id_to_number)}", file=sys.stderr)
    print(f"  Passed tests:  {len(collector.passed_tests)}", file=sys.stderr)
    print(f"  Failed tests:  {len(collector.failed_tests)}", file=sys.stderr)

    # Invert mappings: file -> tests, symbol -> tests
    file_to_tests: dict[str, list[int]] = defaultdict(list)
    symbol_to_tests: dict[str, list[int]] = defaultdict(list)

    for num, files in collector.test_to_files.items():
        for f in files:
            file_to_tests[f].append(num)

    for num, syms in collector.test_to_symbols.items():
        for s in syms:
            symbol_to_tests[s].append(num)

    # Sort test ID lists
    file_to_tests = {k: sorted(v) for k, v in sorted(file_to_tests.items())}
    symbol_to_tests = {k: sorted(v) for k, v in sorted(symbol_to_tests.items())}

    result = {
        "metadata": {
            "sqlglot_commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
            "coverage_duration_seconds": round(elapsed, 2),
            "total_tracked_tests": len(collector.test_to_files),
            "covered_source_files": len(file_to_tests),
            "covered_symbols": len(symbol_to_tests),
            "passed_tests_count": len(collector.passed_tests),
            "failed_tests_count": len(collector.failed_tests),
        },
        "file_to_tests": file_to_tests,
        "test_to_files": {str(k): v for k, v in sorted(collector.test_to_files.items())},
        "symbol_to_tests": symbol_to_tests,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(f"Coverage map written to {args.out} ({args.out.stat().st_size / (1024*1024):.2f} MB)", file=sys.stderr)
    print(f"  Covered source files: {len(file_to_tests)}", file=sys.stderr)
    print(f"  Covered symbols:      {len(symbol_to_tests)}", file=sys.stderr)


if __name__ == "__main__":
    main()
