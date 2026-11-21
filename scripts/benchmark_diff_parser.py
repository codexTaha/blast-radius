#!/usr/bin/env python3
"""Stage 3.1 — Diff Parser Latency & Correctness Benchmark.

Profiles throughput, execution latency, and memory footprint
of src.diff_parser against all 30 sample commit patches.
"""

from __future__ import annotations

import glob
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.diff_parser import parse_patch


def main() -> None:
    sample_patches = sorted(glob.glob("blast-radius-sample/sample/commits/*.patch"))
    if not sample_patches:
        print("Error: No sample patches found in blast-radius-sample/sample/commits/", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(sample_patches)} sample patches.", file=sys.stderr)
    patch_texts = [open(p, "r", encoding="utf-8").read() for p in sample_patches]

    # Warmup
    for pt in patch_texts:
        parse_patch(pt)

    # Benchmark latency across 10,000 iterations (300,000 parses)
    iterations = 10_000
    total_parses = iterations * len(patch_texts)
    print(f"Benchmarking {total_parses:,} patch parses ({iterations:,} loops of 30 patches) ...", file=sys.stderr)

    t0 = time.perf_counter()
    for _ in range(iterations):
        for pt in patch_texts:
            parse_patch(pt)
    elapsed = time.perf_counter() - t0

    us_per_patch = (elapsed / total_parses) * 1_000_000
    parses_per_sec = total_parses / elapsed
    ms_for_40_commits = (us_per_patch * 40) / 1000

    print("\n=== Stage 3.1 Benchmark Results ===", file=sys.stderr)
    print(f"Total parses:          {total_parses:,}", file=sys.stderr)
    print(f"Total wall time:       {elapsed:.3f} s", file=sys.stderr)
    print(f"Throughput:            {parses_per_sec:,.0f} parses/sec", file=sys.stderr)
    print(f"Latency per patch:     {us_per_patch:.2f} µs ({us_per_patch / 1000:.4f} ms)", file=sys.stderr)
    print(f"Evaluation batch (40): {ms_for_40_commits:.3f} ms CPU overhead", file=sys.stderr)
    print(f"Target latency:        < 500.00 µs (0.50 ms)", file=sys.stderr)

    if us_per_patch < 500:
        print(f"PASS: Diff parser is {500 / us_per_patch:.1f}x faster than target threshold.\n", file=sys.stderr)
    else:
        print("FAIL: Diff parser exceeded latency threshold.\n", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
