#!/usr/bin/env python3
"""Stage 3.2 — Impact Resolver Latency & Throughput Benchmark.

Profiles end-to-end impact prediction speed across all 30 sample commits.
"""

from __future__ import annotations

import glob
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.impact_resolver import ImpactResolver


def main() -> None:
    bundle_path = Path("artifacts/runtime_index.json")
    if not bundle_path.exists():
        print(f"Error: {bundle_path} does not exist", file=sys.stderr)
        sys.exit(1)

    t_init_0 = time.perf_counter()
    resolver = ImpactResolver(bundle_path)
    init_ms = (time.perf_counter() - t_init_0) * 1000

    sample_patches = sorted(glob.glob("blast-radius-sample/sample/commits/*.patch"))
    patch_texts = [open(p, "r", encoding="utf-8").read() for p in sample_patches]

    # Warmup
    for pt in patch_texts:
        resolver.resolve(pt)

    # Benchmark over 1,000 iterations (30,000 predictions)
    iterations = 1_000
    total_predictions = iterations * len(patch_texts)
    print(f"Benchmarking {total_predictions:,} impact resolutions ({iterations:,} loops of 30 commits) ...", file=sys.stderr)

    t0 = time.perf_counter()
    for _ in range(iterations):
        for pt in patch_texts:
            resolver.resolve(pt)
    elapsed = time.perf_counter() - t0

    us_per_commit = (elapsed / total_predictions) * 1_000_000
    ms_for_40_commits = (us_per_commit * 40) / 1000
    total_cpu_including_init = init_ms + ms_for_40_commits

    print("\n=== Stage 3.2 Benchmark Results ===", file=sys.stderr)
    print(f"Engine init time:      {init_ms:.2f} ms", file=sys.stderr)
    print(f"Total predictions:     {total_predictions:,}", file=sys.stderr)
    print(f"Throughput:            {total_predictions / elapsed:,.0f} predictions/sec", file=sys.stderr)
    print(f"Prediction latency:    {us_per_commit:.2f} µs ({us_per_commit / 1000:.4f} ms per commit)", file=sys.stderr)
    print(f"40-commit prediction:  {ms_for_40_commits:.2f} ms", file=sys.stderr)
    print(f"Total batch CPU time:  {total_cpu_including_init:.2f} ms", file=sys.stderr)

    if total_cpu_including_init < 200:
        print(f"PASS: Impact resolution CPU overhead is negligible ({total_cpu_including_init:.1f} ms < 200 ms budget).\n", file=sys.stderr)
    else:
        print("FAIL: Impact resolution CPU overhead exceeded threshold.\n", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
