#!/usr/bin/env python3
"""Stage 2.9 — Master Reproducibility Pipeline.

Regenerates all static analysis, symbol indexing, dependency graph,
fixture mapping, dialect hierarchy, and consolidated runtime bundle
from scratch deterministically.

Usage:
    python3 scripts/generate_artifacts.py [--run-coverage]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path


def run_step(step_name: str, cmd: list[str]) -> None:
    print(f"\n[PIPELINE] === {step_name} ===", file=sys.stderr)
    print(f"Command: {' '.join(cmd)}", file=sys.stderr)
    t0 = time.perf_counter()
    res = subprocess.run(cmd, check=False)
    elapsed = time.perf_counter() - t0
    if res.returncode != 0:
        print(f"[ERROR] Step '{step_name}' failed with code {res.returncode}", file=sys.stderr)
        sys.exit(res.returncode)
    print(f"[SUCCESS] {step_name} completed in {elapsed:.2f}s", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Master static analysis and artifact generation pipeline.")
    parser.add_argument("--repo", type=Path, default=Path("sqlglot"))
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    parser.add_argument("--sample-dir", type=Path, default=Path("blast-radius-sample/sample"))
    parser.add_argument("--run-coverage", action="store_true", help="Re-run dynamic coverage via analyzer Docker container")
    args = parser.parse_args()

    pipeline_start = time.perf_counter()
    args.artifacts.mkdir(parents=True, exist_ok=True)

    # Stage 2.1: Repository Inventory
    run_step(
        "Stage 2.1: Inventory Repository",
        [
            sys.executable,
            "scripts/inventory_repo.py",
            "--repo", str(args.repo),
            "--out", str(args.artifacts / "repo_inventory.json"),
        ],
    )

    # Stage 2.2: Symbol Extraction
    run_step(
        "Stage 2.2: Extract Symbols & AST Spans",
        [
            sys.executable,
            "scripts/extract_symbols.py",
            "--repo", str(args.repo),
            "--file-symbols-out", str(args.artifacts / "file_symbols.json"),
            "--symbol-index-out", str(args.artifacts / "symbol_index.json"),
        ],
    )

    # Stage 2.3: Module Import Graph
    run_step(
        "Stage 2.3: Build Module Import Graph",
        [
            sys.executable,
            "scripts/build_import_graph.py",
            "--inventory", str(args.artifacts / "repo_inventory.json"),
            "--imports-out", str(args.artifacts / "module_imports.json"),
            "--dependents-out", str(args.artifacts / "module_dependents.json"),
        ],
    )

    # Stage 2.4: Test Index
    run_step(
        "Stage 2.4: Build Test Index",
        [
            sys.executable,
            "scripts/build_test_index.py",
            "--tests", str(args.sample_dir / "tests.json"),
            "--out", str(args.artifacts / "test_index.json"),
        ],
    )

    # Stage 2.5: Dynamic Coverage (optional re-run, otherwise verified present)
    coverage_file = args.artifacts / "test_coverage.json"
    if args.run_coverage:
        run_step(
            "Stage 2.5: Re-collect Dynamic Coverage",
            [
                "docker", "run", "--rm",
                "-v", f"{Path.cwd().resolve()}:/workspace",
                "-w", "/workspace",
                "blast-radius-analyzer:latest",
                "python3", "scripts/collect_coverage.py",
                "--repo", "/workspace/sqlglot",
                "--test-index", "/workspace/artifacts/test_index.json",
                "--out", "/workspace/artifacts/test_coverage.json",
            ],
        )
    else:
        if not coverage_file.exists():
            print(f"[ERROR] {coverage_file} does not exist and --run-coverage was not specified.", file=sys.stderr)
            sys.exit(1)
        print(f"\n[PIPELINE] Stage 2.5: Reusing verified dynamic coverage ({coverage_file.stat().st_size / (1024*1024):.2f} MB)", file=sys.stderr)

    # Stage 2.6: Fixture Mapping
    run_step(
        "Stage 2.6: Map Fixture Dependencies",
        [
            sys.executable,
            "scripts/map_fixtures.py",
            "--repo", str(args.repo),
            "--test-index", str(args.artifacts / "test_index.json"),
            "--out", str(args.artifacts / "fixture_dependencies.json"),
        ],
    )

    # Stage 2.7: Resource & Dialect Mapping
    run_step(
        "Stage 2.7: Map Resources & Dialect Hierarchy",
        [
            sys.executable,
            "scripts/map_resources.py",
            "--repo", str(args.repo),
            "--test-index", str(args.artifacts / "test_index.json"),
            "--fixture-deps", str(args.artifacts / "fixture_dependencies.json"),
            "--out", str(args.artifacts / "dialect_maps.json"),
        ],
    )

    # Stage 2.8: Consolidation, Minification & Permission Verification
    run_step(
        "Stage 2.8: Consolidate & Minify Artifacts",
        [
            sys.executable,
            "scripts/consolidate_artifacts.py",
            "--artifacts", str(args.artifacts),
            "--repo-root", ".",
        ],
    )

    total_time = time.perf_counter() - pipeline_start
    print(f"\n=======================================================", file=sys.stderr)
    print(f"[PIPELINE COMPLETE] All artifacts regenerated in {total_time:.2f}s", file=sys.stderr)
    print(f"=======================================================\n", file=sys.stderr)


if __name__ == "__main__":
    main()
