"""Stage 10 — Optional, Zero-Side-Effect Diagnostic Telemetry Collector.

Records structured per-commit and aggregate decision telemetry made by our own
ImpactResolver and WorkerScheduler without modifying selected tests, worker
assignments, or official stdout JSONL.

Disabled by default. Enabled only via:
- CLI flags: `--telemetry` or `--telemetry-out <path>`
- Environment variables: `BLAST_RADIUS_TELEMETRY=1` or `BLAST_RADIUS_TELEMETRY_PATH=<path>`
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from src.diff_parser import ParsedPatch, parse_patch
from src.impact_resolver import ImpactResolver

HEAVY_OUTLIER_TESTS: dict[int, str] = {
    1490: "test_max_nodes(28817ms)",
    1106: "test_execute_tpcds(21454ms)",
    1224: "test_generator_memory(6686ms)",
    1107: "test_optimize_tpcds(3863ms)",
    1440: "test_tpch(2232ms)",
    1295: "test_qualify_columns(1963ms)",
}


def is_telemetry_enabled(cli_enabled: bool = False, cli_out_path: str | None = None) -> bool:
    """Return True only when telemetry is explicitly requested via CLI or environment variable."""
    if cli_enabled or cli_out_path:
        return True
    env_flag = os.environ.get("BLAST_RADIUS_TELEMETRY", "").strip().lower()
    if env_flag in ("1", "true", "yes", "stderr"):
        return True
    if os.environ.get("BLAST_RADIUS_TELEMETRY_PATH", "").strip():
        return True
    return False


class TelemetryCollector:
    """Collects per-commit and aggregate decision diagnostics from ImpactResolver and WorkerScheduler."""

    def __init__(
        self,
        resolver: ImpactResolver,
        out_path: str | Path | None = None,
        emit_stderr: bool = False,
    ) -> None:
        self.resolver = resolver
        env_path = os.environ.get("BLAST_RADIUS_TELEMETRY_PATH", "").strip()
        self.out_path: Path | None = Path(out_path) if out_path else (Path(env_path) if env_path else None)
        env_flag = os.environ.get("BLAST_RADIUS_TELEMETRY", "").strip().lower()
        self.emit_stderr: bool = emit_stderr or (env_flag == "stderr")
        self.commit_records: list[dict[str, Any]] = []

        # Build set of class symbol keys ("file::ClassName") to distinguish broad class fallbacks from method/key hits
        self._class_sym_keys: set[str] = set()
        for f_path, syms in getattr(resolver, "file_symbols", {}).items():
            for s in syms:
                if len(s) >= 5 and s[4] == 2:
                    self._class_sym_keys.add(f"{f_path}::{s[3]}")

    def _compute_category_tests(
        self,
        parsed: ParsedPatch,
        triggers: dict[str, list[str]],
        selected_set: set[int],
    ) -> tuple[dict[str, int], int, list[str], list[str]]:
        """Estimate per-category test counts, fallback-exclusive tests, fallback tags, and broad-selection reasons."""
        tier0_tests: set[int] = set()
        for tf in triggers.get("tier0_test_files", []):
            tier0_tests.update(self.resolver.by_file.get(tf, []))
        tier0_tests &= selected_set

        tier1_tests: set[int] = set()
        for fx in triggers.get("tier1_fixtures", []):
            tier1_tests.update(self.resolver.fixture_to_tests.get(fx, []))
            tier1_tests.update(self.resolver.file_to_tests.get(fx, []))
        tier1_tests &= selected_set

        tier2_tests: set[int] = set()
        for d_tag in triggers.get("tier2_dialects", []):
            d_name = d_tag.split("::", 1)[0]
            tier2_tests.update(self.resolver.dialect_to_tests.get(d_name, []))
        tier2_tests &= selected_set

        tier3_direct_tests: set[int] = set()
        tier3_class_fallback_tests: set[int] = set()
        fallback_categories: list[str] = []
        broad_reasons: list[str] = []

        for sym_tag in triggers.get("tier3_symbols", []):
            if sym_tag in self._class_sym_keys:
                c_tests = set(self.resolver.class_to_tests.get(sym_tag, [])) & selected_set
                tier3_class_fallback_tests.update(c_tests)
                fallback_categories.append(f"tier3_class_fallback:{sym_tag}")
                if len(c_tests) >= 50:
                    broad_reasons.append(f"class_level_fallback:{sym_tag}({len(c_tests)}_tests)")
            elif "[" in sym_tag and sym_tag.endswith("]"):
                k_tests = set(self.resolver.key_to_tests.get(sym_tag, [])) & selected_set
                tier3_direct_tests.update(k_tests)
            else:
                q_tests = self.resolver.qual_symbol_to_tests.get(sym_tag)
                if isinstance(q_tests, list) and len(q_tests) == 1 and isinstance(q_tests[0], int) and q_tests[0] < 0:
                    leaf = sym_tag.split("::", 1)[-1].split(".")[-1]
                    f_prefix = sym_tag.split("::", 1)[0]
                    s_tests = set(self.resolver.symbol_to_tests.get(f"{f_prefix}::{leaf}", [])) & selected_set
                    tier3_direct_tests.update(s_tests)
                    fallback_categories.append(f"tier3_high_freq_sentinel:{sym_tag}")
                    if len(s_tests) >= 50:
                        broad_reasons.append(f"high_freq_symbol_sentinel:{sym_tag}({len(s_tests)}_tests)")
                elif isinstance(q_tests, list):
                    tier3_direct_tests.update(set(q_tests) & selected_set)

        tier4_tests: set[int] = set()
        for df in triggers.get("tier4_dynamic_files", []):
            f_tests = (
                set(self.resolver.file_to_tests.get(df, []))
                | set(self.resolver.by_file.get(df, []))
            ) & selected_set
            tier4_tests.update(f_tests)
            fallback_categories.append(f"tier4_file_closure:{df}")
            broad_reasons.append(f"file_closure_fallback:{df}({len(f_tests)}_tests)")

        tier5_tests: set[int] = set()
        for fb in triggers.get("tier5_fallback", []):
            fallback_categories.append(f"tier5_fallback:{fb}")
            if fb == "core_safety_net":
                for c_tf in (
                    "tests/dialects/test_dialect.py",
                    "tests/test_transpile.py",
                    "tests/test_parser.py",
                    "tests/test_generator.py",
                    "tests/test_expressions.py",
                ):
                    tier5_tests.update(set(self.resolver.by_file.get(c_tf, [])) & selected_set)
                broad_reasons.append(f"core_safety_net({len(tier5_tests)}_tests)")
            else:
                fb_t = set(self.resolver.by_file.get(fb, [])) & selected_set
                tier5_tests.update(fb_t)
                broad_reasons.append(f"static_import_dependent:{fb}({len(fb_t)}_tests)")

        # Tests not explained by specific key/qual lookups in tier3 (e.g. statement-unit or transitive cross_attr_deps)
        tier3_total_tests = selected_set - (tier0_tests | tier1_tests | tier4_tests | tier5_tests)
        if triggers.get("tier3_symbols"):
            tier3_direct_tests |= tier3_total_tests - tier3_class_fallback_tests

        non_fallback_tests = tier0_tests | tier1_tests | tier3_direct_tests
        fallback_only_tests = selected_set - non_fallback_tests

        for h_tnum, h_label in sorted(HEAVY_OUTLIER_TESTS.items()):
            if h_tnum in selected_set:
                broad_reasons.append(f"includes_heavy_test:#{h_tnum}_{h_label}")

        if len(parsed.files) > 1:
            broad_reasons.append(f"multi_file_patch({len(parsed.files)}_files)")
        total_hunks = sum(len(f.hunks) for f in parsed.files)
        if total_hunks > 1:
            broad_reasons.append(f"multi_hunk_patch({total_hunks}_hunks)")

        category_counts = {
            "tier0_test_files": len(tier0_tests),
            "tier1_fixtures": len(tier1_tests),
            "tier2_dialects": len(tier2_tests),
            "tier3_direct_symbols": len(tier3_direct_tests),
            "tier3_class_fallbacks": len(tier3_class_fallback_tests),
            "tier4_dynamic_files": len(tier4_tests),
            "tier5_fallback": len(tier5_tests),
        }
        return category_counts, len(fallback_only_tests), fallback_categories, broad_reasons

    def record_commit(
        self,
        commit_id: str,
        patch_text: str,
        resolution_meta: dict[str, Any],
        sched_result: dict[str, Any],
    ) -> None:
        """Record telemetry for a single processed commit. Never raises."""
        try:
            parsed = parse_patch(patch_text)
            selected_tests: list[int] = resolution_meta.get("selected_tests", [])
            selected_set = set(selected_tests)
            triggers: dict[str, list[str]] = resolution_meta.get(
                "triggers",
                {
                    "tier0_test_files": [],
                    "tier1_fixtures": [],
                    "tier2_dialects": [],
                    "tier3_symbols": [],
                    "tier4_dynamic_files": [],
                    "tier5_fallback": [],
                },
            )
            reason = resolution_meta.get("reason", "resolved")
            if parsed.is_empty:
                parse_outcome = "empty_patch"
            elif reason == "comment_only_non_sensitive" or parsed.is_comment_only:
                parse_outcome = "comment_only_non_sensitive"
            elif parsed.is_whitespace_only:
                parse_outcome = "whitespace_only"
            else:
                parse_outcome = "parsed_ok"

            cat_counts, fallback_added_cnt, fallback_cats, broad_reasons = self._compute_category_tests(
                parsed, triggers, selected_set
            )

            workers: list[list[int]] = sched_result.get("workers", [])
            k: int = sched_result.get("k", len(workers))
            durations = self.resolver.test_durations
            worker_durations_ms = [
                sum(durations[t] for t in w if 0 <= t < len(durations)) for w in workers
            ]
            makespan_ms = max(worker_durations_ms, default=0)
            busiest_idx = (
                worker_durations_ms.index(makespan_ms) if worker_durations_ms else None
            )
            worker_overhead_ms = 2000 * k
            total_cost_ms = makespan_ms + worker_overhead_ms
            seq_duration_ms = sum(durations[t] for t in selected_tests if 0 <= t < len(durations))

            triggered_cats = [cat for cat, items in triggers.items() if items]
            direct_rels = [
                s for s in triggers.get("tier3_symbols", []) if s not in self._class_sym_keys
            ]
            transitive_rels = (
                [s for s in triggers.get("tier3_symbols", []) if s in self._class_sym_keys]
                + triggers.get("tier2_dialects", [])
                + triggers.get("tier4_dynamic_files", [])
                + triggers.get("tier5_fallback", [])
            )

            record = {
                "commit": commit_id,
                "commit_processing": {
                    "commit_id": commit_id,
                    "changed_file_count": len(parsed.files),
                    "changed_files": [f.path for f in parsed.files],
                    "hunk_count": sum(len(f.hunks) for f in parsed.files),
                    "total_added_lines": parsed.total_added,
                    "total_deleted_lines": parsed.total_deleted,
                    "patch_parsing_outcome": parse_outcome,
                    "changed_symbols_identified": list(triggers.get("tier3_symbols", [])),
                },
                "dependency_resolution": {
                    "dependency_categories_triggered": triggered_cats,
                    "triggers": triggers,
                    "direct_relationships": direct_rels,
                    "transitive_and_fallback_relationships": transitive_rels,
                    "tests_selected_by_category": cat_counts,
                    "fallback_categories_triggered": fallback_cats,
                    "broad_selection_reasons": broad_reasons if len(selected_tests) >= 25 or fallback_cats else [],
                },
                "test_selection": {
                    "total_selected_tests": len(selected_tests),
                    "unique_test_count": len(selected_set),
                    "test_selection_counts_by_category": cat_counts,
                    "tests_added_by_fallback_paths": fallback_added_cnt,
                    "estimated_sequential_duration_ms": seq_duration_ms,
                },
                "scheduling": {
                    "worker_count": k,
                    "tests_assigned_per_worker": [len(w) for w in workers],
                    "estimated_worker_durations_ms": worker_durations_ms,
                    "busiest_worker_index": busiest_idx,
                    "makespan_ms": makespan_ms,
                    "makespan_s": round(makespan_ms / 1000.0, 3),
                    "worker_overhead_ms": worker_overhead_ms,
                    "worker_overhead_s": round(worker_overhead_ms / 1000.0, 3),
                    "total_simulated_cost_ms": total_cost_ms,
                    "total_simulated_cost_s": round(total_cost_ms / 1000.0, 3),
                },
            }
            self.commit_records.append(record)
        except Exception:
            pass

    def build_report(self) -> dict[str, Any]:
        """Build the complete structured telemetry report including the aggregate summary."""
        total_commits = len(self.commit_records)
        total_sel = sum(r["test_selection"]["total_selected_tests"] for r in self.commit_records)
        max_rec = max(
            self.commit_records,
            key=lambda r: r["test_selection"]["total_selected_tests"],
            default=None,
        )
        fallback_counter: Counter[str] = Counter()
        category_cost_ms: dict[str, int] = defaultdict(int)
        category_commit_count: Counter[str] = Counter()
        worker_dist = [0] * 9

        agg_makespan_ms = 0
        agg_overhead_ms = 0

        for r in self.commit_records:
            k = r["scheduling"]["worker_count"]
            if 0 <= k <= 8:
                worker_dist[k] += 1
            cost_ms = r["scheduling"]["total_simulated_cost_ms"]
            agg_makespan_ms += r["scheduling"]["makespan_ms"]
            agg_overhead_ms += r["scheduling"]["worker_overhead_ms"]

            for fb in r["dependency_resolution"]["fallback_categories_triggered"]:
                fb_kind = fb.split(":", 1)[0]
                fallback_counter[fb_kind] += 1
                fallback_counter[fb] += 1

            cats = r["dependency_resolution"]["dependency_categories_triggered"]
            if not cats:
                category_commit_count["none_zero_tests"] += 1
            for c in cats:
                category_commit_count[c] += 1
                category_cost_ms[c] += cost_ms

        top_expensive = sorted(
            self.commit_records,
            key=lambda r: (
                -r["scheduling"]["total_simulated_cost_ms"],
                -r["test_selection"]["total_selected_tests"],
                r["commit"],
            ),
        )[:10]

        aggregate = {
            "processed_commits": total_commits,
            "total_selected_tests": total_sel,
            "average_tests_selected": round(total_sel / total_commits, 2) if total_commits else 0.0,
            "max_tests_selected": max_rec["test_selection"]["total_selected_tests"] if max_rec else 0,
            "max_tests_commit": max_rec["commit"] if max_rec else None,
            "worker_distribution": {f"k={i}": worker_dist[i] for i in range(9) if worker_dist[i] > 0},
            "most_frequent_fallback_categories": [
                {"category": k, "count": v} for k, v in fallback_counter.most_common(15)
            ],
            "most_expensive_dependency_categories": [
                {
                    "category": k,
                    "commit_count": category_commit_count[k],
                    "cumulative_simulated_cost_s": round(v / 1000.0, 3),
                }
                for k, v in sorted(category_cost_ms.items(), key=lambda x: (-x[1], x[0]))
            ],
            "aggregate_makespan_s": round(agg_makespan_ms / 1000.0, 3),
            "aggregate_worker_overhead_s": round(agg_overhead_ms / 1000.0, 3),
            "aggregate_simulated_score_s": round((agg_makespan_ms + agg_overhead_ms) / 1000.0, 3),
            "top_expensive_commits": [
                {
                    "commit": r["commit"],
                    "selected_tests": r["test_selection"]["total_selected_tests"],
                    "worker_count": r["scheduling"]["worker_count"],
                    "makespan_s": r["scheduling"]["makespan_s"],
                    "worker_overhead_s": r["scheduling"]["worker_overhead_s"],
                    "total_simulated_cost_s": r["scheduling"]["total_simulated_cost_s"],
                    "changed_files": r["commit_processing"]["changed_files"],
                    "fallback_categories": r["dependency_resolution"]["fallback_categories_triggered"],
                    "broad_selection_reasons": r["dependency_resolution"]["broad_selection_reasons"],
                }
                for r in top_expensive
            ],
        }

        return {
            "aggregate_report": aggregate,
            "commits": self.commit_records,
        }

    def finalize_and_emit(self) -> dict[str, Any] | None:
        """Finalize report and write to configured file path and/or stderr. Never raises."""
        try:
            report = self.build_report()
            if self.out_path is not None:
                self.out_path.parent.mkdir(parents=True, exist_ok=True)
                self.out_path.write_text(
                    json.dumps(report, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            if self.emit_stderr:
                agg = report["aggregate_report"]
                print(
                    f"[TELEMETRY] Aggregate: commits={agg['processed_commits']} | "
                    f"tests={agg['total_selected_tests']} (avg={agg['average_tests_selected']}, "
                    f"max={agg['max_tests_selected']} on {agg['max_tests_commit']}) | "
                    f"sim={agg['aggregate_simulated_score_s']:.3f}s "
                    f"(makespan={agg['aggregate_makespan_s']:.3f}s + workers={agg['aggregate_worker_overhead_s']:.3f}s)",
                    file=sys.stderr,
                )
            return report
        except Exception:
            return None
