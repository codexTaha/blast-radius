#!/usr/bin/env python3
"""Stage 2.8 — Artifact Consolidation and Minification.

Builds a compact, zero-overhead runtime bundle `artifacts/runtime_index.json`
containing full-fidelity in-process + multiprocessing coverage, MRO class-dispatch maps,
factory-aware AST symbol spans, and multiline-string guards.
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

    mls_path = artifacts_dir / "multiline_strings.json"
    mls = json.loads(mls_path.read_text(encoding="utf-8")) if mls_path.exists() else {}

    # Compact file symbols:
    # file -> list of [start, body_start, end, qualname, kind_flag]
    # kind_flag: 0 = regular function/method, 1 = factory/import-called function, 2 = class
    compact_fs: dict[str, list[list[Any]]] = {}
    class_symbol_keys: set[str] = set()
    for f, syms in fs.items():
        if syms:
            entries: list[list[Any]] = []
            for s in syms:
                qual = s.get("qualname") or s["qname"]
                if s.get("type") == "class":
                    kflag = 2
                    class_symbol_keys.add(f"{f}::{s['name']}")
                elif s.get("type") == "attribute":
                    kflag = 3
                elif s.get("type") == "dict_key":
                    kflag = 4
                elif s.get("is_factory"):
                    kflag = 1
                else:
                    kflag = 0
                entries.append([
                    s["start"],
                    s.get("body_start", s["start"]),
                    s["end"],
                    qual,
                    kflag,
                ])
            compact_fs[f] = entries

    # Filter out one-time class-body import execution artifacts from symbol_to_tests
    # (classes are now accurately resolved via class_to_tests MRO dispatch!)
    clean_symbol_to_tests = {
        k: v
        for k, v in tc["symbol_to_tests"].items()
        if k not in class_symbol_keys
    }

    test_durations = [t["duration_ms"] for t in ti["by_number"]]

    runtime_bundle = {
        "metadata": {
            "total_tests": len(test_durations),
            "total_files": len(ti["by_file"]),
            "total_fixtures": len(fd["fixture_to_tests"]),
            "total_dialects": len(dm["dialect_to_files"]),
            "total_symbols_indexed": len(clean_symbol_to_tests),
            "total_classes_indexed": len(tc.get("class_to_tests", {})),
            "version": "2.0",
        },
        "test_durations": test_durations,
        "by_file": ti["by_file"],
        "by_id": ti["by_id"],
        "fixture_to_tests": fd["fixture_to_tests"],
        "dialect_to_tests": dm["dialect_to_tests"],
        "dialect_to_optimizer_tests": dm.get("dialect_to_optimizer_tests", {}),
        "file_to_dialect": dm["file_to_dialect"],
        "core_engine_files": dm["core_engine_files"],
        "file_to_tests": tc["file_to_tests"],
        "symbol_to_tests": clean_symbol_to_tests,
        "class_to_tests": tc.get("class_to_tests", {}),
        "file_symbols": compact_fs,
        "multiline_strings": mls,
        "test_dependents": md["test_dependents"],
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

    runtime_bundle = build_runtime_bundle(artifacts_dir)
    runtime_bundle_path = artifacts_dir / "runtime_index.json"
    print(f"Writing {runtime_bundle_path} ...", file=sys.stderr)
    bundle_data = json.dumps(runtime_bundle, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    runtime_bundle_path.write_bytes(bundle_data)

    print("Minifying artifacts in-place ...", file=sys.stderr)
    for p in sorted(artifacts_dir.glob("*.json")):
        orig, mini = minify_file(p)
        pct = (mini / orig * 100) if orig > 0 else 100
        print(f"  {p.name:30s} {orig / 1024:7.1f} KB -> {mini / 1024:7.1f} KB ({pct:5.1f}%)", file=sys.stderr)

    print("Enforcing permissions (0o644 files, 0o755 dirs) ...", file=sys.stderr)
    fix_permissions(artifacts_dir)

    artifacts_size_mb = sum(f.stat().st_size for f in artifacts_dir.glob("*.json")) / (1024 * 1024)
    print(f"  Total minified artifacts:  {artifacts_size_mb:.2f} MB (limit: 20.00 MB)", file=sys.stderr)
    if artifacts_size_mb > 15.0:
        print("ERROR: Artifacts exceed 15 MB threshold!", file=sys.stderr)
        sys.exit(1)
    print("PASS: Artifacts are well within budget.", file=sys.stderr)


if __name__ == "__main__":
    main()
