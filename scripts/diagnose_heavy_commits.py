#!/usr/bin/env python3
"""Stage 6.1 — Heavy Commit Diagnostic & Test Isolation Analysis.

Analyzes all 30 sample commits to determine:
1. Exact cost breakdown per commit.
2. What tests are selected by Dynamic Coverage (Tiers 0-3) vs Static Import Closure (Tier 4).
3. Ground-truth recall of Dynamic Coverage alone (Tiers 0-3) vs Current (Tiers 0-4).
4. Individual test duration bottlenecks (such as test 1104 test_execute_tpcds).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Ensure project root and blast-radius-sample are in sys.path
root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))
sys.path.insert(0, str(root_dir / "blast-radius-sample"))

from src.diff_parser import parse_patch
from src.impact_resolver import ImpactResolver
from src.worker_scheduler import WorkerScheduler


def run_diagnostics() -> None:
    sample_dir = root_dir / "blast-radius-sample" / "sample"
    bundle_path = root_dir / "artifacts" / "runtime_index.json"
    tests_json = sample_dir / "tests.json"
    answers_json = sample_dir / "answers.json"
    commits_dir = sample_dir / "commits"

    tests_data = json.loads(tests_json.read_text())["tests"]
    answers_data = json.loads(answers_json.read_text())["commits"]
    bundle = json.loads(bundle_path.read_text())

    resolver = ImpactResolver(bundle)
    scheduler = WorkerScheduler(resolver.test_durations)

    diagnostics = []

    for cid, info in answers_data.items():
        failing = set(info["failing"])
        kind = info["kind"]
        patch_file = commits_dir / f"{cid}.patch"
        patch_text = patch_file.read_text()
        parsed = parse_patch(patch_text)

        meta = resolver.resolve_with_metadata(patch_text)
        current_tests = set(meta["selected_tests"])
        sched_curr = scheduler.schedule(meta["selected_tests"])

        # Breakdown what Tiers contributed to current_tests
        tier0_3_tests = set()
        tier4_tests = set()

        # Re-run resolution step-by-step
        if parsed.is_empty:
            pass
        elif parsed.is_comment_only:
            has_sensitive = any(
                f.path.startswith("tests/")
                or f.path.startswith("tests/fixtures/")
                or f.path in resolver.core_engine_files
                for f in parsed.files
            )
            if not has_sensitive:
                pass
            else:
                pass

        for f in parsed.files:
            p = f.path
            if f.is_comment_only:
                if not (
                    p.startswith("tests/")
                    or p.startswith("tests/fixtures/")
                    or p in resolver.core_engine_files
                ):
                    continue

            # Tier 0 & 1
            if p.startswith("tests/"):
                if p in resolver.by_file:
                    tier0_3_tests.update(resolver.by_file[p])
                elif p in resolver.fixture_to_tests:
                    tier0_3_tests.update(resolver.fixture_to_tests[p])
                continue

            if p in resolver.fixture_to_tests:
                tier0_3_tests.update(resolver.fixture_to_tests[p])
                continue

            # Tier 2
            if p in resolver.file_to_dialect:
                d = resolver.file_to_dialect[p]
                if d in resolver.dialect_to_tests:
                    tier0_3_tests.update(resolver.dialect_to_tests[d])
                if p in resolver.file_to_tests:
                    tier0_3_tests.update(resolver.file_to_tests[p])
                syms = resolver.file_symbols.get(p, [])
                for s_l, e_l, qn in syms:
                    for ms, me in f.modified_ranges:
                        if not (e_l < ms or s_l > me):
                            if qn in resolver.symbol_to_tests:
                                tier0_3_tests.update(resolver.symbol_to_tests[qn])
                continue

            # Tier 3 (Symbol & file dynamic coverage)
            syms = resolver.file_symbols.get(p, [])
            matched_symbols = set()
            for s_l, e_l, qn in syms:
                for ms, me in f.modified_ranges:
                    if not (e_l < ms or s_l > me):
                        matched_symbols.add(qn)
                        break

            for qn in matched_symbols:
                if qn in resolver.symbol_to_tests:
                    tier0_3_tests.update(resolver.symbol_to_tests[qn])

            if p in resolver.file_to_tests:
                tier0_3_tests.update(resolver.file_to_tests[p])

            # Tier 4 (Transitive import closure)
            mod_name = resolver._file_to_module(p)
            if mod_name in resolver.test_dependents:
                for dep_test_file in resolver.test_dependents[mod_name]:
                    if dep_test_file in resolver.by_file:
                        tier4_tests.update(resolver.by_file[dep_test_file])

        # Test recall of tier0_3 alone vs ground-truth failing
        tier0_3_caught = failing & tier0_3_tests
        tier0_3_missed = failing - tier0_3_tests

        # Calculate scheduling for tier0_3 alone
        sched_tier0_3 = scheduler.schedule(sorted(tier0_3_tests))

        # Check slow tests in current_tests
        slow_tests_in_commit = [
            (t, resolver.test_durations[t], tests_data[t]["id"] if t < len(tests_data) else "unknown")
            for t in current_tests
            if resolver.test_durations[t] > 1000  # > 1s
        ]
        slow_tests_in_commit.sort(key=lambda x: x[1], reverse=True)

        diagnostics.append({
            "commit": cid,
            "kind": kind,
            "failing_count": len(failing),
            "files": [f.path for f in parsed.files],
            "current_tests_count": len(current_tests),
            "current_k": sched_curr["k"],
            "current_makespan_s": sched_curr["makespan_ms"] / 1000.0,
            "current_cost_s": sched_curr["total_cost_ms"] / 1000.0,
            "tier0_3_tests_count": len(tier0_3_tests),
            "tier0_3_missed_count": len(tier0_3_missed),
            "tier0_3_missed": sorted(tier0_3_missed),
            "tier0_3_k": sched_tier0_3["k"],
            "tier0_3_makespan_s": sched_tier0_3["makespan_ms"] / 1000.0,
            "tier0_3_cost_s": sched_tier0_3["total_cost_ms"] / 1000.0,
            "tier4_only_count": len(tier4_tests - tier0_3_tests),
            "slow_tests": slow_tests_in_commit[:3],
        })

    # Sort by current_cost_s descending
    diagnostics.sort(key=lambda x: x["current_cost_s"], reverse=True)

    print("=" * 95)
    print(f"{'CID':<6} {'Kind':<14} {'Fail':<5} {'Curr Tests':<11} {'Curr Cost':<10} {'T0-3 Tests':<11} {'T0-3 Cost':<10} {'T0-3 Miss':<10} {'Files Changed'}")
    print("=" * 95)

    total_curr_cost = sum(d["current_cost_s"] for d in diagnostics)
    total_t0_3_cost = sum(d["tier0_3_cost_s"] for d in diagnostics)
    total_t0_3_misses = sum(d["tier0_3_missed_count"] for d in diagnostics)

    for d in diagnostics:
        files_str = ", ".join(d["files"])
        if len(files_str) > 35:
            files_str = files_str[:32] + "..."
        print(f"{d['commit']:<6} {d['kind']:<14} {d['failing_count']:<5} {d['current_tests_count']:<11} {d['current_cost_s']:<10.3f} {d['tier0_3_tests_count']:<11} {d['tier0_3_cost_s']:<10.3f} {d['tier0_3_missed_count']:<10} {files_str}")

    print("=" * 95)
    print(f"Total Current Cost: {total_curr_cost:.3f} s")
    print(f"Total Tier 0-3 Cost (Dynamic Only): {total_t0_3_cost:.3f} s (reduction of {total_curr_cost - total_t0_3_cost:.3f} s!)")
    print(f"Total Tier 0-3 Misses: {total_t0_3_misses}")
    print("=" * 95)

    # Detailed report on missed commits if any
    missed_commits = [d for d in diagnostics if d["tier0_3_missed_count"] > 0]
    if missed_commits:
        print("\n--- COMMITS WHERE TIER 0-3 (DYNAMIC COVERAGE) MISSED FAILING TESTS ---")
        for d in missed_commits:
            print(f"Commit {d['commit']} (kind={d['kind']}):")
            print(f"  Files: {d['files']}")
            print(f"  Failing tests total: {d['failing_count']}")
            print(f"  Missed tests ({d['tier0_3_missed_count']}): {d['tier0_3_missed']}")
            for t in d['tier0_3_missed']:
                name = tests_data[t]["id"] if t < len(tests_data) else "unknown"
                print(f"    - Test {t}: {name}")
    else:
        print("\nASTONISHING RESULT: ZERO MISSES ACROSS ALL 30 COMMITS WITH DYNAMIC COVERAGE ALONE!")

    # Check the slow tests impact
    print("\n--- TEST 1104 (test_execute_tpcds, ~21.5s) OCCURRENCES ---")
    for d in diagnostics:
        t1104 = [st for st in d["slow_tests"] if st[0] == 1104]
        if t1104:
            print(f"Commit {d['commit']} includes test 1104! Failing tests in this commit: {d['failing_count']}. In Tier 0-3? {1104 in range(d['tier0_3_tests_count'])}")


if __name__ == "__main__":
    run_diagnostics()
