# BLAST RADIUS — MASTER ENGINEERING ROADMAP
## End-to-End Development Plan: Baseline → Competitive Submission → 50-Second Stretch Target

**Role:** Autonomous Senior Software Engineer, Test-Impact Analysis Specialist, Performance Engineer, and Competition Optimizer  
**Project:** Builders Buildathon — RWX / Blast Radius  
**Repository:** `/home/taha/Arc_Project/BlastRadius`  
**Primary Reference:** [`BLAST_RADIUS_MASTER_GUIDE.md`](./BLAST_RADIUS_MASTER_GUIDE.md)  
**Pinned SQLGlot Baseline:** `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`  
**Targets:** 80s (Milestone 1) → 65s (Milestone 2) → **50s (Stretch Target)**  

---

## 0. GLOBAL PROJECT CONTEXT & CORE ARCHITECTURE

### 0.1 Problem Statement
Build an automated, high-recall, ultra-low-overhead test-impact prediction and parallel test-selection system for SQLGlot. Given an arbitrary Git unified diff against the pinned baseline, predict which tests will fail and schedule selected tests across $k \in [1, 8]$ workers to minimize execution makespan and worker overhead while strictly avoiding missed failures.

### 0.2 Economic & Scoring Model
$$\text{duration}(t) = \lfloor \text{test.seconds} \times 1000 + 0.5 \rfloor$$
$$\text{load}(w) = \sum_{t \in w} \text{duration}(t)$$
$$\text{Commit Cost} = \begin{cases} 0 & \text{if } |w_{\text{active}}| = 0 \\ \max_{w \in w_{\text{active}}}(\text{load}(w)) + 2000 \times |w_{\text{active}}| & \text{if } |w_{\text{active}}| > 0 \end{cases}$$
$$\text{Simulated Time} = \sum_{\text{commits}} \text{Commit Cost} + 300,000 \times \text{Missed Commits}$$
$$\text{Run Total} = (\text{Simulated Time} \times 1000) + \text{CPU Microseconds}$$
$$\text{Submission Score} = \text{median of 3 valid runs, rounded to 0.1 s: } \frac{\lfloor (\text{microseconds} + 50000) / 100000 \rfloor}{10}$$

* **Critical Asymmetry:** Missed failure penalty = **300.0s**. Total execution time of all 1,620 tests = **~103.0s**. Recall strictly dominates Precision.
* **Worker Economics ($T/k + 2k$):** Fixed 2.0s penalty per active worker. Sizing $k \in [1, 8]$ must be dynamic based on test durations.

### 0.3 Operational Constraints
* **Build Time:** 4 CPU cores, 8 GB RAM, 10 minutes limit. Network enabled (public package registries, fixed GitHub releases, pinned SQLGlot commit only). Max repo size 20 MB.
* **Runtime:** 2 CPU cores, 4 GB RAM, 3.0 min batch timeout (4.5 min final, ~1.5x commits). Strictly NO network (`--network none`). Read-only root filesystem; `/tmp` tmpfs 64 MB. User `nobody:nobody` (65534:65534), `HOME=/tmp`, max 256 processes.
* **Accounting:** Container CPU time is directly charged to the score.

### 0.4 Core Engineering Hierarchy
1. Valid submission and evaluator compatibility (no rejections).
2. High recall / zero missed failures on validation data.
3. Correct impact analysis and dependency propagation.
4. Efficient test selection.
5. Optimal worker count and scheduling ($k \in [1, 8]$).
6. Minimal selector CPU time ($< 0.05\text{s}$ per batch).
7. Compact artifacts ($< 20\text{ MB}$) and reproducible builds.
8. Generalization to unseen commits.

---

## 1. PHASE 1 — BASELINE, REPOSITORY UNDERSTANDING & REPRODUCIBILITY

