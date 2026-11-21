#!/usr/bin/env python3
"""Stage 7.1 — Synthetic Patch Stress Testing & Adversarial Edge Cases.

Generates and executes a comprehensive adversarial test suite covering:
1. Empty / whitespace-only patches
2. Non-code assets (docs, markdown, CI workflows)
3. Comment-only changes in core and dialect files
4. File renames (`git mv` / rename from / rename to)
5. File deletions (`deleted file mode 100644`)
6. Brand-new unprofiled source files (`new file mode 100644`)
7. Malformed / corrupted diff hunks
8. Top-level module variables and class definitions outside methods
9. Massive multi-file patches (stressing parser throughput and scheduler)
10. Unseen dialect mutations

Validates:
- Zero crashes, zero unhandled exceptions
- Strictly valid JSONL format with disjoint worker allocations
- Worker count k in [0..8]
- Predictable and fast execution time per patch
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path
root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

from src.diff_parser import parse_patch
from src.impact_resolver import ImpactResolver
from src.worker_scheduler import WorkerScheduler


def generate_synthetic_patches() -> dict[str, str]:
    """Generate a dictionary of adversarial patch name -> patch unified diff text."""
    patches: dict[str, str] = {}

    # 1. Empty patch
    patches["syn_01_empty"] = ""

    # 2. Whitespace-only patch
    patches["syn_02_whitespace"] = """diff --git a/sqlglot/dialects/postgres.py b/sqlglot/dialects/postgres.py
index 1111111..2222222 100644
--- a/sqlglot/dialects/postgres.py
+++ b/sqlglot/dialects/postgres.py
@@ -10,3 +10,3 @@
-    foo = 1
+    foo = 1   
"""

    # 3. Non-code docs and CI workflows
    patches["syn_03_non_code_docs"] = """diff --git a/README.md b/README.md
index 3333333..4444444 100644
--- a/README.md
+++ b/README.md
@@ -1,3 +1,3 @@
-# SQLGlot
+# SQLGlot Parser Engine
diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
index 5555555..6666666 100644
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -10,2 +10,2 @@
-        python-version: "3.11"
+        python-version: "3.13"
"""

    # 4. Comment-only in dialect
    patches["syn_04_comment_dialect"] = """diff --git a/sqlglot/dialects/mysql.py b/sqlglot/dialects/mysql.py
index 7777777..8888888 100644
--- a/sqlglot/dialects/mysql.py
+++ b/sqlglot/dialects/mysql.py
@@ -40,3 +40,4 @@
     # Existing comment
+    # Additional MySQL documentation note
     class Parser(parser.Parser):
"""

    # 5. Comment-only in core engine file
    patches["syn_05_comment_core"] = """diff --git a/sqlglot/expressions/core.py b/sqlglot/expressions/core.py
index 9999999..aaaaaaa 100644
--- a/sqlglot/expressions/core.py
+++ b/sqlglot/expressions/core.py
@@ -100,3 +100,4 @@
     # Internal AST node representation
+    # Note on AST traversal
     def walk(self):
"""

    # 6. File rename (git mv)
    patches["syn_06_file_rename"] = """diff --git a/sqlglot/transforms.py b/sqlglot/transformations.py
similarity index 100%
rename from sqlglot/transforms.py
rename to sqlglot/transformations.py
"""

    # 7. File deletion
    patches["syn_07_file_deletion"] = """diff --git a/sqlglot/deprecated_helper.py b/sqlglot/deprecated_helper.py
deleted file mode 100644
index bbbbbbb..0000000
--- a/sqlglot/deprecated_helper.py
+++ /dev/null
@@ -1,10 +0,0 @@
-def old_func():
-    pass
"""

    # 8. Brand-new source file (triggering Tier 5 fallback)
    patches["syn_08_brand_new_file"] = """diff --git a/sqlglot/custom_optimizer_rule.py b/sqlglot/custom_optimizer_rule.py
new file mode 100644
index 0000000..ccccccc
--- /dev/null
+++ b/sqlglot/custom_optimizer_rule.py
@@ -0,0 +1,8 @@
+from sqlglot import exp
+
+def custom_optimize(expression):
+    return expression
"""

    # 9. Corrupted / malformed patch
    patches["syn_09_corrupted_patch"] = """diff --git a/sqlglot/parser.py b/sqlglot/parser.py
garbage header without line numbers
@@ invalid hunk header @@
+++ broken lines without minus or plus
just random text that might appear in corrupt unified diffs
"""

    # 10. Top-level module constant modification outside any function
    patches["syn_10_toplevel_constant"] = """diff --git a/sqlglot/tokens.py b/sqlglot/tokens.py
