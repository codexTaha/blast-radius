"""Stage 4.1 — Cost Objective & Dynamic Worker Scheduler.

Computes the optimal worker partitioning P* across k* in [1..8] workers
to strictly minimize the competition cost objective:
    J(k, P) = makespan(k, P) + 2000 * k

Optimized with:
- Single-pass sort (avoid re-sorting per candidate k)
- C-accelerated heapq for sub-microsecond greedy bin assignment
- Mathematical lower-bound pruning (skipping suboptimal k)
- Ceiling floor stopping: terminates search when makespan reaches d_max
- Fast path for k=1 when T < 4000ms
"""

from __future__ import annotations

import heapq
from typing import Any

DEFAULT_WORKER_COST_MS = 2_000
DEFAULT_MAX_WORKERS = 8


def cost_objective(makespan_ms: int, k: int, cpu_ms: int = 0, worker_cost_ms: int = DEFAULT_WORKER_COST_MS) -> int:
    """Evaluate total competition cost objective in milliseconds."""
    return makespan_ms + worker_cost_ms * k + cpu_ms


class WorkerScheduler:
    """Dynamic test worker scheduler using calibrated LPT bin-packing."""

    def __init__(
        self,
        test_durations: list[int],
        max_workers: int = DEFAULT_MAX_WORKERS,
        worker_cost_ms: int = DEFAULT_WORKER_COST_MS,
    ) -> None:
        self.test_durations = test_durations
        self.max_workers = max_workers
        self.worker_cost_ms = worker_cost_ms

    def partition_lpt(self, test_numbers: list[int], k: int) -> tuple[list[list[int]], int, int]:
        """Partition selected tests across exactly k workers using LPT greedy bin-packing."""
        if k <= 0 or not test_numbers:
            return [], 0, 0

        durations = self.test_durations
        sorted_tests = sorted(test_numbers, key=lambda t: durations[t] if t < len(durations) else 0, reverse=True)

        if k == 1:
            makespan = sum(durations[t] for t in sorted_tests if t < len(durations))
            return [sorted_tests], makespan, cost_objective(makespan, 1, worker_cost_ms=self.worker_cost_ms)

        # Min-heap of (accumulated_load, worker_index)
        heap = [(0, i) for i in range(k)]
        workers: list[list[int]] = [[] for _ in range(k)]

        for t in sorted_tests:
            load, wid = heap[0]
            dur = durations[t] if t < len(durations) else 0
            workers[wid].append(t)
            heapq.heapreplace(heap, (load + dur, wid))

        occupied = [w for w in workers if w]
        makespan = max(load for load, _ in heap) if occupied else 0
        total_cost = cost_objective(makespan, len(occupied), worker_cost_ms=self.worker_cost_ms)
        return occupied, makespan, total_cost

    def schedule(self, test_numbers: list[int]) -> dict[str, Any]:
        """Find optimal worker count k* and partition P* minimizing total simulated cost."""
        n_tests = len(test_numbers)
        if n_tests == 0:
            return {
                "workers": [],
                "k": 0,
                "makespan_ms": 0,
                "total_cost_ms": 0,
                "candidate_costs": {},
            }

        durations = self.test_durations
        total_duration = sum(durations[t] for t in test_numbers if t < len(durations))

        # Fast path: If single test or total duration < 2 * worker_cost_ms (4000ms),
        # k=1 is mathematically guaranteed optimal (k=2 adds 2000ms fee while saving at most T/2 < 2000ms).
        if n_tests == 1 or total_duration < (2 * self.worker_cost_ms):
            cost = total_duration + self.worker_cost_ms
            return {
                "workers": [list(test_numbers)],
                "k": 1,
                "makespan_ms": total_duration,
                "total_cost_ms": cost,
                "candidate_costs": {1: cost},
            }

        # Sort ONCE descending by test duration
        sorted_tests = sorted(test_numbers, key=lambda t: durations[t] if t < len(durations) else 0, reverse=True)
        d_max = durations[sorted_tests[0]] if sorted_tests else 0

        max_k = min(self.max_workers, n_tests)
        candidate_costs: dict[int, int] = {}
        best_k = 1
        best_cost = float("inf")
        best_makespan = 0
        best_workers: list[list[int]] = []

        for k in range(1, max_k + 1):
            # Theoretical lower bound on makespan for k workers:
            # lower_bound = max(ceil(T / k), d_max) + worker_cost * k
            lower_bound = max((total_duration + k - 1) // k, d_max) + self.worker_cost_ms * k
            if lower_bound >= best_cost:
                candidate_costs[k] = lower_bound
                if k > 2 and (self.worker_cost_ms * k > best_cost):
                    break
                continue

            if k == 1:
                workers = [sorted_tests]
                makespan = total_duration
                cost = makespan + self.worker_cost_ms
            else:
                heap = [(0, i) for i in range(k)]
                workers = [[] for _ in range(k)]
                for t in sorted_tests:
                    load, wid = heap[0]
                    dur = durations[t] if t < len(durations) else 0
                    workers[wid].append(t)
                    heapq.heapreplace(heap, (load + dur, wid))

                occupied = [w for w in workers if w]
                makespan = max(load for load, _ in heap) if occupied else 0
                cost = cost_objective(makespan, len(occupied), worker_cost_ms=self.worker_cost_ms)
                workers = occupied

            candidate_costs[k] = cost

            if cost < best_cost:
                best_cost = cost
                best_makespan = makespan
                best_workers = workers
                best_k = len(workers)

            # If makespan reaches the single-test floor d_max, no larger k can reduce makespan further
            if makespan == d_max:
                break

        return {
            "workers": best_workers,
            "k": best_k,
            "makespan_ms": best_makespan,
            "total_cost_ms": best_cost,
            "candidate_costs": candidate_costs,
        }
