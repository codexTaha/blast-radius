#!/usr/bin/env python3
"""Score decomposition and makespan analysis across all 30 sample commits."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.diff_parser import parse_patch
from src.impact_resolver import ImpactResolver
from src.worker_scheduler import WorkerScheduler


def main() -> None:
    root_dir = Path(__file__).resolve().parent.parent
    sample_dir = root_dir / "blast-radius-sample" / "sample"
    bundle = json.loads((root_dir / "artifacts" / "runtime_index.json").read_text(encoding="utf-8"))
    answers = json.loads((sample_dir / "answers.json").read_text(encoding="utf-8"))["commits"]
    tests_data = json.loads((sample_dir / "tests.json").read_text(encoding="utf-8"))["tests"]

    resolver = ImpactResolver(bundle)
    scheduler = WorkerScheduler(resolver.test_durations)

    total_sim_cost = 0.0
    total_worker_overhead = 0.0
    total_makespan = 0.0

    commit_data = []

    for cid, info in sorted(answers.items()):
        patch = (sample_dir / "commits" / f"{cid}.patch").read_text(encoding="utf-8")
        meta = resolver.resolve_with_metadata(patch)
        selected = meta["selected_tests"]
        sched = scheduler.schedule(selected)

        k = sched["k"]
        ms_ms = sched["makespan_ms"]
        cost_ms = sched["total_cost_ms"]
        workers = sched["workers"]

        ms_s = ms_ms / 1000.0
        cost_s = cost_ms / 1000.0
        worker_cost_s = (2000 * k) / 1000.0

        total_sim_cost += cost_s
        total_makespan += ms_s
        total_worker_overhead += worker_cost_s

        # Find bottleneck worker and its slowest test
        bottleneck_test = (None, 0.0, "none")
        worker_loads = []
        if workers:
            for w in workers:
                w_dur = sum(resolver.test_durations[t] for t in w)
                worker_loads.append(w_dur / 1000.0)

            max_w_idx = max(range(len(workers)), key=lambda i: worker_loads[i])
            max_w = workers[max_w_idx]
            if max_w:
                slowest_t = max(max_w, key=lambda t: resolver.test_durations[t])
                bottleneck_test = (slowest_t, resolver.test_durations[slowest_t] / 1000.0, tests_data[slowest_t]["id"])

        commit_data.append({
            "cid": cid,
            "kind": info["kind"],
            "failing": len(info["failing"]),
            "selected": len(selected),
            "k": k,
            "makespan_s": ms_s,
            "worker_cost_s": worker_cost_s,
            "total_cost_s": cost_s,
            "bottleneck_test": bottleneck_test,
            "worker_loads": worker_loads,
            "has_1106": 1106 in selected,
            "has_1490": 1490 in selected,
        })

    commit_data.sort(key=lambda x: x["total_cost_s"], reverse=True)

    header = "%-6s %-9s %-5s %-5s %-3s %-10s %-11s %-10s %-35s" % (
        "CID", "Kind", "Fail", "Sel", "k", "Makespan", "WorkerCost", "TotalCost", "Bottleneck Test (ID, Dur, Name)"
    )
    print("=" * 105)
    print(header)
    print("=" * 105)

    for c in commit_data:
        bt = c["bottleneck_test"]
        bt_str = f"T{bt[0]} ({bt[1]:.2f}s) {bt[2].split('::')[-1]}" if bt[0] is not None else "None"
        line = "%-6s %-9s %-5d %-5d %-3d %-10.3f %-11.3f %-10.3f %-35s" % (
            c["cid"], c["kind"], c["failing"], c["selected"], c["k"],
            c["makespan_s"], c["worker_cost_s"], c["total_cost_s"], bt_str[:35]
        )
        print(line)

    print("=" * 105)
    print(f"Total Simulated Score: {total_sim_cost:.3f} s")
    print(f"  - Pure Makespan:     {total_makespan:.3f} s ({total_makespan / total_sim_cost * 100:.1f}%)")
    print(f"  - Worker Overhead:   {total_worker_overhead:.3f} s ({total_worker_overhead / total_sim_cost * 100:.1f}%)")
    print("=" * 105)

    # Detailed inspection of tests 1106 and 1490
    print("\n--- OCCURRENCES OF TEST 1106 (test_execute_tpcds, 21.454s) ---")
    commits_with_1106 = [c for c in commit_data if c["has_1106"]]
    print(f"Selected in {len(commits_with_1106)} commits:")
    for c in commits_with_1106:
        print(f"  Commit {c['cid']}: kind={c['kind']}, fail_count={c['failing']}, k={c['k']}, cost={c['total_cost_s']:.3f}s, worker_loads={c['worker_loads']}")

    print("\n--- OCCURRENCES OF TEST 1490 (test_max_nodes, 28.817s) ---")
    commits_with_1490 = [c for c in commit_data if c["has_1490"]]
    print(f"Selected in {len(commits_with_1490)} commits:")
    for c in commits_with_1490:
        print(f"  Commit {c['cid']}: kind={c['kind']}, fail_count={c['failing']}, k={c['k']}, cost={c['total_cost_s']:.3f}s, worker_loads={c['worker_loads']}")


if __name__ == "__main__":
    main()
