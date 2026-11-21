#!/usr/bin/env python3
"""Stage 4.2 — LPT Bin-Packing Calibration & Simulated Evaluation.

Runs end-to-end impact resolution and worker scheduling across all 30 sample commits.
Generates compliant competition JSONL output, scores it with score.py evaluate_run,
and benchmarks Dynamic k-Worker vs Static 3-Worker and Baseline Starter.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

# Ensure project root and blast-radius-sample are in sys.path
root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))
sys.path.insert(0, str(root_dir / "blast-radius-sample"))

from score import evaluate_run  # type: ignore[import-not-found]
from src.impact_resolver import ImpactResolver
from src.worker_scheduler import WorkerScheduler


def run_evaluation(
    bundle_path: Path,
    sample_dir: Path,
) -> dict[str, Any]:
    tests_json_path = sample_dir / "tests.json"
    answers_json_path = sample_dir / "answers.json"
    commits_dir = sample_dir / "commits"

    tests_data = json.loads(tests_json_path.read_text(encoding="utf-8"))["tests"]
    answers_data = json.loads(answers_json_path.read_text(encoding="utf-8"))["commits"]

    resolver = ImpactResolver(bundle_path)
    scheduler = WorkerScheduler(resolver.test_durations)

    jsonl_lines_dynamic: list[str] = []
    jsonl_lines_static3: list[str] = []

    commit_details: list[dict[str, Any]] = []

    total_tests_selected = 0
    total_dyn_worker_cost_ms = 0
    total_s3_worker_cost_ms = 0

    t_start = time.perf_counter()

    for cid in sorted(answers_data.keys()):
        patch_path = commits_dir / f"{cid}.patch"
        patch_text = patch_path.read_text(encoding="utf-8")

        # 1. Resolve impact
        selected_tests = resolver.resolve(patch_text)
        total_tests_selected += len(selected_tests)

        # 2. Schedule with Dynamic k*
        sched_dyn = scheduler.schedule(selected_tests)
        workers_dyn = sched_dyn["workers"]
        k_dyn = sched_dyn["k"]
        dyn_makespan = sched_dyn["makespan_ms"]
        dyn_cost = sched_dyn["total_cost_ms"]
        total_dyn_worker_cost_ms += 2000 * k_dyn

        # 3. Schedule with Static 3 workers (for comparison)
        k_s3 = min(3, len(selected_tests))
        workers_s3, s3_makespan, s3_cost = scheduler.partition_lpt(selected_tests, k_s3)
        total_s3_worker_cost_ms += 2000 * k_s3

        # Form JSONL lines
        line_dyn = json.dumps({"commit": cid, "workers": workers_dyn}, separators=(",", ":"))
        line_s3 = json.dumps({"commit": cid, "workers": workers_s3}, separators=(",", ":"))
        jsonl_lines_dynamic.append(line_dyn)
        jsonl_lines_static3.append(line_s3)

        savings_ms = s3_cost - dyn_cost
        commit_details.append({
            "commit": cid,
            "kind": answers_data[cid]["kind"],
            "failing_count": len(answers_data[cid]["failing"]),
            "selected_count": len(selected_tests),
            "static3_k": k_s3,
            "static3_makespan_s": round(s3_makespan / 1000, 3),
            "static3_cost_s": round(s3_cost / 1000, 3),
            "dynamic_k": k_dyn,
            "dynamic_makespan_s": round(dyn_makespan / 1000, 3),
            "dynamic_cost_s": round(dyn_cost / 1000, 3),
            "savings_s": round(savings_ms / 1000, 3),
        })

    resolution_time_s = time.perf_counter() - t_start

    # Evaluate with official evaluator score.py
    bytes_dyn = ("\n".join(jsonl_lines_dynamic) + "\n").encode("utf-8")
    bytes_s3 = ("\n".join(jsonl_lines_static3) + "\n").encode("utf-8")

    eval_dyn = evaluate_run(bytes_dyn, tests_data, answers_data)
    eval_s3 = evaluate_run(bytes_s3, tests_data, answers_data)

    if not eval_dyn["ok"]:
        print(f"Error: evaluate_run rejected dynamic output: {eval_dyn}", file=sys.stderr)
        sys.exit(1)
    if not eval_s3["ok"]:
        print(f"Error: evaluate_run rejected static3 output: {eval_s3}", file=sys.stderr)
        sys.exit(1)

    baseline_starter_ms = 1_210_470  # Phase 1 benchmark score
    dyn_score_ms = eval_dyn["simulated_milliseconds"]
    s3_score_ms = eval_s3["simulated_milliseconds"]

    reduction_vs_baseline = (1.0 - (dyn_score_ms / baseline_starter_ms)) * 100
    reduction_vs_static3 = (1.0 - (dyn_score_ms / s3_score_ms)) * 100

    report = {
        "summary": {
            "total_commits": len(answers_data),
            "total_tests_selected": total_tests_selected,
            "mean_tests_selected": round(total_tests_selected / len(answers_data), 1),
            "baseline_starter_s": round(baseline_starter_ms / 1000, 3),
            "static3_score_s": round(s3_score_ms / 1000, 3),
            "dynamic_score_s": round(dyn_score_ms / 1000, 3),
            "reduction_vs_baseline_pct": round(reduction_vs_baseline, 2),
            "reduction_vs_static3_pct": round(reduction_vs_static3, 2),
            "dyn_misses": len(eval_dyn["missed_commits"]),
            "s3_misses": len(eval_s3["missed_commits"]),
            "jsonl_bytes": len(bytes_dyn),
            "total_cpu_time_s": round(resolution_time_s, 4),
            "avg_cpu_time_ms_per_commit": round((resolution_time_s / len(answers_data)) * 1000, 2),
        },
        "commits": commit_details,
    }
    return report


def main() -> None:
    bundle_path = Path("artifacts/runtime_index.json")
    sample_dir = Path("blast-radius-sample/sample")
    out_json = Path("artifacts/scheduler_evaluation.json")

    print("Running Stage 4.2 end-to-end evaluation ...", file=sys.stderr)
    report = run_evaluation(bundle_path, sample_dir)

    out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    s = report["summary"]

    print("\n=================== Stage 4.2 Evaluation Results ===================", file=sys.stderr)
    print(f"Total Commits Evaluated:      {s['total_commits']}", file=sys.stderr)
    print(f"Total Tests Selected:         {s['total_tests_selected']} (mean {s['mean_tests_selected']}/commit)", file=sys.stderr)
    print(f"Missed Commits (Penalties):   {s['dyn_misses']} (0.0s penalty)", file=sys.stderr)
    print(f"JSONL Payload Size:           {s['jsonl_bytes']:,} bytes (limit 2,000,000 bytes)", file=sys.stderr)
    print(f"Batch Execution CPU Time:     {s['total_cpu_time_s']:.4f} s ({s['avg_cpu_time_ms_per_commit']:.2f} ms/commit)", file=sys.stderr)
    print(f"-------------------------------------------------------------------", file=sys.stderr)
    print(f"1. Starter Baseline Score:    {s['baseline_starter_s']:.3f} s", file=sys.stderr)
    print(f"2. Impact Model + Static 3:   {s['static3_score_s']:.3f} s (saved {s['baseline_starter_s'] - s['static3_score_s']:.3f} s)", file=sys.stderr)
    print(f"3. Impact Model + Dynamic k*: {s['dynamic_score_s']:.3f} s (saved {s['baseline_starter_s'] - s['dynamic_score_s']:.3f} s)", file=sys.stderr)
    print(f"-------------------------------------------------------------------", file=sys.stderr)
    print(f"Margin vs Baseline Starter:   {s['reduction_vs_baseline_pct']:.1f}% reduction", file=sys.stderr)
    print(f"Margin vs Static 3 Workers:   {s['reduction_vs_static3_pct']:.1f}% reduction", file=sys.stderr)
    print(f"===================================================================\n", file=sys.stderr)

    if s["dyn_misses"] > 0:
        print("FAIL: One or more commits missed failing tests!", file=sys.stderr)
        sys.exit(1)
    if s["reduction_vs_baseline_pct"] < 50.0:
        print("FAIL: Score reduction did not meet target threshold!", file=sys.stderr)
        sys.exit(1)
    print("PASS: Dynamic worker scheduler successfully validated under official scoring.\n", file=sys.stderr)


if __name__ == "__main__":
    main()
