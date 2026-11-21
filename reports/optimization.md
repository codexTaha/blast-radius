# Phase 6 Optimization Report: Precision Tuning, Import Pruning, and Subprocess Bridging

## 1. Executive Summary
* **Phase Objective:** Perform deep commit-level diagnostics, eliminate unnecessary over-selection in heavy commits, fix dynamic profiler blindspots, and drive simulated execution score toward the competition target while strictly maintaining **100.00% recall (0 misses)**.
* **Baseline Score (Phase 1):** 1,210.470 s (100% full-suite execution).
* **Previous Score (Phase 5):** 284.987 s (76.46% reduction, 7,011 tests selected).
* **Final Phase 6 Score:** **184.575 s** (84.75% reduction vs baseline, 1,689 tests selected).
* **Net Phase 6 Improvement:** **-100.412 s (35.23% additional speedup)** over Phase 5.
* **Recall Rate:** **100.00% (73 / 73 failing tests caught across all 30 sample commits, 0 penalties)**.
* **Batch Container Execution:** 124.2 ms across all 30 commits (73.9 ms init, 4.14 ms / commit).

---

## 2. Stage 6.1 — Heavy Commit Diagnostics & Test Isolation Analysis
Through automated diagnostic profiling across all 30 sample commits (`scripts/diagnose_heavy_commits.py`), we identified:

1. **The 67% Pareto Concentration:**
   Six commits (`2haz`, `3utz`, `ktz3`, `2jrq`, `5y72`, `66ks`) accounted for **190.813 s (66.95%)** of the entire 284.987 s Phase 5 score.
2. **The Bottleneck Tests:**
   * Test `1490` (`tests/test_parser.py::TestParser::test_max_nodes`): **28.817 s**.
   * Test `1106` (`tests/test_executor.py::TestExecutor::test_execute_tpcds`): **21.454 s**.
3. **The Static Import Closure (`test_dependents`) Inflation:**
   When core files like `serde.py` or `ddl.py` were modified, Tier 4 unconditionally pulled in the static module import closure, triggering up to 59 test files (1,615 tests) and pulling in slow test `1490` even when dynamic coverage proved 0 or only a tiny handful of tests were affected.
4. **The Multiprocessing Profiler Blindspot:**
   `tests/test_optimizer.py` and `tests/test_executor.py` execute integration tests inside spawned child processes:
   * `cls.pool = ProcessPoolExecutor(mp_context=get_context("spawn"))` in `test_optimizer.py`.
   * `with ProcessPoolExecutor(mp_context=get_context("spawn")) as pool:` in `test_executor.py`.
   Python 3.12+ `sys.monitoring` only traced the parent process, leaving child-process execution of `sqlglot/optimizer/*` rules untracked. This explained why pure dynamic coverage without static fallbacks missed tests `1106`, `1440`, `1441` on commit `2jrq`.
5. **The AST Symbol Key Mismatch:**
   AST extraction produced class-qualified names (`sqlglot.dialects.dialect.Dialect.case_sensitive`), whereas runtime profiling recorded bytecode function keys (`sqlglot/dialects/dialect.py::case_sensitive`). As a result, Tier 3 symbol matching was silently failing for class methods, forcing whole-file fallbacks.

---

## 3. Stage 6.2 — Precision Tuning & Pruning Implementation

### 3.1 Architectural Enhancements to `src/impact_resolver.py`
1. **Symbol Key Normalization:**
   Implemented `_symbol_to_key(path, qname)`, mapping qualified AST symbol names to runtime bytecode keys `f"{path}::{func_name}"`. This allowed method-level edits like `2haz` (`case_sensitive`) to resolve directly to 95 tests instead of 1,586 tests, reducing commit cost from **36.817 s to 7.885 s**.
2. **Optimizer Multiprocessing Safety Bridge:**
   Mapped `tests/test_optimizer.py` (167 tests, 19.8s) and `tests/test_executor.py` explicitly to changes under `sqlglot/optimizer/*`. This completely covered the child-process execution blindspot, ensuring 100% recall on optimizer mutations (`2jrq` caught all 8 failing tests).
3. **Pruned Static Import Graph Closure:**
   Replaced unconditional `test_dependents` queries with dynamic file-level coverage (`file_to_tests`). Retained `test_dependents` strictly as a fallback for brand-new or unprofiled files with zero dynamic coverage.

---

## 4. Empirical Evaluation & Trajectory Comparison

### 4.1 Trajectory Comparison Across All Phases

| Metric | Phase 1 Baseline Starter | Phase 5 Container Assembly | Phase 6 Precision Tuned | Delta (Phase 6 vs Baseline) | Delta (Phase 6 vs Phase 5) |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Simulated Score** | **1,210.470 s** | **284.987 s** | **184.575 s** | **-1,025.895 s (-84.75%)** | **-100.412 s (-35.23%)** |
| **Total Tests Selected** | 48,600 | 7,011 | 1,689 | -46,911 (-96.53%) | -5,322 (-75.91%) |
| **Mean Tests / Commit** | 1,620.0 | 233.7 | 56.3 | -1,563.7 | -177.4 |
| **Failing Tests Caught** | 73 / 73 | 73 / 73 | 73 / 73 | 0 missed | 0 missed |
| **Recall Rate** | 100.00% | 100.00% | 100.00% | Exact parity | Exact parity |
| **Miss Penalties** | 0.0 s | 0.0 s | 0.0 s | 0.0 s | 0.0 s |
| **Batch CPU Time** | 0.0089 s | 0.0873 s | 0.1242 s | +0.1153 s | +0.0369 s |
| **JSONL Output Size** | ~80 KB | 31,202 B | 8,332 B | -89.6% | -73.3% |

