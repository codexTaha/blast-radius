"""Stage 3.2 / Stage 6.2 — Multi-Tiered Impact Resolver.

Resolves Git diff patches into minimal, 100%-recall test selections using:
- Tier 0: Direct test file modifications
- Tier 1: Fixture dependency mappings
- Tier 2: Dialect hierarchy, component bindings, and optimizer fixture linkages
- Tier 3: Fine-grained AST symbol-level dynamic bytecode coverage
- Tier 4: Dynamic file-level execution coverage with optimizer multiprocessing bridge
- Tier 5: Transitive module import graph fallback for unprofiled / new modules
- Harmless comment/whitespace detection on non-sensitive paths
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.diff_parser import ParsedPatch, parse_patch

# Specific optimizer rules focusing on subqueries and CTEs:
# These execute AST-level transformations verified by test_optimizer.py
# and targeted subquery executor tests, safely bypassing heavy 21s TPC-DS execution.
SUBQUERY_OPTIMIZER_FILES = {
    "sqlglot/optimizer/eliminate_subqueries.py",
    "sqlglot/optimizer/eliminate_ctes.py",
    "sqlglot/optimizer/merge_subqueries.py",
    "sqlglot/optimizer/unnest_subqueries.py",
}


class ImpactResolver:
    """Multi-tiered test impact prediction engine."""

    def __init__(self, bundle_or_path: Path | str | dict[str, Any]) -> None:
        if isinstance(bundle_or_path, dict):
            bundle = bundle_or_path
        else:
            bundle_path = Path(bundle_or_path)
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))

        self.test_durations: list[int] = bundle["test_durations"]
        self.by_file: dict[str, list[int]] = bundle["by_file"]
        self.fixture_to_tests: dict[str, list[int]] = bundle["fixture_to_tests"]
        self.dialect_to_tests: dict[str, list[int]] = bundle["dialect_to_tests"]
        self.file_to_dialect: dict[str, str] = bundle["file_to_dialect"]
        self.core_engine_files: set[str] = set(bundle["core_engine_files"])
        self.file_to_tests: dict[str, list[int]] = bundle["file_to_tests"]
        self.symbol_to_tests: dict[str, list[int]] = bundle["symbol_to_tests"]
        self.file_symbols: dict[str, list[list[Any]]] = bundle["file_symbols"]
        self.test_dependents: dict[str, list[str]] = bundle["test_dependents"]
        self.executor_subquery_tests: list[int] = bundle.get("executor_subquery_tests", [])

    @staticmethod
    def _file_to_module(path: str) -> str:
        """Convert repository file path to Python import module name."""
        p = path[:-3] if path.endswith(".py") else path
        return p.replace("/", ".")

    @staticmethod
    def _symbol_to_key(path: str, qname: str) -> str:
        """Convert AST symbol qualified name to runtime bytecode symbol key."""
        func_name = qname.split(".")[-1]
        return f"{path}::{func_name}"

    def resolve(self, patch: str | ParsedPatch) -> list[int]:
        """Resolve a patch into a sorted list of selected test numbers."""
        return self.resolve_with_metadata(patch)["selected_tests"]

    def resolve_with_metadata(self, patch: str | ParsedPatch) -> dict[str, Any]:
        """Resolve patch and return selected test numbers with attribution metadata."""
        if isinstance(patch, str):
            parsed = parse_patch(patch)
        else:
            parsed = patch

        if parsed.is_empty:
            return {
                "selected_tests": [],
                "reason": "empty_patch",
                "estimated_runtime_ms": 0,
            }

        # Check comment-only on non-test, non-fixture, non-core files
        if parsed.is_comment_only:
            has_sensitive = False
            for f in parsed.files:
                if (
                    f.path.startswith("tests/")
                    or f.path.startswith("tests/fixtures/")
                    or f.path in self.core_engine_files
                ):
                    has_sensitive = True
                    break
            if not has_sensitive:
                return {
                    "selected_tests": [],
                    "reason": "comment_only_non_sensitive",
                    "estimated_runtime_ms": 0,
                }

        selected: set[int] = set()
        triggers: dict[str, list[str]] = {
            "tier0_test_files": [],
            "tier1_fixtures": [],
            "tier2_dialects": [],
            "tier3_symbols": [],
            "tier4_dynamic_files": [],
            "tier5_fallback": [],
        }

        for f in parsed.files:
            # For file renames, resolve impact against the source file (old_path) that exists in precomputed artifacts
            p = f.old_path if (f.is_rename and f.old_path) else f.path

            # Skip individual files that are comment-only and non-sensitive
            if f.is_comment_only:
                if not (
                    p.startswith("tests/")
                    or p.startswith("tests/fixtures/")
                    or p in self.core_engine_files
                ):
                    continue

            # Tier 0: Direct test file modified
            if p.startswith("tests/"):
                if p in self.by_file:
                    selected.update(self.by_file[p])
                    triggers["tier0_test_files"].append(p)
                elif p in self.fixture_to_tests:
                    selected.update(self.fixture_to_tests[p])
                    triggers["tier1_fixtures"].append(p)
                continue

            # Tier 1: Fixture file modified
            if p in self.fixture_to_tests:
                selected.update(self.fixture_to_tests[p])
                triggers["tier1_fixtures"].append(p)
                continue

            # Tier 2: Dialect component modified
            if p in self.file_to_dialect:
                d = self.file_to_dialect[p]

                # Safe tokenizer-only dialect filtering (Opportunity 1 - Option B):
                # When every modified range in a dialect file is conclusively enclosed within
                # a Tokenizer or JSONPathTokenizer class scope:
                # 1. Select tests/dialects/test_<dialect>.py
                # 2. Select tests/dialects/test_dialect.py
                # 3. Bypass dialect optimizer fixture expansion
                # 4. Bypass broad dialect-file fallback
                is_tokenizer_only = False
                if (
                    not f.is_new
                    and not f.is_deleted
                    and not f.is_rename
                    and not f.is_binary
                    and f.modified_ranges
                ):
                    tok_ranges = [
                        (s[0], s[1])
                        for s in self.file_symbols.get(p, [])
                        if s[2].endswith(".Tokenizer")
                        or s[2].endswith(".JSONPathTokenizer")
                        or ".Tokenizer." in s[2]
                        or ".JSONPathTokenizer." in s[2]
                    ]
                    if tok_ranges:
                        all_ranges_in_tok = True
                        for mod_start, mod_end in f.modified_ranges:
                            # Must be strictly inside class scope (s_start < mod_start <= mod_end <= s_end)
                            # so modifications to class header (name, bases, decorators) do not qualify.
                            in_tok = any(
                                s_start < mod_start and mod_end <= s_end
                                for s_start, s_end in tok_ranges
                            )
                            if not in_tok:
                                all_ranges_in_tok = False
                                break
                        if all_ranges_in_tok:
                            is_tokenizer_only = True

                if is_tokenizer_only:
                    candidate_test = f"tests/dialects/test_{d}.py"
                    if candidate_test in self.by_file:
                        selected.update(self.by_file[candidate_test])
                    dialect_test = "tests/dialects/test_dialect.py"
                    if dialect_test in self.by_file:
                        selected.update(self.by_file[dialect_test])
                    triggers["tier2_dialects"].append(f"{d}::tokenizer_only")
                    continue

                if d in self.dialect_to_tests:
                    selected.update(self.dialect_to_tests[d])
                    triggers["tier2_dialects"].append(d)

            # Tier 3: Symbol-level dynamic coverage
            syms = self.file_symbols.get(p, [])
            matched_sym_keys: set[str] = set()
            all_ranges_matched = bool(syms) and bool(f.modified_ranges)

            for mod_start, mod_end in f.modified_ranges:
                range_matched = False
                for start_line, end_line, qname in syms:
                    if not (end_line < mod_start or start_line > mod_end):
                        sym_key = self._symbol_to_key(p, qname)
                        matched_sym_keys.add(sym_key)
                        range_matched = True
                if not range_matched:
                    all_ranges_matched = False

            has_symbol_coverage = False
            for sym_key in matched_sym_keys:
                if sym_key in self.symbol_to_tests and self.symbol_to_tests[sym_key]:
                    selected.update(self.symbol_to_tests[sym_key])
                    triggers["tier3_symbols"].append(sym_key)
                    has_symbol_coverage = True

            # Tier 4: Dynamic file-level coverage
            # Modules with known multiprocessing child process execution (serde.py, optimizer/*)
            # must always pull file-level tests to avoid subprocess profiler gaps.
            if p == "sqlglot/serde.py" or p.startswith("sqlglot/optimizer/"):
                if p in self.file_to_tests:
                    selected.update(self.file_to_tests[p])
                    triggers["tier4_dynamic_files"].append(p)
            else:
                # If changes fell outside identified functions, or matched functions had no coverage,
                # fall back to file-level dynamic coverage.
                if not all_ranges_matched or not has_symbol_coverage:
                    if p in self.file_to_tests:
                        selected.update(self.file_to_tests[p])
                        triggers["tier4_dynamic_files"].append(p)

            # Optimizer integration test bridge:
            # Optimizer rules execute inside ProcessPoolExecutor in test_optimizer.py and test_executor.py
            if p.startswith("sqlglot/optimizer/"):
                if "tests/test_optimizer.py" in self.by_file:
                    selected.update(self.by_file["tests/test_optimizer.py"])
                    triggers["tier4_dynamic_files"].append("tests/test_optimizer.py")

                # Targeted executor coverage:
                # Subquery/CTE rules pull targeted subquery executor tests, safely bypassing Test 1106 (TPC-DS).
                # All other optimizer rules (joins, root optimizer, qualify, unmapped) retain full executor tests.
                if p in SUBQUERY_OPTIMIZER_FILES and self.executor_subquery_tests:
                    selected.update(self.executor_subquery_tests)
                    triggers["tier4_dynamic_files"].append("tests/test_executor.py[subquery_scoped]")
                elif "tests/test_executor.py" in self.by_file:
                    selected.update(self.by_file["tests/test_executor.py"])
                    triggers["tier4_dynamic_files"].append("tests/test_executor.py")

            # Tier 5: Fallback to static import graph (test_dependents) ONLY when file has ZERO dynamic coverage
            if p not in self.file_to_tests or len(self.file_to_tests[p]) == 0:
                mod_name = self._file_to_module(p)
                if mod_name in self.test_dependents:
                    for dep_test_file in self.test_dependents[mod_name]:
                        if dep_test_file in self.by_file:
                            selected.update(self.by_file[dep_test_file])
                            triggers["tier5_fallback"].append(dep_test_file)

        sorted_tests = sorted(selected)
        total_duration = sum(
            self.test_durations[t] for t in sorted_tests if t < len(self.test_durations)
        )

        return {
            "selected_tests": sorted_tests,
            "count": len(sorted_tests),
            "estimated_runtime_ms": total_duration,
            "triggers": triggers,
        }
