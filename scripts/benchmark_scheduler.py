#!/usr/bin/env python3
"""Stage 4.1 — Worker Scheduler Latency & Optimization Benchmark.

Profiles scheduling throughput, per-commit latency, and makespan optimization
across all 30 sample commits.
"""

from __future__ import annotations

import glob
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.impact_resolver import ImpactResolver
from src.worker_scheduler import WorkerScheduler


def main() -> None:
    bundle_path = Path("artifacts/runtime_index.json")
    answers_path = Path("blast-radius-sample/sample/answers.json")
    commits_dir = Path("blast-radius-sample/sample/commits")

    resolver = ImpactResolver(bundle_path)
    scheduler = WorkerScheduler(resolver.test_durations)
    answers = json.loads(answers_path.read_text(encoding="utf-8"))["commits"]

    # Pre-resolve test sets for all 30 commits
    resolved_test_sets: list[list[int]] = []
    for cid in sorted(answers.keys()):
        patch = (commits_dir / f"{cid}.patch").read_text(encoding="utf-8")
        resolved_test_sets.append(resolver.resolve(patch))

    # Benchmark scheduling latency over 1,000 iterations (30,000 schedule calls)
    iterations = 1_000
    total_calls = iterations * len(resolved_test_sets)
    print(f"Benchmarking {total_calls:,} scheduling evaluations ({iterations:,} loops of 30 commits) ...", file=sys.stderr)

    t0 = time.perf_counter()
    for _ in range(iterations):
        for tests in resolved_test_sets:
            scheduler.schedule(tests)
    elapsed = time.perf_counter() - t0

    us_per_schedule = (elapsed / total_calls) * 1_000_000
    calls_per_sec = total_calls / elapsed
    ms_for_40_commits = (us_per_schedule * 40) / 1000

    print("\n=== Stage 4.1 Scheduler Benchmark Results ===", file=sys.stderr)
    print(f"Total schedules:       {total_calls:,}", file=sys.stderr)
    print(f"Total wall time:       {elapsed:.3f} s", file=sys.stderr)
    print(f"Throughput:            {calls_per_sec:,.0f} schedules/sec", file=sys.stderr)
    print(f"Latency per schedule:  {us_per_schedule:.2f} µs ({us_per_schedule / 1000:.4f} ms per commit)", file=sys.stderr)
    print(f"40-commit batch time:  {ms_for_40_commits:.3f} ms CPU overhead", file=sys.stderr)
    print(f"Target latency:        < 500.00 µs (0.50 ms)", file=sys.stderr)

    if us_per_schedule < 500.0:
        print(f"PASS: Scheduler latency is {500.0 / us_per_schedule:.1f}x faster than target threshold.\n", file=sys.stderr)
    else:
        print("FAIL: Scheduler exceeded latency threshold.\n", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
