# Full Execution Timeline and Time-Usage Audit

- **Baseline Checkpoint:** `950e07e` (Opportunity 1 Verified Checkpoint)
- **Official Simulated Score:** **180.769 s**
- **Recall:** **100.00% (73/73 caught, 0 misses)**
- **Total Tests Selected:** 1660 (mean 55.3 tests/commit)

---

## 1. Overall Time Decomposition (Simulated vs Real)

The competition evaluation consists of two completely separate temporal domains:

1. **Real Selector Wall-Clock & CPU Execution (Container Overhead):**
   - **Total Selector Wall Time:** **89.23 ms**
   - **Total Selector CPU Time:** **80.00 ms** (median inside container: ~120–130 ms)
   - **Data Loading & Bundle Ingestion:** **73.79 ms**
   - **Engine Initialization:** **0.02 ms**
   - **Commit Processing Loop (30 commits):** **13.40 ms** (mean 0.45 ms/commit)

2. **Simulated Parallel Test Execution ($J(k, P) = \text{makespan} + 2\text{s} \times k$):**
   - **Total Simulated Score:** **180.769 s**
   - **Pure Parallel Test Makespan:** **100.769 s** (55.7% of score)
   - **Worker Overhead Fees ($2\text{s} \times k$):** **80.000 s** (44.3% of score)
   - **Total Unparallelized Test Workload:** **200.644 s** (parallel scheduling achieved a 1.99x compression factor)

---

## 2. The 5 Biggest Time Consumers

### Top 5 Most Expensive Commits
| Rank | Commit ID | Kind | Tests Sel | Workers ($k$) | Makespan | Worker Fees | Total Cost | % of Total Score | Bottleneck Test |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **1** | `2jrq` | code | 231 | 3 | 21.454 s | 6.000 s | **27.454 s** | 15.18% | Test 1106 (`test_execute_tpcds`, 21.45s) |
| **2** | `5y72` | code | 231 | 3 | 21.454 s | 6.000 s | **27.454 s** | 15.18% | Test 1106 (`test_execute_tpcds`, 21.45s) |
| **3** | `3utz` | code | 37 | 2 | 21.454 s | 4.000 s | **25.454 s** | 14.08% | Test 1106 (`test_execute_tpcds`, 21.45s) |
| **4** | `ary6` | code | 144 | 2 | 5.390 s | 4.000 s | **9.390 s** | 5.19% | Test 1428 (`test_scope`, 2.53s) |
| **5** | `2haz` | code | 95 | 2 | 3.885 s | 4.000 s | **7.885 s** | 4.36% | Test 1428 (`test_scope`, 2.53s) |

> Top 3 commits alone account for **80.362 s (44.45%)** of the entire score.
> Top 5 commits account for **97.637 s (54.01%)**.

---

## 3. Top 5 Slowest Individual Tests & Cumulative Workload Contributors

### Slowest Individual Tests in SQLGlot
1. **Test 1490:** `tests/test_parser.py::TestParser::test_max_nodes` (**28.817 s**) — *Completely eliminated from selection (0 occurrences)*.
2. **Test 1106:** `tests/test_executor.py::TestExecutor::test_execute_tpcds` (**21.454 s**) — *Selected in 3 commits (`2jrq`, `5y72`, `3utz`), contributing 64.362 s makespan*.
3. **Test 1428:** `tests/test_optimizer.py::TestOptimizer::test_scope` (**2.532 s**) — *Selected in 3 commits (`2haz`, `2jrq`, `5y72`), contributing 7.596 s workload*.
4. **Test 1295:** `tests/test_optimizer.py::TestOptimizer::test_annotate_funcs` (**1.960 s**) — *Selected in 10 commits, contributing 19.600 s cumulative workload*.
5. **Test 1382:** `tests/test_optimizer.py::TestOptimizer::test_qualify_columns` (**1.410 s**) — *Selected in 9 commits, contributing 12.690 s cumulative workload*.

---

## 4. Workload vs Worker Fees Analysis

- **Actual Test Execution Workload:** **100.769 s (55.7%)** of simulated score.
- **Worker Overhead Fees ($2\text{s} \times k$):** **80.000 s (44.3%)** of simulated score across 40 total worker launches.
- Worker distribution across 30 commits:
  - $k=0$: 3 commits (comment-only commits `9u5k`, `bap8`, `f4xa` $\rightarrow$ cost = 0.000 s)
  - $k=1$: 16 commits (including `4d63` after Opportunity 1 optimization)
  - $k=2$: 9 commits
  - $k=3$: 2 commits (`2jrq`, `5y72` due to Test 1106)
  - $k \in [4..8]$: 0 commits

---

## 5. Key Optimization Opportunities Revealed by the Timeline

1. **Avoidable Workload in Commit `5y72` (Optimizer Safety Bridge Over-Selection):**
   - In `5y72` (`sqlglot/optimizer/qualify.py`), `test_executor.py` is pulled by Tier 4's multiprocessing safety bridge, selecting Test 1106 (21.454 s).
   - In ground truth, `5y72` has **0 failures**.
   - If `test_executor.py` selection for optimizer rules can safely isolate executor integration tests (or run only TPCH instead of TPCDS when TPCDS expressions are not modified), `5y72` cost drops from **27.454 s to ~7.000 s**, saving **~20.45 s**!
2. **Worker Overhead vs Makespan Pruning on Light Commits:**
   - For 16 commits on $k=1$, makespan is often $< 0.1$ s, but the fixed worker fee is 2.000 s (accounting for > 95% of the commit's cost).
   - Worker allocation is already mathematically optimal given the selected tests, so reducing tests to 0 is the only way to eliminate the 2.000 s fee for clean commits.

---

## 6. Deliverables Generated
- `commit_timeline.csv`: Per-commit timing, worker allocations, and bottleneck metrics.
- `worker_timeline.csv`: Reconstructed chronological timeline for each worker test-by-test.
- `global_timeline.csv`: High-resolution timestamps for all program initialization and processing stages.
- `test_costs.csv`: Frequency, cumulative workload, and bottleneck classification for all tests.
- `timeline.json`: Complete machine-readable audit payload.
