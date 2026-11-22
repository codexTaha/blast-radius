"""Stage 4.1 — Cost Objective & Dynamic Worker Scheduler.

Computes the optimal worker partitioning P* across k* in [1..8] workers
to strictly minimize the competition cost objective:
    J(k, P) = makespan(k, P) + 2000 * k

Optimized with:
- Deterministic ID-tie-broken single-pass sort
- Fast heapless LPT path for k=2 and C-indexed heapq for k>=3
- Mathematical lower-bound pruning & ceiling floor stopping (makespan == d_max)
- Zero-duration test separation (positive-duration bin packing only)
- Gated deterministic post-LPT refinement (exact k=2 bitset DP, MultiFit,
  1-move/1-swap local search, and ROI-guarded bounded branch-and-bound)
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
    """Dynamic test worker scheduler using calibrated LPT bin-packing and deterministic refinement."""

    def __init__(
        self,
        test_durations: list[int],
        max_workers: int = DEFAULT_MAX_WORKERS,
        worker_cost_ms: int = DEFAULT_WORKER_COST_MS,
    ) -> None:
        self.test_durations = test_durations
        self.max_workers = max_workers
        self.worker_cost_ms = worker_cost_ms

    @staticmethod
    def _exact_k2_dp(
        nz_tests: list[int],
        durations: list[int],
        total_duration: int,
        target_lt: int,
    ) -> tuple[list[list[int]], int] | None:
        """Exact 2-way makespan minimization via Python int bitset DP."""
        m = len(nz_tests)
        if m < 2 or m > 128:
            return None
        d0 = durations[nz_tests[0]]
        hi = target_lt - 1 - d0
        if hi < 0 or hi > 16_384:
            return None
        lo = total_duration - (target_lt - 1) - d0
        if lo > hi:
            return None
        if lo < 0:
            lo = 0

        mask_all = (1 << (hi + 1)) - 1
        dp = 1
        hist: list[tuple[int, int, int]] = []
        for idx in range(1, m):
            v = durations[nz_tests[idx]]
            if v <= hi:
                hist.append((idx, v, dp))
                dp = (dp | (dp << v)) & mask_all

        valid = dp & (mask_all ^ ((1 << lo) - 1)) if lo > 0 else dp
        if not valid:
            return None

        ideal_r = (total_duration // 2) - d0
        best_r: int | None = None
        best_ms = target_lt

        if ideal_r >= lo:
            below = valid & ((1 << (min(ideal_r, hi) + 1)) - 1)
            if below:
                r = below.bit_length() - 1
                ms = max(d0 + r, total_duration - d0 - r)
                if ms < best_ms:
                    best_ms = ms
                    best_r = r

        above_start = max(lo, ideal_r + 1)
        if above_start <= hi:
            above = valid >> above_start
            if above:
                lsb = above & -above
                r = above_start + (lsb.bit_length() - 1)
                ms = max(d0 + r, total_duration - d0 - r)
                if ms < best_ms:
                    best_ms = ms
                    best_r = r

        if best_r is None:
            return None

        in_b0 = {0}
        rem = best_r
        for idx, v, prev_dp in reversed(hist):
            if rem >= v and ((prev_dp >> (rem - v)) & 1):
                in_b0.add(idx)
                rem -= v

        b0: list[int] = []
        b1: list[int] = []
        for idx, t in enumerate(nz_tests):
            if idx in in_b0:
                b0.append(t)
            else:
                b1.append(t)
        if not b1:
            return None
        return [b0, b1], best_ms

    @staticmethod
    def _multifit_k(
        nz_tests: list[int],
        durations: list[int],
        k: int,
        lb_ms: int,
        target_lt: int,
    ) -> tuple[list[list[int]], int] | None:
        """Deterministic MultiFit (First-Fit Decreasing binary search) for k >= 3."""
        lo = lb_ms
        hi = target_lt - 1
        if lo > hi:
            return None
        best_bins: list[list[int]] | None = None
        best_ms = target_lt
        nz_durs = [durations[t] for t in nz_tests]
        for _ in range(12):
            if lo > hi:
                break
            mid = (lo + hi) // 2
            loads = [0] * k
            bins: list[list[int]] = [[] for _ in range(k)]
            ok = True
            for t, dt in zip(nz_tests, nz_durs):
                placed = False
                for b in range(k):
                    if loads[b] + dt <= mid:
                        loads[b] += dt
                        bins[b].append(t)
                        placed = True
                        break
                if not placed:
                    ok = False
                    break
            if ok:
                actual_ms = max(loads)
                if actual_ms < best_ms:
                    best_ms = actual_ms
                    best_bins = [w for w in bins if w]
                hi = actual_ms - 1
            else:
                lo = mid + 1
        if best_bins is not None:
            return best_bins, best_ms
        return None

    @staticmethod
    def _refine_move_1swap(
        workers: list[list[int]],
        durations: list[int],
        lb_ms: int,
        max_iters: int = 32,
    ) -> tuple[list[list[int]], int]:
        """Deterministic post-LPT 1-move and 1-swap local search preserving non-empty workers."""
        bins = [list(w) for w in workers]
        n_dur = len(durations)
        loads = [sum(durations[t] if 0 <= t < n_dur else 0 for t in w) for w in bins]
        cur_max = max(loads)
        if cur_max <= lb_ms or len(bins) < 2:
            return bins, cur_max

        k = len(bins)
        for _ in range(max_iters):
            if cur_max <= lb_ms:
                break
            max_w = 0
            max_l = loads[0]
            for i in range(1, k):
                if loads[i] > max_l:
                    max_l = loads[i]
                    max_w = i

            others = sorted((i for i in range(k) if i != max_w), key=lambda i: (loads[i], i))
            moved = False

            if len(bins[max_w]) > 1:
                best_move: tuple[int, int, int, int] | None = None
                bw = bins[max_w]
                for idx, t in enumerate(bw):
                    dt = durations[t] if 0 <= t < n_dur else 0
                    if dt <= 0:
                        continue
                    new_mw = max_l - dt
                    for tw in others:
                        new_tw = loads[tw] + dt
                        if new_tw >= max_l:
                            break
                        pair_max = new_mw if new_mw >= new_tw else new_tw
                        cand = (pair_max, new_tw, tw, idx)
                        if best_move is None or cand < best_move:
                            best_move = cand
                if best_move is not None:
                    _, _, tw, idx = best_move
                    t = bins[max_w].pop(idx)
                    bins[tw].append(t)
                    dt = durations[t] if 0 <= t < n_dur else 0
                    loads[max_w] -= dt
                    loads[tw] += dt
                    cur_max = max(loads)
                    moved = True
            if moved:
                continue

            best_swap: tuple[int, int, int, int] | None = None
            bw = bins[max_w]
            for tw in others:
                bt = bins[tw]
                lt = loads[tw]
                slack = max_l - lt
                if slack <= 1:
                    continue
                for i_idx, ti in enumerate(bw):
                    di = durations[ti] if 0 <= ti < n_dur else 0
                    for j_idx, tj in enumerate(bt):
                        dj = durations[tj] if 0 <= tj < n_dur else 0
                        diff = di - dj
                        if 0 < diff < slack:
                            new_mw = max_l - diff
                            new_tw = lt + diff
                            pair_max = new_mw if new_mw >= new_tw else new_tw
                            cand = (pair_max, tw, i_idx, j_idx)
                            if best_swap is None or cand < best_swap:
                                best_swap = cand
            if best_swap is not None:
                _, tw, i_idx, j_idx = best_swap
                ti = bins[max_w][i_idx]
                tj = bins[tw][j_idx]
                bins[max_w][i_idx] = tj
                bins[tw][j_idx] = ti
                di = durations[ti] if 0 <= ti < n_dur else 0
                dj = durations[tj] if 0 <= tj < n_dur else 0
                diff = di - dj
                loads[max_w] -= diff
                loads[tw] += diff
                cur_max = max(loads)
            else:
                break
        return bins, cur_max

    @staticmethod
    def _exact_k_bnb(
        nz_tests: list[int],
        durations: list[int],
        k: int,
        lb_ms: int,
        target_lt: int,
        max_nodes: int = 600,
    ) -> tuple[list[list[int]], int] | None:
        """Bounded exact branch-and-bound for small-to-medium non-zero sets (m <= 32)."""
        m = len(nz_tests)
        if m <= k or m > 32:
            return None
        nz_d = [durations[t] for t in nz_tests]
        loads = [0] * k
        assign = [0] * m
        best_ms = target_lt
        best_assign: list[int] | None = None
        suffix = [0] * (m + 1)
        for i in range(m - 1, -1, -1):
            suffix[i] = suffix[i + 1] + nz_d[i]
        nodes = 0
        aborted = False

        def dfs(idx: int, cur_max: int) -> None:
            nonlocal best_ms, best_assign, nodes, aborted
            nodes += 1
            if nodes > max_nodes:
                aborted = True
                return
            if cur_max >= best_ms:
                return
            if idx == m:
                best_ms = cur_max
                best_assign = assign[:]
                return
            cap = best_ms - 1
            slack = 0
            for b in range(k):
                slack += cap - loads[b]
            if slack < suffix[idx]:
                return
            v = nz_d[idx]
            seen: set[int] = set()
            for b in range(k):
                l = loads[b]
                if l in seen:
                    continue
                seen.add(l)
                nl = l + v
                if nl < best_ms:
                    loads[b] = nl
                    assign[idx] = b
                    dfs(idx + 1, nl if nl > cur_max else cur_max)
                    loads[b] = l
                    if aborted or best_ms == lb_ms:
                        return

        loads[0] = nz_d[0]
        assign[0] = 0
        dfs(1, nz_d[0])
        if best_assign is None:
            return None
        bins: list[list[int]] = [[] for _ in range(k)]
        for i, b in enumerate(best_assign):
            bins[b].append(nz_tests[i])
        occupied = [w for w in bins if w]
        return occupied, best_ms

    def _refine_partition(
        self,
        nz_tests: list[int],
        k: int,
        lpt_workers: list[list[int]],
        lpt_makespan: int,
        total_duration: int,
        d_max: int,
        lb_ms: int,
        target_lt: int,
    ) -> tuple[list[list[int]], int] | None:
        """Deterministically refine an LPT partition targeting makespan < target_lt."""
        durations = self.test_durations
        if k == 2:
            dp_res = self._exact_k2_dp(nz_tests, durations, total_duration, target_lt)
            if dp_res is not None:
                return dp_res

        cur_bins = lpt_workers
        cur_ms = min(lpt_makespan, target_lt)
        mf = self._multifit_k(nz_tests, durations, k, lb_ms, cur_ms)
        if mf is not None and mf[1] < cur_ms:
            cur_bins, cur_ms = mf

        if cur_ms > lb_ms:
            rb, rms = self._refine_move_1swap(cur_bins, durations, lb_ms)
            if rms < cur_ms:
                cur_bins, cur_ms = rb, rms

        if cur_ms > lb_ms and len(nz_tests) <= 32:
            potential = cur_ms - lb_ms
            if potential >= 3 or len(nz_tests) <= 24:
                bb = self._exact_k_bnb(nz_tests, durations, k, lb_ms, cur_ms)
                if bb is not None and bb[1] < cur_ms:
                    cur_bins, cur_ms = bb

        if cur_ms < target_lt:
            return cur_bins, cur_ms
        return None

    def partition_lpt(self, test_numbers: list[int], k: int) -> tuple[list[list[int]], int, int]:
        """Partition valid selected tests across up to k non-empty workers using deterministic LPT."""
        durations = self.test_durations
        n_dur = len(durations)
        valid_tests = [
            t for t in test_numbers if isinstance(t, int) and not isinstance(t, bool) and 0 <= t < n_dur
        ]
        if k <= 0 or not valid_tests:
            return [], 0, 0

        asc_tests = sorted(valid_tests)
        get_dur = durations.__getitem__
        sorted_tests = sorted(asc_tests, key=get_dur, reverse=True)
        sorted_durs = list(map(get_dur, sorted_tests))

        if k == 1:
            makespan = sum(sorted_durs)
            return [sorted_tests], makespan, cost_objective(makespan, 1, worker_cost_ms=self.worker_cost_ms)

        if k == 2:
            l0 = l1 = 0
            w0: list[int] = []
            w1: list[int] = []
            for t, dur in zip(sorted_tests, sorted_durs):
                if l0 <= l1:
                    w0.append(t)
                    l0 += dur
                else:
                    w1.append(t)
                    l1 += dur
            occupied = [w for w in (w0, w1) if w]
            makespan = l0 if l0 >= l1 else l1
            total_cost = cost_objective(makespan, len(occupied), worker_cost_ms=self.worker_cost_ms)
            return occupied, makespan, total_cost

        heap = [(0, i) for i in range(k)]
        workers: list[list[int]] = [[] for _ in range(k)]
        for t, dur in zip(sorted_tests, sorted_durs):
            load, wid = heap[0]
            workers[wid].append(t)
            heapq.heapreplace(heap, (load + dur, wid))

        occupied = [w for w in workers if w]
        makespan = max(load for load, _ in heap) if occupied else 0
        total_cost = cost_objective(makespan, len(occupied), worker_cost_ms=self.worker_cost_ms)
        return occupied, makespan, total_cost

    def schedule(self, test_numbers: list[int]) -> dict[str, Any]:
        """Find optimal worker count k* and partition P* minimizing total simulated cost."""
        durations = self.test_durations
        n_dur = len(durations)
        asc_tests = sorted({
            t for t in test_numbers if isinstance(t, int) and not isinstance(t, bool) and 0 <= t < n_dur
        })
        n_tests = len(asc_tests)
        if n_tests == 0 or self.max_workers <= 0:
            return {
                "workers": [],
                "k": 0,
                "makespan_ms": 0,
                "total_cost_ms": 0,
                "candidate_costs": {},
            }

        if n_tests == 1:
            t0 = asc_tests[0]
            d0 = durations[t0]
            cost = d0 + self.worker_cost_ms
            return {
                "workers": [[t0]],
                "k": 1,
                "makespan_ms": d0,
                "total_cost_ms": cost,
                "candidate_costs": {1: cost},
            }

        get_dur = durations.__getitem__
        total_duration = sum(map(get_dur, asc_tests))
        if total_duration < (2 * self.worker_cost_ms) or total_duration == 0:
            cost = total_duration + self.worker_cost_ms
            return {
                "workers": [asc_tests],
                "k": 1,
                "makespan_ms": total_duration,
                "total_cost_ms": cost,
                "candidate_costs": {1: cost},
            }

        sorted_tests = sorted(asc_tests, key=get_dur, reverse=True)
        sorted_durs = list(map(get_dur, sorted_tests))
        d_max = sorted_durs[0]
        if total_duration == d_max:
            cost = total_duration + self.worker_cost_ms
            return {
                "workers": [sorted_tests],
                "k": 1,
                "makespan_ms": total_duration,
                "total_cost_ms": cost,
                "candidate_costs": {1: cost},
            }

        if sorted_durs[-1] == 0:
            nz_end = n_tests - 1
            while nz_end > 0 and sorted_durs[nz_end - 1] == 0:
                nz_end -= 1
            nz_tests = sorted_tests[:nz_end]
            nz_durs = sorted_durs[:nz_end]
            zero_tests = sorted_tests[nz_end:]
        else:
            nz_tests = sorted_tests
            nz_durs = sorted_durs
            zero_tests = None

        max_k = min(self.max_workers, len(nz_tests))
        best_cost = total_duration + self.worker_cost_ms
        best_k = 1
        best_makespan = total_duration
        best_workers: list[list[int]] = [nz_tests]
        candidate_costs: dict[int, int] = {1: best_cost}
        refinable: list[tuple[int, list[list[int]], int, int]] | None = None
        wcost = self.worker_cost_ms

        for k in range(2, max_k + 1):
            lb_ms = (total_duration + k - 1) // k
            if d_max > lb_ms:
                lb_ms = d_max
            lower_bound = lb_ms + wcost * k
            if lower_bound >= best_cost:
                candidate_costs[k] = lower_bound
                if d_max + wcost * k >= best_cost:
                    break
                continue

            if k == 2:
                l0 = l1 = 0
                w0: list[int] = []
                w1: list[int] = []
                w0_app = w0.append
                w1_app = w1.append
                for t, dur in zip(nz_tests, nz_durs):
                    if l0 <= l1:
                        w0_app(t)
                        l0 += dur
                    else:
                        w1_app(t)
                        l1 += dur
                workers = [w0, w1]
                makespan = l0 if l0 >= l1 else l1
            else:
                heap = [(0, i) for i in range(k)]
                workers = [[] for _ in range(k)]
                heapreplace = heapq.heapreplace
                for t, dur in zip(nz_tests, nz_durs):
                    load, wid = heap[0]
                    workers[wid].append(t)
                    heapreplace(heap, (load + dur, wid))
                makespan = max(load for load, _ in heap)

            cost = makespan + wcost * k
            candidate_costs[k] = cost
            if makespan > lb_ms:
                if refinable is None:
                    refinable = [(k, workers, makespan, lb_ms)]
                else:
                    refinable.append((k, workers, makespan, lb_ms))

            if cost < best_cost:
                best_cost = cost
                best_makespan = makespan
                best_workers = workers
                best_k = k

            if makespan == d_max:
                break

        if refinable is not None:
            for k, workers, makespan, lb_ms in refinable:
                wc = wcost * k
                if k == best_k:
                    gap = makespan - lb_ms
                    if gap < 2 and not (k == 2 and gap == 1 and len(nz_tests) <= 40 and (makespan - 1 - d_max) <= 8192):
                        continue
                    target_lt = makespan
                else:
                    if lb_ms + wc >= best_cost:
                        continue
                    target_lt = best_cost - wc
                    if target_lt <= lb_ms:
                        continue

                refined = self._refine_partition(
                    nz_tests, k, workers, makespan, total_duration, d_max, lb_ms, target_lt
                )
                if refined is not None:
                    new_bins, new_ms = refined
                    new_k = len(new_bins)
                    new_cost = new_ms + wcost * new_k
                    if new_cost < candidate_costs.get(new_k, float("inf")):
                        candidate_costs[new_k] = new_cost
                    if new_cost < best_cost:
                        best_cost = new_cost
                        best_makespan = new_ms
                        best_workers = new_bins
                        best_k = new_k

        if zero_tests:
            best_workers = [list(w) for w in best_workers]
            best_workers[0].extend(zero_tests)

        return {
            "workers": best_workers,
            "k": best_k,
            "makespan_ms": best_makespan,
            "total_cost_ms": best_cost,
            "candidate_costs": candidate_costs,
        }