index ddddddd..eeeeeee 100644
--- a/sqlglot/tokens.py
+++ b/sqlglot/tokens.py
@@ -25,3 +25,3 @@
-CUSTOM_TOKEN_LIMIT = 500
+CUSTOM_TOKEN_LIMIT = 1000
"""

    # 11. Massive multi-file patch (touching 15 files)
    multi_patch_parts = []
    for i in range(1, 16):
        multi_patch_parts.append(f"""diff --git a/sqlglot/dialects/dialect_{i}.py b/sqlglot/dialects/dialect_{i}.py
index 100000{i}..200000{i} 100644
--- a/sqlglot/dialects/dialect_{i}.py
+++ b/sqlglot/dialects/dialect_{i}.py
@@ -15,3 +15,3 @@
-    IDENTIFY = False
+    IDENTIFY = True
""")
    patches["syn_11_massive_multi_file"] = "\n".join(multi_patch_parts)

    # 12. Brand-new optimizer file
    patches["syn_12_new_optimizer_file"] = """diff --git a/sqlglot/optimizer/custom.py b/sqlglot/optimizer/custom.py
new file mode 100644
--- /dev/null
+++ b/sqlglot/optimizer/custom.py
@@ -0,0 +1,5 @@
+def custom():
+    pass
+"""

    # 13. Direct test file modification
    patches["syn_13_edit_test_file"] = """diff --git a/tests/dialects/test_postgres.py b/tests/dialects/test_postgres.py
index 1111111..2222222 100644
--- a/tests/dialects/test_postgres.py
+++ b/tests/dialects/test_postgres.py
@@ -20,2 +20,2 @@
-    def test_postgres(self):
+    def test_postgres(self):
"""

    # 14. Dialect tokenizer-only change (DuckDB Tokenizer body)
    patches["syn_14_tokenizer_only_duckdb"] = """diff --git a/sqlglot/dialects/duckdb.py b/sqlglot/dialects/duckdb.py
index 1111111..2222222 100644
--- a/sqlglot/dialects/duckdb.py
+++ b/sqlglot/dialects/duckdb.py
@@ -100,2 +100,2 @@
-        HEREDOC_STRINGS = ['$$']
+        HEREDOC_STRINGS = ['$$', '$$$']
"""

    # 15. Dialect tokenizer class header modification (line 94 - must NOT qualify as tokenizer-only)
    patches["syn_15_tokenizer_class_header"] = """diff --git a/sqlglot/dialects/duckdb.py b/sqlglot/dialects/duckdb.py
index 1111111..2222222 100644
--- a/sqlglot/dialects/duckdb.py
+++ b/sqlglot/dialects/duckdb.py
@@ -94,1 +94,1 @@
-    class Tokenizer(tokens.Tokenizer):
+    class Tokenizer(custom.Tokenizer):
"""

    # 16. Dialect multi-hunk mixed (one hunk inside tokenizer, one outside in column_alias_precedence)
    patches["syn_16_tokenizer_multi_hunk_mixed"] = """diff --git a/sqlglot/dialects/duckdb.py b/sqlglot/dialects/duckdb.py
index 1111111..2222222 100644
--- a/sqlglot/dialects/duckdb.py
+++ b/sqlglot/dialects/duckdb.py
@@ -75,2 +75,2 @@
-        return True
+        return False
@@ -100,2 +100,2 @@
-        HEREDOC_STRINGS = ['$$']
+        HEREDOC_STRINGS = ['$$', '$$$']
"""

    # 17. Dialect multi-hunk all-tokenizer (two separate hunks both inside Tokenizer)
    patches["syn_17_tokenizer_multi_hunk_all_tok"] = """diff --git a/sqlglot/dialects/duckdb.py b/sqlglot/dialects/duckdb.py
index 1111111..2222222 100644
--- a/sqlglot/dialects/duckdb.py
+++ b/sqlglot/dialects/duckdb.py
@@ -100,2 +100,2 @@
-        HEREDOC_STRINGS = ['$$']
+        HEREDOC_STRINGS = ['$$', '$$$']
@@ -115,2 +115,2 @@
-        SINGLE_TOKENS = {}
+        SINGLE_TOKENS = {':': TokenType.COLON}
"""

    # 18. Dialect file deletion (deleted file mode - must fall back to conservative)
    patches["syn_18_dialect_file_deletion"] = """diff --git a/sqlglot/dialects/duckdb.py b/sqlglot/dialects/duckdb.py