* **Stage 1.1 — Project reconnaissance:** Inspect complete structure, inventory files, verify starter kit components.
* **Stage 1.2 — Specification verification:** Cross-check `BLAST_RADIUS_MASTER_GUIDE.md`, `SCORING.md`, `score.py`, `Dockerfile`, `Makefile`, and `requirements.lock`.
* **Stage 1.3 — SQLGlot baseline checkout:** Clone and verify pinned SQLGlot commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`.
* **Stage 1.4 — Reproduce starter evaluation:** Run official sample evaluation, record baseline simulated time (~1210.5s), worker counts, and execution metrics.
* **Stage 1.5 — Build/runtime compatibility:** Test official Docker configuration with actual runtime flags (`--read-only`, `--network none`, `--user 65534:65534`, etc.).
* **Stage 1.6 — Baseline report:** Generate `reports/baseline.md`.
* **Phase 1 Exit Gate:** Starter runs reproducibly; baseline revision confirmed; evaluator verified; Docker flags tested.

---

## 2. PHASE 2 — OFFLINE STATIC ANALYSIS PIPELINE

* **Stage 2.1 — Repository inventory:** Map modules, classes, functions, methods, re-exports, dynamic imports, test files, fixtures, shared utilities.
* **Stage 2.2 — AST and symbol extraction:** Python AST parsing; generate file-to-symbol and symbol-to-file indexes with line ranges.
* **Stage 2.3 — Module import graph:** Direct/relative imports, re-exports, transitive dependency closure, dialect/optimizer linkages.
* **Stage 2.4 — Test inventory and ID mapping:** Map `test_id -> test_number -> file -> seconds`.
* **Stage 2.5 — Test-to-source coverage mapping:** Execute coverage profiling on pinned commit during build time with `SKIP_INTEGRATION=0`.
* **Stage 2.6 — Fixture dependency mapping:** Map all `.sql` fixture files under `tests/fixtures/` and pytest fixtures in `conftest.py` to test methods.
* **Stage 2.7 — Additional resource and configuration maps:** Dialects, generators, parsers, expressions, and tokenizers.
* **Stage 2.8 — Artifact generation:** Produce deterministic, compact JSON/binary lookup maps in `artifacts/` ($< 15\text{ MB}$).
* **Stage 2.9 — Reproducibility:** Script `scripts/generate_artifacts.py` to regenerate all maps from scratch.
* **Phase 2 Exit Gate:** Maps regenerate deterministically, fit under 20 MB cap, and provide complete baseline coverage.

---

## 3. PHASE 3 — DEPENDENCY INTELLIGENCE & IMPACT MODEL

* **Stage 3.1 — Dependency graph integration:** Combine AST symbols, imports, transitive closures, fixtures, and coverage into a unified graph.
* **Stage 3.2 — Direct impact analysis:** Direct coverage and direct symbol-to-test matching.
* **Stage 3.3 — Transitive impact propagation:** Multi-hop propagation (e.g. `serde.py` or `optimizer/` $\rightarrow$ executor and dialect tests).
* **Stage 3.4 — Confidence and evidence classification:** Tag relationships (Direct Coverage, Direct Import, Transitive Import, Fixture, Dialect, Heuristic).
* **Stage 3.5 — Dynamic dependency handling:** Account for dynamic imports, `__getattr__`, dialect dispatch tables, and execution fallbacks.
* **Stage 3.6 — Patch-category analysis:** Analyze `code`, `fixture`, `multi`, and `comment` categories without memorizing specific commit hashes.
* **Stage 3.7 — Recall validation:** Validate on 30 sample commits; conduct root-cause analysis for any false negative.
* **Stage 3.8 — Impact-model calibration:** Balance recall and precision across categories.
* **Phase 3 Exit Gate:** Unified impact model validated, 100% recall on sample set, explainable fallback mechanisms.

---

## 4. PHASE 4 — LOW-OVERHEAD RUNTIME TEST SELECTOR

* **Stage 4.1 — Runtime architecture:** Ultra-lean pipeline: `Diff Parse -> Change Extract -> Index Lookup -> Candidate Selection -> Worker Allocator -> JSONL Stdout`.
* **Stage 4.2 — JSONL input/output interface:** Strict conformance to `score.py` contracts (no extra stdout, max 2 MB stdout, diagnostics to stderr).
* **Stage 4.3 — Unified diff parser:** High-speed string parser extracting changed file paths, hunk ranges, modified symbols, and comment-only markers.
* **Stage 4.4 — Symbol resolution:** Fast intersection between diff hunks and precomputed symbol ranges.
* **Stage 4.5 — Affected-test resolution:** Set-union of direct, transitive, fixture, and dialect impact.
* **Stage 4.6 — Conservative fallback system:** Graded fallback expansion for unrecognized changes or global file edits.
* **Stage 4.7 — Runtime efficiency:** Zero disk writes, minimal memory allocations, sub-millisecond execution per commit.
* **Stage 4.8 — Explainability and diagnostics:** Verbose stderr logging for development/debugging.
* **Phase 4 Exit Gate:** Runtime selector passes all checks, operates completely offline, respects read-only limits, CPU time $< 0.05\text{s}$.

---

## 5. PHASE 5 — WORKER SCHEDULING & COST OPTIMIZATION

* **Stage 5.1 — Runtime model:** Integer millisecond arithmetic matching `score.py` exactly.
* **Stage 5.2 — Baseline scheduler:** Longest Processing Time first (LPT) multi-processor scheduling.
* **Stage 5.3 — Worker-count optimization:** Dynamic search over $k \in [1, \min(8, |S|)]$ minimizing $\max_w(\text{load}_w) + 2000 \times k$.
* **Stage 5.4 — Scheduling improvements:** Bin-packing optimizations, heavy test isolation (e.g. `test_execute_tpcds` 21.5s).
* **Stage 5.5 — Scheduling correctness:** Verification against all 8 worker constraints (no duplicate test, valid IDs, $\le 8$ workers).
* **Stage 5.6 — Scheduling benchmark:** Measure makespan vs. worker cost across all sample commits.
* **Phase 5 Exit Gate:** Dynamic worker selection beats static worker allocation; produces valid schedules in $< 1\text{ ms}$.

---

## 6. PHASE 6 — RECALL, ACCURACY & GENERALIZATION OPTIMIZATION

* **Stage 6.1 — Full sample evaluation:** Detailed per-commit report with recall, precision, makespan, and CPU cost.
* **Stage 6.2 — False-negative investigation:** Zero tolerance for missed failures (300s penalty). Root cause every omission.
* **Stage 6.3 — False-positive reduction:** Prune unneeded test expansion once 100% recall is stable.
* **Stage 6.4 — Special-case validation:** Test fixture edits, comment changes, dialect dispatch tables, subtest failures, import errors.
* **Stage 6.5 — Generalization testing:** Generate synthetic patches from SQLGlot commits to test unseen change patterns.
* **Stage 6.6 — Conservative selection calibration:** Tune fallback thresholds to guarantee zero missed failures.
* **Stage 6.7 — Regression protection:** Maintain automated regression harness for all tested patches.
* **Phase 6 Exit Gate:** 0 missed failures across all 30 sample commits and synthetic test sets.

---

## 7. PHASE 7 — PERFORMANCE, SCORE & RESOURCE OPTIMIZATION

* **Stage 7.1 — Score decomposition:** Granular cost breakdown: Test Makespan vs. Worker Overhead vs. Miss Penalty vs. Selector CPU.
* **Stage 7.2 — Runtime profiling:** Profile diff parsing, index querying, and scheduling.
* **Stage 7.3 — Data structure optimization:** Compact arrays, integer test IDs, bitsets, memory-mapped or compact JSON lookups.
* **Stage 7.4 — Static artifact optimization:** Compression and deduplication to stay well below 20 MB repo limit.
* **Stage 7.5 — Scheduling optimization:** Refine partition heuristics for complex commit test sets.
* **Stage 7.6 — Algorithmic optimization:** Sub-millisecond graph queries.
* **Stage 7.7 — Performance benchmark matrix:** Track score progression in `reports/EXPERIMENT_LOG.md`.
* **Stage 7.8 — Milestone targets:**
  * Milestone 1: $\le 80\text{ s}$
  * Milestone 2: $\le 65\text{ s}$
  * Stretch Milestone: $\le 50\text{ s}$
* **Phase 7 Exit Gate:** Measured, reproducible progression through milestone targets without sacrificing recall.

---

## 8. PHASE 8 — FINAL HARDENING, PRACTICE VALIDATION & SUBMISSION

* **Stage 8.1 — Clean build:** Rebuild Docker container from scratch with no cache; verify all files world-readable (`chmod 644`).
* **Stage 8.2 — Runtime isolation:** Validate under exact scorer command line (`--network none`, `--read-only`, `--tmpfs /tmp:rw,size=64m`, `--user 65534:65534`).
* **Stage 8.3 — Full correctness audit:** Run all 30 sample commits and synthetic cases through `score.py`.
* **Stage 8.4 — Score verification:** 3 consecutive runs, verify median score and zero variance.
* **Stage 8.5 — Practice submission strategy:** Submit candidate to official platform (1 submission/hour); record feedback.
* **Stage 8.6 — Final candidate selection:** Lock the best-performing, most resilient git commit.
* **Stage 8.7 — Final submission:** Push to public GitHub repository, verify Dockerfile at root, select final commit on portal.
* **Phase 8 Exit Gate:** Production container verified on practice board, documented in `reports/final_submission.md`.

---

## 9. EXPERIMENT LOG & DECISION PROTOCOL

All experiments and architectural adjustments must be logged in `reports/EXPERIMENT_LOG.md`:
`Experiment ID | Hypothesis | Change | Dataset | Recall | Selected Runtime | CPU | Total Score | Decision`

Every major design decision must follow:
$$\text{Hypothesis} \longrightarrow \text{Implementation} \longrightarrow \text{Measurement} \longrightarrow \text{Comparison} \longrightarrow \text{Decision}$$
