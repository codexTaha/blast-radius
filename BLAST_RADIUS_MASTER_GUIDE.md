# BLAST RADIUS — MASTER SPECIFICATION & DECISION REFERENCE

> **Version:** 1.0 (Locked)  
> **Track:** RWX — Blast Radius (Builders Buildathon 2026)  
> **Baseline Commit:** `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` ([sqlglot](https://github.com/tobymao/sqlglot))  
> **Status:** Authoritative Working Specification  

---

## 1. Executive Summary

* **Track Purpose:** Test-Impact Analysis (TIA) and parallel test-scheduling optimization for Python's SQLGlot library.
* **Core Objective:** Given an arbitrary unified Git diff patch against the pinned baseline, identify all tests that will fail (including errors, import issues, timeouts, and subtest failures) and partition them across up to 8 workers to minimize makespan and worker overhead while strictly avoiding missed failures.
* **Winner-Take-All Prize:** **$5,000** for 1st place (2nd place receives $0). Additional $100 side prize for best unlisted demo video. `[CONFIRMED]`
* **Official Baseline Score:** **1614.1 s** (all tests run across 3 workers). Current live leader: **131.5 s** (team `67`). `[CONFIRMED]`

---

## 2. Competition Overview & Operational Timeline

| Parameter | Specification | Evidence / Source |
| :--- | :--- | :--- |
| **Competition** | Builders Buildathon | [joinbuilders.org/buildathon](https://www.joinbuilders.org/buildathon) |
| **Track Sponsor** | RWX (Dan Manges, Co-Founder & CEO) | [joinbuilders.org/buildathon/tracks/blast-radius](https://www.joinbuilders.org/buildathon/tracks/blast-radius) |
| **Submission Cutoff** | **Sunday, Nov 22, 2026, 11:00 AM ET** | Portal Schedule `[CONFIRMED]` |
| **Closing Ceremony** | Sunday, Nov 22, 2026, 12:00 PM ET (Hitchcock Hall 131, Ohio State) | Portal Schedule `[CONFIRMED]` |
| **Team Size** | 1 to 2 members (Solo permitted) | Rules Section `[CONFIRMED]` |
| **Participation** | 100% remote allowed; prizes paid electronically | Rules Section `[CONFIRMED]` |
| **License** | **MIT License** mandatory for all submitted code | General Rules `[CONFIRMED]` |
| **Submission Limit** | 1 submission per hour per team (practice board) | Track Details `[CONFIRMED]` |
| **Tie-Breaker Rule** | Final scores within **1.0 s** of best score tie; ranked by earliest submission time | Track Rules `[CONFIRMED]` |

---

## 3. Exact Problem Specification

* **Repository Under Test:** `sqlglot` at pinned commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`.
* **Nature of Input:** Standalone patches (each commit in `/data/commits/<id>.patch` stands alone; none depends on another).
* **Failure Definition:** A test fails if pytest reports `FAILED`, `ERROR`, or `TIMEOUT`, or if its module fails during import.
* **Subtests Definition:** A test containing subtests is treated as a single test. In pytest 9, a test displaying `PASSED` alongside `SUBFAILED` lines is classified as **failing**. `[CONFIRMED: README.md#L66]`
* **Zero-Failure Commits:** If a patch breaks no tests, the correct output is `"workers": []`. This costs **0.0 s**, but incurs a 300s penalty if any test actually fails.

---

## 4. Input / Output Contract

### Input Files (Read-Only)
1. `/data/tests.json`:
   * Schema: `{"tests": [{"number": int, "id": str, "file": str, "seconds": float}], "skipped": [str]}`
   * Pinned baseline characteristics:
     * **1,620 active tests** numbered consecutively `0` through `1619` without gaps.
     * **58 test files**.
     * **103.047 seconds** cumulative test execution time.
     * **1 skipped test** (cannot fail and has no number).
2. `/data/commits/<id>.patch`:
   * Standard Git unified diff text file. Commit identifier is the filename without `.patch`.

### Output Contract (stdout)
* UTF-8 encoded JSONL (one JSON object per line, ended by `\n`).
* Exactly one line per patch in `/data/commits`. Order of lines does not matter.
* Exact JSON Schema:
  ```json
  {"commit": "<id>", "workers": [[<test_num>, <test_num>], [<test_num>]]}
  ```
* Maximum allowed stdout volume: **2 MB** (exceeding 2 MB causes immediate rejection). `[CONFIRMED: README.md#L59]`
* Logging / Diagnostics: Exclusively on `stderr`. Any non-JSON line on `stdout` causes run rejection. `[CONFIRMED: SCORING.md#L17]`

### Validation Rejection Hierarchy (`score.py`)
Evaluated in strict sequential order per line:
1. `not_utf8`: Output cannot be decoded as UTF-8.
2. `not_json`: Line cannot be parsed as standard JSON (`NaN` and `Infinity` are rejected).
3. `wrong_shape`: Missing keys or invalid types (must have exact keys `commit` (str) and `workers` (list of lists of whole numbers)).
4. `unknown_commit`: Commit ID not present in `/data/commits`.
5. `repeated_commit`: Duplicate line for a previously evaluated commit.
6. `unknown_test`: Test number not defined in `/data/tests.json`.
7. `repeated_test`: Test number scheduled more than once in the same commit.
8. `too_many_workers`: More than 8 non-empty worker lists.
9. `missing_commit`: Omitting any commit from `/data/commits` (checked after processing all lines).

---

## 5. Scoring System & Optimization Economics

### Core Formula
All durations are computed in integer milliseconds:
$$\text{duration}(t) = \lfloor \text{test.seconds} \times 1000 + 0.5 \rfloor$$
For each commit:
$$\text{load}(w) = \sum_{t \in w} \text{duration}(t)$$
$$\text{Commit Cost} = \begin{cases} 0 & \text{if } |w_{\text{active}}| = 0 \\ \max_{w \in w_{\text{active}}}(\text{load}(w)) + 2000 \times |w_{\text{active}}| & \text{if } |w_{\text{active}}| > 0 \end{cases}$$
$$\text{Simulated Time} = \sum_{\text{commits}} \text{Commit Cost} + 300,000 \times \text{Missed Commits}$$
$$\text{Run Total} = (\text{Simulated Time} \times 1000) + \text{CPU Microseconds}$$
$$\text{Submission Score} = \text{median of 3 valid runs, rounded to 0.1 s: } \frac{\lfloor (\text{microseconds} + 50000) / 100000 \rfloor}{10}$$

### Scoring Parameters
* **Worker Cost:** Fixed **2.0 seconds** (2,000 ms) per non-empty worker.
* **Worker Limit:** Up to 8 workers ($1 \le k \le 8$). Empty worker lists are automatically discarded.
* **Miss Penalty:** Fixed **300.0 seconds** (300,000 ms) per commit missing at least one failing test.
* **CPU Accounting:** Total CPU time (user + system) of all processes spawned inside the container is added to the score.

### Economic Trade-Offs

```
                      +---------------------------------------+
                      |       Test Selection Decision         |
                      +---------------------------------------+
                                     /         \
                         (Selected) /           \ (Omitted)
                                   v             v
                    Test Cost: 0.01s - 21.5s    Missed Failure Penalty: 300.0s
```

* **The 300-Second Asymmetry:** A single missed failure adds 300.0s. Running all 1,620 tests across the entire repo only takes ~103s. Therefore, **Recall strictly dominates Precision**. When in doubt, select the test.
* **Worker Scaling Overhead ($T/k + 2k$):**
  * Spreading 2.0s of tests across 8 workers costs: $\frac{2.0}{8} + 16.0 = 16.25\text{s}$.
  * Running 2.0s of tests on 1 worker costs: $2.0 + 2.0 = 4.0\text{s}$.
  * The number of workers $k \in [1, 8]$ must be determined dynamically per commit by minimizing $\max(\text{load}) + 2000k$.

---

## 6. Build and Runtime Environment

| Dimension | Build Environment | Runtime Environment |
| :--- | :--- | :--- |
| **Network** | ✅ Available (Public registries & pinned sqlglot repo only) | ❌ **Strictly None (`--network none`)** |
| **CPU Allocation** | 4 CPU cores | 2 CPU cores (`--cpus 2`) |
| **Memory Limit** | 8 GB RAM | 4 GB RAM (`--memory 4096m --memory-swap 4096m`) |
| **Execution Time** | 10 minutes maximum | **3.0 min** (practice, 30 commits) / **4.5 min** (final, ~45 commits) |
| **Filesystem State** | Standard Docker build layers | **Read-Only** (`--read-only`); `/tmp` is tmpfs 64 MB (`mode=1777`) |
| **Process Limit** | Host limit | Max 256 processes (`--pids-limit 256`) |
| **User & Privileges** | `root` (build) | User `nobody:nobody` (UID `65534`, GID `65534`), `HOME=/tmp` |
| **Security Drops** | Standard | `--cap-drop ALL`, `--security-opt no-new-privileges` |
| **Repository Size** | Max **20 MB** submitted repo | Submodules and Git LFS **not supported** |
| **Base Image** | `python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90` | Same |

---

## 7. Allowed vs. Prohibited Methodologies

### Explicitly Prohibited `[WARNING]`
* **Patch Lookup Tables:** Precomputing answers for specific patches or commit hashes and looking them up.
* **Edit-Type Lookup Maps:** Mapping specific syntactic edit types directly to predetermined test sets.
* **Private Network Endpoints:** Downloading code or assets from team-controlled URLs during build or runtime.
* **Uncommitted Runtime Artifacts:** Depending on Git submodules or Git LFS.
* **Shared Code:** Reusing repositories or code between teams.

### Explicitly Allowed & Recommended `[CONFIRMED]`
* **Static Analysis on Pinned Baseline:** AST symbol extraction, module dependency graphs, direct and transitive import graphs, fixture mappings, and coverage matrices built from commit `eb11aec4...`.
* **Committed Lookup Maps:** Generating compact binary/JSON maps offline and committing them into the 20 MB repository.
* **Runtime Dynamic Execution:** Running Python code or tests dynamically (with the caveat that runtime CPU time is charged).

---

## 8. Starter Kit Technical Artifacts

The sample distribution (`blast-radius-sample/`) contains:
* `Dockerfile`: Minimal 6-line container using pinned Python 3.13-slim digest.
* `Makefile`: Contains exact local evaluation harness commands matching server flags:
  ```bash
  docker run --rm --platform linux/amd64 --network none --cpus 2 \
    --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
    --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
    --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
    --volume $(SAMPLE)/commits:/data/commits:ro \
    --volume $(SAMPLE)/tests.json:/data/tests.json:ro $(IMAGE)
  ```
* `score.py`: Authoritative scoring engine reproducing server evaluation logic.
* `selector.py`: Baseline naive selector (schedules all 1,620 tests over 3 workers using Longest-Processing-Time-First). Simulated score: **1210.470 s** on 30 sample commits.
* `requirements.lock`: Complete frozen environment (Python 3.13, `pytest==9.2.0`, `duckdb==2.0.0`, `numpy==2.5.5`, `pandas==3.1.1`).
* `sample/answers.json`: 30 sample commits with breakdown:
  * `code`: 16 commits (0 to 37 failing tests).
  * `fixture`: 8 commits (1 failing test each, corresponding to the fixture file).
  * `comment`: 3 commits (0 failing tests in sample, but comments are not guaranteed harmless).
  * `multi`: 3 commits (1 to 3 failing tests across multiple modules).

---

## 9. Submission Requirements & Operational Pipeline

1. **GitHub Repository:** Public repository containing `Dockerfile` at the root.
2. **Submission Form:** Submit repository URL and target commit SHA on the track portal.
3. **Automated Verification:**
   * Container must compile from scratch in $< 10$ minutes.
   * Container must execute on `/data` within 3.0 minutes and output valid JSONL on `stdout`.
   * Three runs are executed; median score is recorded on the leaderboard.
4. **Final Selection:** Choose the desired commit before Sunday, Nov 22, 11:00 AM ET. If none is picked, the highest-scoring practice commit is evaluated automatically.

---

## 10. Critical Edge Cases & Failure Conditions

1. **`SKIP_INTEGRATION=0` Requirement:** SQLGlot skips executor tests unless `SKIP_INTEGRATION=0` is set. The competition benchmark was labeled with this flag set. `[CONFIRMED: README.md#L67]`
2. **File Permissions (`nobody` User):** All precomputed maps copied into the Docker container must be world-readable (`chmod -R 644 / 755`). Files generated as `root` with `chmod 600` will fail with `PermissionError` at runtime. `[CONFIRMED: README.md#L65]`
3. **Empty Commits / Harmless Changes:** For changes that break nothing, output `"workers": []`. Any non-empty worker list will incur a minimum 2.0s worker cost.
4. **Heavyweight Tests:** Tests like `test_execute_tpcds` take **21.454 s**. Isolating them on their own worker is critical when selected to prevent makespan bottlenecking.
5. **Read-Only `/tmp` Quota:** Only `/tmp` is writable, with a strict **64 MB limit**. Avoid writing large temporary files.
6. **Max 2 MB stdout:** Excessive verbose logging on stdout will trigger an immediate disqualification.

---

## 11. Strategic Architectural Blueprint

```
+---------------------------------------------------------------------------------+
|                               BUILD PHASE (Offline)                             |
|                                                                                 |
|  [SQLGlot eb11aec4...] ---> Static AST Parser ---> Symbol & Dialect Maps       |
|                       ---> Import Analyzer ---> Transitive Dependency Graph     |
|                       ---> Fixture Scanner ---> Fixture-to-Test Map (< 5 MB)   |
+---------------------------------------------------------------------------------+
                                      |
                                      v (Committed to Repo)
+---------------------------------------------------------------------------------+
|                               RUNTIME PHASE (Online)                            |
|                                                                                 |
|  [Patch Diff] ---> Micro-Diff Parser (symbols, files, AST diff)                |
|               ---> Graph Traversal on Precomputed Maps                          |
|               ---> Candidate Failing Tests (High Recall)                        |
|               ---> Dynamic Worker Partitioning ($k \in [1, 8]$, LPT Bin-Pack)   |
|               ---> stdout: {"commit": id, "workers": [[...]]}                   |
|               Execution Time: < 0.05s CPU per commit                            |
+---------------------------------------------------------------------------------+
```

1. **Zero-Overhead Runtime:**
   * Do NOT run tests dynamically at runtime; doing so consumes container CPU time, which directly degrades the score.
   * Parse the unified diff in Python using fast string/regex scanning ($< 1$ ms per patch).
2. **Multi-Tiered Blast Radius Resolution:**
   * **Tier 1 (Fixture changes):** Match modified fixture paths in `tests/fixtures/` directly to the specific test method.
   * **Tier 2 (Dialect changes):** Changes in `sqlglot/dialects/<dialect>.py` map to dialect test cases (`test_dialects.py`, `test_<dialect>.py`).
   * **Tier 3 (Core/Optimizer changes):** Transitive dependency graph propagation (e.g. `optimizer/` $\rightarrow$ `test_optimizer.py`, `test_executor.py`).
3. **Makespan-Optimal Worker Partitioning:**
   * For selected test set $S$:
     * If $|S| = 0 \rightarrow$ return `[]`.
     * For each candidate worker count $k \in [1, \min(8, |S|)]$:
       * Schedule $S$ using Longest Processing Time (LPT) first.
       * Cost($k$) = $\max_w(\text{load}_w) + 2000 \times k$.
     * Choose $k^* = \arg\min_k \text{Cost}(k)$.

---

## 12. Non-Negotiable Checklist

- [ ] Repository size $\le$ 20 MB (no git submodules, no git LFS).
- [ ] Dockerfile placed at repository root.
- [ ] Pinned base image `python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90` retained.
- [ ] License file is standard MIT.
- [ ] All precomputed maps are world-readable (`chmod 644`).
- [ ] All diagnostic logs go to `stderr`; only valid JSONL goes to `stdout`.
- [ ] Maximum 8 non-empty workers per commit.
- [ ] No test number repeated within or across workers in the same commit.
- [ ] Every commit in `/data/commits` is represented in the output.
- [ ] Container operates cleanly under `nobody:nobody` with read-only root and 64 MB `/tmp`.

---

## 13. Unknowns and Clarifications

* `[CONFIRMED]` The practice board contains 30 commits; the final evaluation contains ~45 commits (~1.5×).
* `[CONFIRMED]` The batch execution timeout increases from 3.0 minutes to 4.5 minutes for the final evaluation.
* `[CONFIRMED]` The ground truth answers in the final evaluation will contain unseen kinds of changes not present in the 30 sample commits (e.g. subtle comment syntax, configuration updates, or cross-cutting core engine modifications).

---

## 14. Source Index

1. **Official Competition Portal:** `https://www.joinbuilders.org/buildathon`
2. **Blast Radius Track Portal:** `https://www.joinbuilders.org/buildathon/tracks/blast-radius`
3. **Official Scorer Specification:** `SCORING.md` (local copy: `blast-radius-sample/SCORING.md`)
4. **Official Scorer Implementation:** `score.py` (local copy: `blast-radius-sample/score.py`)
5. **SQLGlot Baseline Repository:** `https://github.com/tobymao/sqlglot/tree/eb11aec4be8cb2122afe713a4816662c3f0fb2fa`
6. **Environment Packages:** `requirements.lock` (local copy: `blast-radius-sample/requirements.lock`)
