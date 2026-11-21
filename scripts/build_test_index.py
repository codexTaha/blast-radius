#!/usr/bin/env python3
"""Stage 2.4 — Test Inventory and ID Mapping.

Analyzes tests.json to construct canonical mappings:
- by_number: test metadata & integer millisecond duration
- by_id: Pytest node ID -> test number
- by_file: test file path -> list of test numbers
- by_file_and_method: file::method -> test number
- file_stats: runtime profile per test file

Produces artifacts/test_index.json.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any


def duration_milliseconds(seconds: float) -> int:
    return math.floor(seconds * 1000 + 0.5)


def build_test_index(tests_json_path: Path) -> dict[str, Any]:
    raw_data = json.loads(tests_json_path.read_text(encoding="utf-8"))
    raw_tests = raw_data["tests"]
    skipped_tests = raw_data.get("skipped", [])

    by_number: list[dict[str, Any]] = []
    by_id: dict[str, int] = {}
    by_file: dict[str, list[int]] = {}
    by_file_and_method: dict[str, int] = {}
    file_runtimes: dict[str, float] = {}
    file_heaviest: dict[str, dict[str, Any]] = {}

    for t in raw_tests:
        num = t["number"]
        node_id = t["id"]
        file_path = t["file"]
        sec = t["seconds"]
        dur_ms = duration_milliseconds(sec)

        parts = node_id.split("::")
        class_name = parts[1] if len(parts) >= 2 else None
        method_name = parts[2] if len(parts) >= 3 else parts[-1]

        info = {
            "number": num,
            "id": node_id,
            "file": file_path,
            "class": class_name,
            "method": method_name,
            "seconds": sec,
            "duration_ms": dur_ms,
        }

        by_number.append(info)
        by_id[node_id] = num

        if file_path not in by_file:
            by_file[file_path] = []
            file_runtimes[file_path] = 0.0
            file_heaviest[file_path] = {"number": num, "seconds": sec, "id": node_id}

        by_file[file_path].append(num)
        file_runtimes[file_path] += sec

        if sec > file_heaviest[file_path]["seconds"]:
            file_heaviest[file_path] = {"number": num, "seconds": sec, "id": node_id}

        by_file_and_method[f"{file_path}::{method_name}"] = num

    # Compute per-file statistics
    file_stats: dict[str, dict[str, Any]] = {}
    for f, test_nums in by_file.items():
        file_stats[f] = {
            "test_count": len(test_nums),
            "total_seconds": round(file_runtimes[f], 3),
            "total_ms": sum(by_number[n]["duration_ms"] for n in test_nums),
            "heaviest_test": file_heaviest[f],
        }

    total_seconds = sum(t["seconds"] for t in by_number)
    total_ms = sum(t["duration_ms"] for t in by_number)

    return {
        "metadata": {
            "total_tests": len(by_number),
            "total_files": len(by_file),
            "skipped_count": len(skipped_tests),
            "total_seconds": round(total_seconds, 3),
            "total_duration_ms": total_ms,
        },
        "by_number": by_number,
        "by_id": by_id,
        "by_file": by_file,
        "by_file_and_method": by_file_and_method,
        "file_stats": file_stats,
        "skipped": skipped_tests,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build canonical test index from tests.json.")
    parser.add_argument("--tests", type=Path, default=Path("blast-radius-sample/sample/tests.json"))
    parser.add_argument("--out", type=Path, default=Path("artifacts/test_index.json"))
    args = parser.parse_args()

    print(f"Reading test specifications from {args.tests} ...", file=sys.stderr)
    test_index = build_test_index(args.tests)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(test_index, f, indent=2)

    meta = test_index["metadata"]
    print(f"Test index complete!", file=sys.stderr)
    print(f"  Active tests:   {meta['total_tests']}", file=sys.stderr)
    print(f"  Distinct files: {meta['total_files']}", file=sys.stderr)
    print(f"  Total runtime:  {meta['total_seconds']:.3f} s ({meta['total_duration_ms']} ms)", file=sys.stderr)
    print(f"  Output path:    {args.out} ({args.out.stat().st_size / 1024:.1f} KB)", file=sys.stderr)


if __name__ == "__main__":
    main()
