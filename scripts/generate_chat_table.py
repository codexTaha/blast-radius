#!/usr/bin/env python3
"""Format and print the unified master combined table joining all audit CSVs."""

import csv
from pathlib import Path
from collections import defaultdict

audit_dir = Path("reports/timeline_audit")
c_rows = list(csv.DictReader(open(audit_dir / "commit_timeline.csv")))
w_rows = list(csv.DictReader(open(audit_dir / "worker_timeline.csv")))
g_rows = list(csv.DictReader(open(audit_dir / "global_timeline.csv")))
t_rows = list(csv.DictReader(open(audit_dir / "test_costs.csv")))

cw_map = defaultdict(lambda: defaultdict(list))
for w in w_rows:
    cw_map[w["commit_id"]][w["worker_index"]].append(w)

total_sim_s = sum(float(r["score_contribution_s"]) for r in c_rows)

print("| Stage / Commit | Category | Tests Sel | $k$ | Makespan | Overhead Fee | Total Cost | % of Total | Worker Assignment | Worker Tests | Worker Load | Timeline Range | Dominant / Bottleneck Test | Test Dur |")
print("| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :--- | :---: | :---: | :---: | :--- | :---: |")

# 1. Global stages
for g in g_rows:
    dur_s = float(g["duration_ms"]) / 1000.0
    start_s = float(g["start_time_ms"]) / 1000.0
    end_s = float(g["end_time_ms"]) / 1000.0
    print(f"| **Stage {g['stage_number']}: {g['stage_name']}** | `{g['category']}` | - | - | - | - | **{dur_s:.4f}s** | - | All (Host Main) | - | {dur_s:.4f}s | {start_s:.3f}s -> {end_s:.3f}s | {g['details'][:35]} | {dur_s:.3f}s |")

# Separator
print("| **---** | **---** | **---** | **---** | **---** | **---** | **---** | **---** | **---** | **---** | **---** | **---** | **---** | **---** |")

# 2. Commits sorted by cost
c_sorted = sorted(c_rows, key=lambda x: float(x["total_simulated_cost_ms"]), reverse=True)

for c in c_sorted:
    cid = c["commit_id"]
    workers = cw_map[cid]
    k = int(c["workers_allocated"])
    pct = (float(c["total_simulated_cost_ms"]) / (total_sim_s * 1000.0)) * 100.0
    kind = c["kind"]
    sel = c["tests_selected"]
    ms = float(c["makespan_ms"]) / 1000.0
    fee = float(c["worker_fee_ms"]) / 1000.0
    cost = float(c["total_simulated_cost_ms"]) / 1000.0

    if k == 0:
        print(f"| **Commit `{cid}`** | `{kind}` | 0 | 0 | 0.000s | 0.0s | **0.000s** | **0.00%** | *None (0 workers)* | 0 tests | 0.000s | 0.000s -> 0.000s | *No tests required (comment-only)* | 0.000s |")
    else:
        for w_idx in sorted(workers.keys(), key=lambda x: int(x)):
            w_tests = workers[w_idx]
            w_dur_s = sum(int(t["duration_ms"]) for t in w_tests) / 1000.0
            w_start_s = w_tests[0]["start_offset_s"]
            w_end_s = w_tests[-1]["end_offset_s"]
            is_longest = (int(w_idx) == int(c["longest_worker_id"]))
            w_tag = f"**Worker {w_idx}** (bottleneck)" if is_longest else f"Worker {w_idx}"

            slowest_on_w = max(w_tests, key=lambda t: int(t["duration_ms"]))
            t_num = slowest_on_w["test_number"]
            t_name = slowest_on_w["test_name"]
            t_dur_s = float(slowest_on_w["duration_ms"]) / 1000.0
            fail_badge = " [FAIL]" if slowest_on_w["is_failing_test"] == "True" else ""

            print(f"| **Commit `{cid}`** | `{kind}` | {sel} | {k} | {ms:.3f}s | {fee:.1f}s | **{cost:.3f}s** | **{pct:.2f}%** | {w_tag} | {len(w_tests)} tests | {w_dur_s:.3f}s | {w_start_s}s -> {w_end_s}s | `T{t_num}` {t_name}{fail_badge} | {t_dur_s:.3f}s |")
