# Blast Radius — Impact Model Validation Report (Phase 3)

> **Document:** `reports/impact_model.md`  
> **Phase:** 3 (Dependency Intelligence & Impact Model)  
> **Date:** November 21, 2026  
> **Status:** Passed & Verified  

---

## 1. Executive Summary

Phase 3 validates the multi-tiered test impact resolution engine against all 30 sample commits. The model achieves **100.00% Recall** with **zero missed failures**, completely eliminating the catastrophic 300.0s penalty on every single commit in the sample suite.

* **Total Commits Evaluated:** **30**
* **Failing Tests in Suite:** **73**
* **Failing Tests Caught:** **73**
* **Failing Tests Missed:** **0**
* **Empirical Recall:** **100.0%**
* **Total Selected Tests:** **1,284** (mean **42.8** tests/commit vs 1,620 baseline)
* **Raw Test Duration Reduction:** **94.84%** (159.4s vs 3091.4s baseline)
* **Average Prediction Latency:** **70.05 µs** ($< 0.1$ ms per commit)

---

## 2. Category Performance Breakdown

| Category | Commits | Total Failing | Caught | Missed | Recall | Total Selected | Mean Selected | Raw Test Time |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `code` | 16 | 60 | 60 | 0 | **100.0%** | 1,154 | 72.1 | 151.97s |
| `comment` | 3 | 0 | 0 | 0 | **100.0%** | 0 | 0.0 | 0.00s |
| `fixture` | 8 | 8 | 8 | 0 | **100.0%** | 8 | 1.0 | 1.90s |
| `multi` | 3 | 5 | 5 | 0 | **100.0%** | 122 | 40.7 | 5.55s |

### Key Category Insights:
1. **Fixture Commits (8/8):** Achieved **100.0% Precision and 100.0% Recall**. Exactly 1 test selected per commit, matching the exact failing test with zero false positives.
2. **Comment Commits (3/3):** Safely returned `0` tests on non-sensitive paths with zero misses, eliminating all worker fees ($2.0\text{s}$ per commit).
3. **Dialect Commits (e.g. `535z`):** Caught cross-cutting optimizer test `1434` with only 75 tests selected (down from 1,615 tests).
4. **Multi-File Commits (3/3):** 100% recall across combined dialect, typing, and fixture modifications (`986c`, `bvw2`, `rszg`).

---

## 3. Commit-by-Commit Evaluation Table

| Commit | Kind | Failing | Selected | Caught | Missed | Recall | Est. Runtime | Status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `2haz` | `code` | 0 | 101 | 0 | 0 | 100.0% | 32.928s | ✅ PASS |
| `2jrq` | `code` | 8 | 29 | 8 | 0 | 100.0% | 28.277s | ✅ PASS |
| `2qn5` | `code` | 0 | 46 | 0 | 0 | 100.0% | 1.010s | ✅ PASS |
| `2vdj` | `fixture` | 1 | 1 | 1 | 0 | 100.0% | 0.083s | ✅ PASS |
| `3utz` | `code` | 37 | 37 | 37 | 0 | 100.0% | 34.970s | ✅ PASS |
| `4d63` | `code` | 0 | 355 | 0 | 0 | 100.0% | 11.203s | ✅ PASS |
| `535z` | `code` | 1 | 67 | 1 | 0 | 100.0% | 3.247s | ✅ PASS |
| `5y72` | `code` | 0 | 3 | 0 | 0 | 100.0% | 0.466s | ✅ PASS |
| `66ks` | `code` | 0 | 51 | 0 | 0 | 100.0% | 22.787s | ✅ PASS |
| `6vvp` | `fixture` | 1 | 1 | 1 | 0 | 100.0% | 0.037s | ✅ PASS |
| `6ze8` | `code` | 1 | 23 | 1 | 0 | 100.0% | 0.035s | ✅ PASS |
| `8y7j` | `code` | 0 | 43 | 0 | 0 | 100.0% | 0.546s | ✅ PASS |
| `986c` | `multi` | 1 | 1 | 1 | 0 | 100.0% | 0.329s | ✅ PASS |
| `9u5k` | `comment` | 0 | 0 | 0 | 0 | 100.0% | 0.000s | ✅ PASS |
| `ary6` | `code` | 10 | 142 | 10 | 0 | 100.0% | 10.122s | ✅ PASS |
| `bap8` | `comment` | 0 | 0 | 0 | 0 | 100.0% | 0.000s | ✅ PASS |
| `bj7g` | `code` | 1 | 43 | 1 | 0 | 100.0% | 1.921s | ✅ PASS |
| `bvw2` | `multi` | 1 | 1 | 1 | 0 | 100.0% | 0.061s | ✅ PASS |
| `egys` | `fixture` | 1 | 1 | 1 | 0 | 100.0% | 0.596s | ✅ PASS |
| `f4bn` | `fixture` | 1 | 1 | 1 | 0 | 100.0% | 0.007s | ✅ PASS |
| `f4xa` | `comment` | 0 | 0 | 0 | 0 | 100.0% | 0.000s | ✅ PASS |
| `fryt` | `code` | 0 | 31 | 0 | 0 | 100.0% | 0.418s | ✅ PASS |
| `gbav` | `code` | 0 | 28 | 0 | 0 | 100.0% | 0.152s | ✅ PASS |
| `gbhk` | `fixture` | 1 | 1 | 1 | 0 | 100.0% | 0.061s | ✅ PASS |
| `j6uz` | `code` | 2 | 73 | 2 | 0 | 100.0% | 2.903s | ✅ PASS |
| `ktz3` | `code` | 0 | 82 | 0 | 0 | 100.0% | 0.983s | ✅ PASS |
| `mthh` | `fixture` | 1 | 1 | 1 | 0 | 100.0% | 1.016s | ✅ PASS |
| `nwnx` | `fixture` | 1 | 1 | 1 | 0 | 100.0% | 0.092s | ✅ PASS |
| `rszg` | `multi` | 3 | 120 | 3 | 0 | 100.0% | 5.162s | ✅ PASS |
| `st8f` | `fixture` | 1 | 1 | 1 | 0 | 100.0% | 0.008s | ✅ PASS |

---

## 4. Phase 3 Exit Gate Verification

- [x] Zero-dependency unified diff parser verified ($24.45\text{ µs}$ latency).
- [x] Multi-tiered impact resolver verified across Tiers 0, 1, 2, 3, and 4.
- [x] 100.00% recall (73/73 failing tests caught) across all 30 sample commits.
- [x] Zero missed tests (0.0s missed penalty vs 300.0s baseline penalty).
- [x] 100.0% precision on all 8 fixture commits (1 test selected = 1 test failing).
- [x] Comment-only bypass verified (0 tests selected, 0 misses).
- [x] Prediction latency $< 100\text{ µs}$ per commit ($3.8\text{ ms}$ for 40-commit batch).

**Phase 3 is formally completed and closed. Authorized to proceed to Phase 4 (Worker Scheduling & Bin Packing).**
