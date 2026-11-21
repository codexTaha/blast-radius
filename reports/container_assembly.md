# Blast Radius — Runtime Architecture & Container Assembly Report (Phase 5)

> **Document:** `reports/container_assembly.md`  
> **Phase:** 5 (Runtime Architecture & Container Assembly)  
> **Date:** November 21, 2026  
> **Status:** Passed & Verified  
> **Pinned Base Image:** `python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90`  

---

## 1. Executive Summary

Phase 5 packages the complete Blast Radius prediction engine into a standalone, competition-compliant Docker container. The container was constructed and verified against the official evaluation harness under exact competition constraints (`--network none`, `--read-only`, `--user 65534:65534`, `--cpus 2`, `--memory 4096m`, `/tmp` 64 MB tmpfs).

* **Validation Harness Checks:**
  * `make check`: **PASSED** (`format ok`).
  * `make sample`: **PASSED** (`Simulated: 284.987 s`, `Misses: 0`).
* **Performance vs Baseline:**
  * Baseline Starter Score: **1,210.470 s**
  * Production Container Score: **284.987 s** (**−76.46% score reduction**, saving **925.483 seconds**).
* **Runtime Efficiency:**
  * Execution Time: **87.3 ms** across all 30 sample commits (**2.91 ms / commit**).
  * Startup & Initialization: **49.1 ms** (one-time index load).
  * Payload Size: **31,202 bytes** on stdout (well below the 2,000,000 byte limit).
* **Container Isolation & Security:**
  * Runs as unprivileged user `nobody` (`UID 65534:65534`).
  * Enforces `--read-only` root filesystem; only writes to tmpfs at `/tmp`.
  * Zero network access (`--network none`).
  * Zero external dependencies (pure Python standard library).
* **Phase 5 Exit Gate:** **PASSED**.

---

## 2. Stage-by-Stage Implementation & Verification

### Stage 5.1 — Standalone Production Selector (`selector.py`)
* **File:** [`selector.py`](file:///home/taha/Arc_Project/BlastRadius/selector.py)
* **Functionality:**
  * Automatically detects data directories (`/data` in Docker, `blast-radius-sample/sample` locally).
  * Reads patch files sequentially from `commits/*.patch`.
  * Uses [`src/impact_resolver.py`](file:///home/taha/Arc_Project/BlastRadius/src/impact_resolver.py) to resolve test impact across Tiers 0–4 with per-file comment bypass.
  * Uses [`src/worker_scheduler.py`](file:///home/taha/Arc_Project/BlastRadius/src/worker_scheduler.py) to partition tests across $k^* \in [0, 8]$ workers using C-accelerated LPT bin-packing.
  * Emits raw UTF-8 JSONL directly to `sys.stdout` (`sys.stdout.write()`).
  * All diagnostics, timings, and logs are directed exclusively to `sys.stderr`.
* **Verification:** Passed on host in 66.1 ms with zero schema violations.

### Stage 5.2 — Production Container Packaging (`Dockerfile` & `Makefile`)
* **Files:** [`Dockerfile`](file:///home/taha/Arc_Project/BlastRadius/Dockerfile), [`Makefile`](file:///home/taha/Arc_Project/BlastRadius/Makefile), [`score.py`](file:///home/taha/Arc_Project/BlastRadius/score.py)
* **Architecture:**
  * Base Image: Pinned official Python 3.13-slim digest.
  * Copies `selector.py`, `src/`, and `artifacts/`.
  * Sets world-readable permissions (`chmod -R 755 /app`) so user `65534` can read all artifacts under `--read-only`.
  * Sets `ENV PYTHONUNBUFFERED=1` for immediate stdout streaming.
* **Build Time:** 4.7 seconds. Content size: 45.2 MB. Tracked repo size: 11 MB ($\le 20\text{ MB}$ limit).

### Stage 5.3 — Containerized Verification with Evaluation Harness
* **Commands:**
  * `make check`: Verified schema, UTF-8, commit coverage, test uniqueness, and worker limits inside Docker. Output: `format ok`.
  * `make sample`: Verified simulated score inside Docker. Output: `Simulated: 284.987 s`, `Misses: 0`.
* **Worker Distribution across 30 Commits:**
  * $k = 0$: 3 commits (comment-only, cost: 0.000s)
  * $k = 1$: 14 commits (fixtures, fast dialects, cost: ~2.0s to 5.7s)
  * $k = 2$: 8 commits (medium dialect/code, cost: ~7.2s to 9.9s)
  * $k = 3$: 2 commits (integration tests, cost: 27.454s)
  * $k = 4$: 3 commits (full suite, cost: 36.817s)

---

## 3. Phase 5 Exit Gate Checklist

- [x] Production `selector.py` implemented at repository root.
- [x] Dockerfile builds cleanly in $< 5\text{s}$ using pinned Python 3.13-slim digest.
- [x] Repository size verified at 11 MB ($\le 20.0\text{ MB}$ limit).
- [x] Image runs under `--network none`, `--read-only`, and user `65534:65534`.
- [x] `make check` passes with `format ok`.
- [x] `make sample` passes with exact score `284.987 s` and `0` misses.
- [x] Batch execution CPU time verified at $87.3\text{ ms}$ ($2.91\text{ ms / commit}$).
- [x] Output strictly conforms to competition JSONL format ($\le 2.0\text{ MB}$).

**Phase 5 is formally completed and closed. Authorized to proceed to Phase 6 (Fine-Grained Optimization toward 50s Target).**
