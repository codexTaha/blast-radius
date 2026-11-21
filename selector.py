#!/usr/bin/env python3
"""Blast Radius — Production Test Selection & Scheduling Engine.

Reads unified diff patches from /data/commits/*.patch, resolves test impact
using precomputed AST/coverage/dialect intelligence, and schedules selected tests
across optimal workers k* in [0..8] to minimize:
    J(k, P) = makespan(k, P) + 2000 * k

Outputs strictly valid UTF-8 JSONL to stdout (one line per commit).
All diagnostics and logs are written exclusively to stderr.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Add script directory to sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from src.impact_resolver import ImpactResolver
from src.worker_scheduler import WorkerScheduler


def find_data_dir(cli_arg: str | None = None) -> Path:
    """Determine the data directory containing 'commits/' and 'tests.json'."""
    candidates = []
    if cli_arg:
        candidates.append(Path(cli_arg))

    # Standard competition container mount
    candidates.append(Path("/data"))

    # Local workspace candidates
    candidates.append(SCRIPT_DIR / "blast-radius-sample" / "sample")
    candidates.append(SCRIPT_DIR / "sample")
    candidates.append(Path("sample"))

    for c in candidates:
        if (c / "commits").is_dir():
            return c

    print("Error: Could not locate data directory containing 'commits/'", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    t_start = time.perf_counter()
    times_start = os.times()

    data_arg: str | None = None
    bundle_arg: str | None = None
    argv = sys.argv[1:]
    idx = 0
    while idx < len(argv):
        arg = argv[idx]
        if arg == "--data" and idx + 1 < len(argv):
            data_arg = argv[idx + 1]
            idx += 2
        elif arg == "--bundle" and idx + 1 < len(argv):
            bundle_arg = argv[idx + 1]
            idx += 2
        else:
            idx += 1

    data_dir = find_data_dir(data_arg)
    commits_dir = data_dir / "commits"

    bundle_path = Path(bundle_arg) if bundle_arg else (SCRIPT_DIR / "artifacts" / "runtime_index.json")
    if not bundle_path.exists():
        print(f"Error: Runtime index bundle not found at {bundle_path}", file=sys.stderr)
        sys.exit(1)

    # 1. Initialize ImpactResolver and WorkerScheduler
    t0_init = time.perf_counter()
    resolver = ImpactResolver(bundle_path)
    scheduler = WorkerScheduler(resolver.test_durations)
    init_ms = (time.perf_counter() - t0_init) * 1000

    # 2. Collect and sort all patch files
    patch_files = sorted(commits_dir.glob("*.patch"))
    if not patch_files:
        print(f"Warning: No .patch files found in {commits_dir}", file=sys.stderr)

    total_commits = len(patch_files)
    total_tests_selected = 0
    worker_distribution = [0] * 9  # k from 0 to 8

    # 3. Process each commit patch
    for patch_path in patch_files:
        commit_id = patch_path.stem
        try:
            patch_text = patch_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"Warning: Failed reading {patch_path}: {e}", file=sys.stderr)
            patch_text = ""

        # Resolve impact
        selected_tests = resolver.resolve(patch_text)
        total_tests_selected += len(selected_tests)

        # Schedule across optimal k workers
        sched = scheduler.schedule(selected_tests)
        workers = sched["workers"]
        k = sched["k"]
        if 0 <= k <= 8:
            worker_distribution[k] += 1

        # Emit exact JSON line to stdout
        line = json.dumps({"commit": commit_id, "workers": workers}, separators=(",", ":"))
        sys.stdout.write(line + "\n")

    sys.stdout.flush()

    total_elapsed_ms = (time.perf_counter() - t_start) * 1000
    t_end = os.times()
    user_s = t_end.user - times_start.user
    sys_s = t_end.system - times_start.system
    cpu_s = user_s + sys_s

    # 4. Diagnostics exclusively to stderr
    print(
        f"Blast Radius complete: {total_commits} commits processed in {total_elapsed_ms:.1f}ms "
        f"(init: {init_ms:.1f}ms, mean: {total_elapsed_ms / max(1, total_commits):.2f}ms/commit) | "
        f"CPU: {cpu_s * 1000:.1f}ms (user: {user_s * 1000:.1f}ms, sys: {sys_s * 1000:.1f}ms) | "
        f"Selected: {total_tests_selected} tests (avg {total_tests_selected / max(1, total_commits):.1f}/commit)",
        file=sys.stderr,
    )
    k_summary = ", ".join(f"k={i}:{cnt}" for i, cnt in enumerate(worker_distribution) if cnt > 0)
    print(f"Worker distribution: [{k_summary}]", file=sys.stderr)


if __name__ == "__main__":
    main()