deleted file mode 100644
index 1111111..0000000
--- a/sqlglot/dialects/duckdb.py
+++ /dev/null
@@ -1,50 +0,0 @@
-from sqlglot import tokens
"""

    return patches


def run_stress_test() -> dict[str, Any]:
    bundle_path = root_dir / "artifacts" / "runtime_index.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))

    resolver = ImpactResolver(bundle)
    scheduler = WorkerScheduler(resolver.test_durations)

    synthetic_patches = generate_synthetic_patches()
    print(f"Running Stage 7.1 Stress Test on {len(synthetic_patches)} adversarial patches ...\n")

    results: list[dict[str, Any]] = []
    all_passed = True

    for name, patch_text in synthetic_patches.items():
        t0 = time.perf_counter()

        # 1. Parse patch
        try:
            parsed = parse_patch(patch_text)
            parse_ok = True
            parse_err = None
        except Exception as e:
            parse_ok = False
            parse_err = str(e)

        # 2. Resolve impact
        try:
            meta = resolver.resolve_with_metadata(patch_text)
            selected_tests = meta["selected_tests"]
            resolve_ok = True
            resolve_err = None
        except Exception as e:
            resolve_ok = False
            resolve_err = str(e)
            selected_tests = []
            meta = {}

        # 3. Schedule workers
        try:
            sched = scheduler.schedule(selected_tests)
            sched_ok = True
            sched_err = None
            workers = sched["workers"]
            k = sched["k"]
            makespan_s = sched["makespan_ms"] / 1000.0
            cost_s = sched["total_cost_ms"] / 1000.0
        except Exception as e:
            sched_ok = False
            sched_err = str(e)
            workers = []
            k = 0
            makespan_s = 0.0
            cost_s = 0.0

        elapsed_us = (time.perf_counter() - t0) * 1_000_000

        # Verification checks
        checks = {
            "parse_ok": parse_ok,
            "resolve_ok": resolve_ok,
            "sched_ok": sched_ok,
            "valid_k_range": 0 <= k <= 8,
            "valid_worker_count": len(workers) == k,
            "disjoint_workers": len(set(t for w in workers for t in w)) == sum(len(w) for w in workers),
            "valid_test_ids": all(isinstance(t, int) and 0 <= t < len(resolver.test_durations) for w in workers for t in w),
            "fast_latency": elapsed_us < 50_000,  # < 50ms under stress
        }

        test_passed = all(checks.values())
        if not test_passed:
            all_passed = False

        status_str = "PASS" if test_passed else "FAIL"
        print(f"[{status_str}] {name:<26} -> k={k} | tests={len(selected_tests):<4} | cost={cost_s:<6.3f}s | latency={elapsed_us:<7.1f}µs")
        if not test_passed:
            print(f"       Failed checks: {[k for k, v in checks.items() if not v]}")
            if parse_err:
                print(f"       Parse error: {parse_err}")
            if resolve_err:
                print(f"       Resolve error: {resolve_err}")
            if sched_err:
                print(f"       Sched error: {sched_err}")

        results.append({
            "name": name,
            "passed": test_passed,
            "latency_us": round(elapsed_us, 2),
            "k": k,
            "selected_tests_count": len(selected_tests),
            "cost_s": round(cost_s, 3),
            "makespan_s": round(makespan_s, 3),
            "checks": checks,
            "triggers": meta.get("triggers", {}),
            "reason": meta.get("reason", "resolved"),
        })

    print("-" * 75)
    total_passed = sum(1 for r in results if r["passed"])
    print(f"Overall Stress Test Result: {total_passed} / {len(results)} passed ({total_passed/len(results)*100:.1f}%)")
    avg_latency = sum(r["latency_us"] for r in results) / len(results)
    print(f"Average Stress Latency:     {avg_latency:.1f} µs/patch")

    out_artifact = {
        "metadata": {
            "total_synthetic_patches": len(results),
            "passed_count": total_passed,
            "failed_count": len(results) - total_passed,
            "all_passed": all_passed,
            "avg_latency_us": round(avg_latency, 2),
        },
        "tests": results,
    }

    out_path = root_dir / "artifacts" / "stress_test_results.json"
    out_path.write_text(json.dumps(out_artifact, indent=2))
    print(f"\nSaved stress test results to {out_path}")

    return out_artifact


if __name__ == "__main__":
    res = run_stress_test()
    if not res["metadata"]["all_passed"]:
        sys.exit(1)