### 4.2 Commit-by-Commit Score Breakdown (Phase 6)

| Commit | Kind | Fail Count | Tests Selected | $k^*$ Workers | Phase 5 Cost | Phase 6 Cost | Net Savings | Misses |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `2haz` | `code` | 0 | 95 | 2 | 36.817 s | 7.885 s | +28.932 s | 0 |
| `3utz` | `code` | 37 | 37 | 2 | 36.817 s | 25.454 s | +11.363 s | 0 |
| `ktz3` | `code` | 0 | 128 | 2 | 36.817 s | 6.965 s | +29.852 s | 0 |
| `2jrq` | `code` | 8 | 231 | 3 | 27.454 s | 27.454 s | 0.000 s | 0 |
| `5y72` | `code` | 0 | 231 | 3 | 27.454 s | 27.454 s | 0.000 s | 0 |
| `66ks` | `code` | 0 | 50 | 1 | 25.454 s | 3.333 s | +22.121 s | 0 |
| `ary6` | `code` | 10 | 144 | 2 | 9.958 s | 9.390 s | +0.568 s | 0 |
| `j6uz` | `code` | 2 | 82 | 2 | 9.958 s | 6.916 s | +3.042 s | 0 |
| `4d63` | `code` | 0 | 258 | 2 | 9.009 s | 9.009 s | 0.000 s | 0 |
| `2qn5` | `code` | 0 | 50 | 2 | 7.817 s | 6.529 s | +1.288 s | 0 |
| `8y7j` | `code` | 0 | 47 | 2 | 7.817 s | 6.297 s | +1.520 s | 0 |
| `bj7g` | `code` | 1 | 46 | 2 | 7.817 s | 6.282 s | +1.535 s | 0 |
| `rszg` | `multi` | 3 | 127 | 2 | 7.200 s | 7.181 s | +0.019 s | 0 |
| `535z` | `code` | 1 | 75 | 1 | 5.701 s | 5.701 s | 0.000 s | 0 |
| `mthh` | `fixture` | 1 | 1 | 1 | 3.016 s | 3.016 s | 0.000 s | 0 |
| `egys` | `fixture` | 1 | 1 | 1 | 2.596 s | 2.596 s | 0.000 s | 0 |
| `986c` | `multi` | 1 | 1 | 1 | 2.329 s | 2.329 s | 0.000 s | 0 |
| `fryt` | `code` | 0 | 25 | 1 | 2.207 s | 2.206 s | +0.001 s | 0 |
| `gbav` | `code` | 0 | 29 | 1 | 2.193 s | 2.178 s | +0.015 s | 0 |
| `nwnx` | `fixture` | 1 | 1 | 1 | 2.092 s | 2.092 s | 0.000 s | 0 |
| `2vdj` | `fixture` | 1 | 1 | 1 | 2.083 s | 2.083 s | 0.000 s | 0 |
| `bvw2` | `multi` | 1 | 1 | 1 | 2.061 s | 2.061 s | 0.000 s | 0 |
| `gbhk` | `fixture` | 1 | 1 | 1 | 2.061 s | 2.061 s | 0.000 s | 0 |
| `6ze8` | `code` | 1 | 24 | 1 | 2.207 s | 2.051 s | +0.156 s | 0 |
| `6vvp` | `fixture` | 1 | 1 | 1 | 2.037 s | 2.037 s | 0.000 s | 0 |
| `st8f` | `fixture` | 1 | 1 | 1 | 2.008 s | 2.008 s | 0.000 s | 0 |
| `f4bn` | `fixture` | 1 | 1 | 1 | 2.007 s | 2.007 s | 0.000 s | 0 |
| `9u5k` | `comment` | 0 | 0 | 0 | 0.000 s | 0.000 s | 0.000 s | 0 |
| `bap8` | `comment` | 0 | 0 | 0 | 0.000 s | 0.000 s | 0.000 s | 0 |
| `f4xa` | `comment` | 0 | 0 | 0 | 0.000 s | 0.000 s | 0.000 s | 0 |
| **TOTAL** | — | **73** | **1,689** | — | **284.987 s** | **184.575 s** | **+100.412 s** | **0** |

---

## 5. Stage 6.3 Verification & Exit Gate Audit
* [x] **Zero Penalties / 100% Recall:** 73/73 true failures caught, 0 misses across all 30 sample commits.
* [x] **Substantial Score Reduction:** Simulated score decreased from 284.987 s to 184.575 s (saving 100.412 s).
* [x] **Unit Testing:** 20/20 unit tests pass in 0.17s (`python3 -m unittest discover -s tests_unit`).
* [x] **Harness Compliance:** Output passes `make check` (`format ok`) and `make sample` (`Simulated: 184.575 s`, `Misses: 0`).
* [x] **Container Boundaries:** Strictly executed under non-root user `65534:65534` (`nobody`), `--network none`, `--read-only`, and tmpfs `/tmp`.
* [x] **Repository Footprint:** Current repository size is **14.5 MB**, strictly within the 20 MB competition ceiling.
* [x] **Git Cleanliness:** All artifacts, scripts, and reports committed with descriptive messages.
* **Phase 6 Exit Gate Status:** **PASSED & CLOSED**.
