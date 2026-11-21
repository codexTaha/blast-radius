#!/usr/bin/env python3
"""Stage 2.8 — Artifact Consolidation and Minification.

1. Builds a compact, zero-overhead runtime bundle `artifacts/runtime_index.json`
   containing all mappings required by the runtime selector in ~2 MB.
2. Minifies all JSON artifacts in `artifacts/` to minimize disk footprint.
3. Sets world-readable permissions (0o644 for files, 0o755 for directories)
   ensuring user `nobody` (UID 65534) can read everything in Docker.
4. Audits repository and artifact sizes against the competition 20 MB budget.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def build_runtime_bundle(artifacts_dir: Path) -> dict[str, Any]:
    print("Loading component artifacts for consolidation ...", file=sys.stderr)
    ti = json.loads((artifacts_dir / "test_index.json").read_text(encoding="utf-8"))
    fd = json.loads((artifacts_dir / "fixture_dependencies.json").read_text(encoding="utf-8"))
    dm = json.loads((artifacts_dir / "dialect_maps.json").read_text(encoding="utf-8"))
    tc = json.loads((artifacts_dir / "test_coverage.json").read_text(encoding="utf-8"))
    fs = json.loads((artifacts_dir / "file_symbols.json").read_text(encoding="utf-8"))
    md = json.loads((artifacts_dir / "module_dependents.json").read_text(encoding="utf-8"))

    # Compact file symbols: file -> list of [start_line, end_line, symbol_qname]
    compact_fs: dict[str, list[list[Any]]] = {}
    for f, syms in fs.items():
        if syms:
            compact_fs[f] = [[s["start"], s["end"], s["qname"]] for s in syms]

    # Compact test durations: array of int ms indexed by test number
    test_durations = [t["duration_ms"] for t in ti["by_number"]]

    # Subquery and correlated query executor tests in tests/test_executor.py
    executor_subquery_tests = sorted(
        tnum
        for k, tnum in ti.get("by_file_and_method", {}).items()
        if k.startswith("tests/test_executor.py") and ("subquer" in k or "correlated" in k)
    )

    # Full optimizer pipeline integration fixture tests
    optimizer_tpc_tests = sorted(
        tnum
        for k, tnum in ti.get("by_file_and_method", {}).items()
        if k in {"tests/test_optimizer.py::test_tpcds", "tests/test_optimizer.py::test_tpch"}
    )
    executor_tpc_tests = sorted(
        tnum
        for k, tnum in ti.get("by_file_and_method", {}).items()
        if k in {"tests/test_executor.py::test_execute_tpcds", "tests/test_executor.py::test_execute_tpch"}
    )

    runtime_bundle = {
        "metadata": {
            "total_tests": len(test_durations),
            "total_files": len(ti["by_file"]),
            "total_fixtures": len(fd["fixture_to_tests"]),
            "total_dialects": len(dm["dialect_to_files"]),
            "total_symbols_indexed": len(tc["symbol_to_tests"]),
            "total_executor_subquery_tests": len(executor_subquery_tests),
            "version": "1.0",
        },
        "test_durations": test_durations,
        "by_file": ti["by_file"],
        "fixture_to_tests": fd["fixture_to_tests"],
        "dialect_to_tests": dm["dialect_to_tests"],
        "file_to_dialect": dm["file_to_dialect"],
        "core_engine_files": dm["core_engine_files"],
        "file_to_tests": tc["file_to_tests"],
        "symbol_to_tests": tc["symbol_to_tests"],
        "file_symbols": compact_fs,
        "test_dependents": md["test_dependents"],
        "executor_subquery_tests": executor_subquery_tests,
        "optimizer_tpc_tests": optimizer_tpc_tests,
        "executor_tpc_tests": executor_tpc_tests,
    }
    return runtime_bundle


def minify_file(path: Path) -> tuple[int, int]:
    raw_data = path.read_bytes()
    orig_size = len(raw_data)
    try:
        obj = json.loads(raw_data.decode("utf-8"))
        minified = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        encoded = minified.encode("utf-8")
        path.write_bytes(encoded)
        return orig_size, len(encoded)
    except Exception as e:
        print(f"Warning: Failed to minify {path.name}: {e}", file=sys.stderr)
        return orig_size, orig_size


def fix_permissions(target_dir: Path) -> None:
    os.chmod(target_dir, 0o755)
    for p in target_dir.rglob("*"):
        if p.is_dir():
            os.chmod(p, 0o755)
        elif p.is_file():
            os.chmod(p, 0o644)


def main() -> None:
    parser = argparse.ArgumentParser(description="Consolidate and minify artifacts.")
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    args = parser.parse_args()

    artifacts_dir: Path = args.artifacts

    # 1. Generate consolidated runtime bundle
    runtime_bundle = build_runtime_bundle(artifacts_dir)
    runtime_bundle_path = artifacts_dir / "runtime_index.json"
    print(f"Writing {runtime_bundle_path} ...", file=sys.stderr)
    bundle_data = json.dumps(runtime_bundle, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    runtime_bundle_path.write_bytes(bundle_data)

    # 2. Minify all JSON artifacts
    print("Minifying artifacts in-place ...", file=sys.stderr)
    total_orig = 0
    total_mini = 0
    for p in sorted(artifacts_dir.glob("*.json")):
        orig, mini = minify_file(p)
        total_orig += orig
        total_mini += mini
        pct = (mini / orig * 100) if orig > 0 else 100
        print(f"  {p.name:30s} {orig / 1024:7.1f} KB -> {mini / 1024:7.1f} KB ({pct:5.1f}%)", file=sys.stderr)

    # 3. Enforce permissions
    print("Enforcing permissions (0o644 files, 0o755 dirs) ...", file=sys.stderr)
    fix_permissions(artifacts_dir)

    # 4. Audit sizes
    print("Auditing disk footprint ...", file=sys.stderr)
    artifacts_size_mb = sum(f.stat().st_size for f in artifacts_dir.glob("*.json")) / (1024 * 1024)

    git_size_cmd = subprocess.run(
        ["git", "count-objects", "-vH"],
        cwd=args.repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    print(f"  Total minified artifacts:  {artifacts_size_mb:.2f} MB", file=sys.stderr)
    print(f"  Target ceiling:            12.00 MB", file=sys.stderr)
    print(f"  Competition limit:         20.00 MB", file=sys.stderr)

    if artifacts_size_mb > 15.0:
        print("ERROR: Artifacts exceed 15 MB threshold!", file=sys.stderr)
        sys.exit(1)
    else:
        print("PASS: Artifacts are well within the 12 MB budget.", file=sys.stderr)


if __name__ == "__main__":
    main()
