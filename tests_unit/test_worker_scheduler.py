#!/usr/bin/env python3
"""Unit tests for src.worker_scheduler."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.worker_scheduler import WorkerScheduler, cost_objective


class TestWorkerScheduler(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        bundle = json.loads(Path("artifacts/runtime_index.json").read_text(encoding="utf-8"))
        cls.durations = bundle["test_durations"]
        cls.scheduler = WorkerScheduler(cls.durations)

    def test_cost_objective_formula(self) -> None:
        self.assertEqual(cost_objective(1000, 1), 3000)
        self.assertEqual(cost_objective(5000, 3), 11000)
        self.assertEqual(cost_objective(0, 0), 0)
        self.assertEqual(cost_objective(2000, 2, cpu_ms=50), 6050)

    def test_empty_test_selection(self) -> None:
        res = self.scheduler.schedule([])
        self.assertEqual(res["k"], 0)
        self.assertEqual(res["workers"], [])
        self.assertEqual(res["makespan_ms"], 0)
        self.assertEqual(res["total_cost_ms"], 0)

    def test_single_test_selection(self) -> None:
        res = self.scheduler.schedule([42])
        self.assertEqual(res["k"], 1)
        self.assertEqual(res["workers"], [[42]])
        self.assertEqual(res["makespan_ms"], self.durations[42])
        self.assertEqual(res["total_cost_ms"], self.durations[42] + 2000)

    def test_small_duration_selection_chooses_k1(self) -> None:
        # Pick 5 fast tests totaling < 2000ms
        fast_tests = [0, 1, 2, 3, 4]
        total_d = sum(self.durations[t] for t in fast_tests)
        self.assertLess(total_d, 2000)

        res = self.scheduler.schedule(fast_tests)
        self.assertEqual(res["k"], 1)
        self.assertEqual(len(res["workers"]), 1)
        self.assertEqual(sorted(res["workers"][0]), sorted(fast_tests))

    def test_partition_integrity_and_bounds(self) -> None:
        # Test across various test subset sizes
        test_subsets = [
            list(range(10)),
            list(range(50, 150)),
            list(range(0, 500, 5)),
            list(range(1620)),
        ]

        for subset in test_subsets:
            res = self.scheduler.schedule(subset)
            workers = res["workers"]
            k = res["k"]

            self.assertLessEqual(k, 8)
            self.assertEqual(len(workers), k)

            flattened = [t for w in workers for t in w]
            self.assertEqual(len(flattened), len(subset), f"Subset size {len(subset)} lost tests")
            self.assertEqual(set(flattened), set(subset), f"Subset size {len(subset)} changed tests")

            # Check makespan calculation accuracy
            loads = [sum(self.durations[t] for t in w) for w in workers]
            self.assertEqual(max(loads), res["makespan_ms"])
            self.assertEqual(res["total_cost_ms"], res["makespan_ms"] + 2000 * k)

    def test_large_duration_scales_workers(self) -> None:
        # Full test suite is ~103s, optimal k should be between 3 and 6
        full_suite = list(range(len(self.durations)))
        res = self.scheduler.schedule(full_suite)
        self.assertGreaterEqual(res["k"], 3)
        self.assertLessEqual(res["k"], 6)

        # Dynamic k should strictly beat static k=1 and static k=8
        _, _, cost_k1 = self.scheduler.partition_lpt(full_suite, 1)
        _, _, cost_k8 = self.scheduler.partition_lpt(full_suite, 8)
        self.assertLess(res["total_cost_ms"], cost_k1)
        self.assertLess(res["total_cost_ms"], cost_k8)

    def test_exact_k2_bitset_dp_beats_greedy(self) -> None:
        # Classic 5-item partition where greedy LPT gives makespan 30000 at k=2 while exact k=2 DP hits 25000.
        sched = WorkerScheduler([15000, 10000, 10000, 10000, 5000], max_workers=2)
        res = sched.schedule([0, 1, 2, 3, 4])
        self.assertEqual(res["k"], 2)
        self.assertEqual(res["makespan_ms"], 25000)
        self.assertEqual(res["total_cost_ms"], 29000)

    def test_single_heavy_item_prefers_k1_or_optimal_k(self) -> None:
        sched = WorkerScheduler([29000, 100, 100, 100])
        res = sched.schedule([0, 1, 2, 3])
        self.assertEqual(res["k"], 1)
        self.assertEqual(res["makespan_ms"], 29300)

    def test_multifit_and_local_search_balance_hard_instance(self) -> None:
        durs = [5000, 4800, 4600, 4400, 4200, 4000, 3800, 3600, 3400, 3200]
        sched = WorkerScheduler(durs)
        res = sched.schedule(list(range(len(durs))))
        _, makespan_lpt, _ = sched.partition_lpt(list(range(len(durs))), res["k"])
        self.assertLessEqual(res["makespan_ms"], makespan_lpt)

    def test_determinism_independent_of_input_order(self) -> None:
        ids = [10, 42, 100, 250, 700, 1106, 1490]
        r1 = self.scheduler.schedule(ids)
        r2 = self.scheduler.schedule(list(reversed(ids)))
        self.assertEqual(r1, r2)

    def test_filters_out_of_bounds_bool_and_duplicate_tests(self) -> None:
        res = self.scheduler.schedule([5, 5, -1, -8, 99999, 10])
        flat = sorted(t for w in res["workers"] for t in w)
        self.assertEqual(flat, [5, 10])

    def test_empty_selection_returns_k0_cost0(self) -> None:
        res = self.scheduler.schedule([])
        self.assertEqual(res["workers"], [])
        self.assertEqual(res["k"], 0)
        self.assertEqual(res["makespan_ms"], 0)
        self.assertEqual(res["total_cost_ms"], 0)

    def test_roi_bounded_branch_and_bound_non_regression(self) -> None:
        durs = [6100, 5900, 5700, 4300, 4100, 3900, 2500, 2400, 2100]
        sched = WorkerScheduler(durs)
        res = sched.schedule(list(range(len(durs))))
        loads = [sum(durs[t] for t in w) for w in res["workers"]]
        self.assertEqual(max(loads), res["makespan_ms"])
        self.assertLessEqual(len(res["workers"]), 8)


if __name__ == "__main__":
    unittest.main()
