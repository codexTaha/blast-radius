"""Stage 3.2 — Multi-Tiered Impact Resolver.

Resolves Git diff patches into minimal, 100%-recall test selections using:
- Tier 0: Direct test file modifications
- Tier 1: Fixture dependency mappings
- Tier 2: Dialect hierarchy, component bindings, and optimizer fixture linkages
- Tier 3: Fine-grained AST symbol-level dynamic bytecode coverage
- Tier 4: Transitive module import graph closures for core/internal engine modules
- Harmless comment/whitespace detection on non-sensitive paths
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.diff_parser import ParsedPatch, parse_patch


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

    @staticmethod
    def _file_to_module(path: str) -> str:
        """Convert repository file path to Python import module name."""
        p = path[:-3] if path.endswith(".py") else path
        return p.replace("/", ".")

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
            "tier4_transitive": [],
        }

        for f in parsed.files:
            p = f.path

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
                if d in self.dialect_to_tests:
                    selected.update(self.dialect_to_tests[d])
                    triggers["tier2_dialects"].append(d)

                if p in self.file_to_tests:
                    selected.update(self.file_to_tests[p])

                # Also inspect symbol-level coverage within dialect file
                syms = self.file_symbols.get(p, [])
                for start_line, end_line, qname in syms:
                    for mod_start, mod_end in f.modified_ranges:
                        if not (end_line < mod_start or start_line > mod_end):
                            if qname in self.symbol_to_tests:
                                selected.update(self.symbol_to_tests[qname])
                                triggers["tier3_symbols"].append(qname)
                continue

            # Tier 3: Symbol-level dynamic coverage
            syms = self.file_symbols.get(p, [])
            matched_symbols = set()
            for start_line, end_line, qname in syms:
                for mod_start, mod_end in f.modified_ranges:
                    if not (end_line < mod_start or start_line > mod_end):
                        matched_symbols.add(qname)
                        break

            for qname in matched_symbols:
                if qname in self.symbol_to_tests:
                    selected.update(self.symbol_to_tests[qname])
                    triggers["tier3_symbols"].append(qname)

            # Tier 4: Core / Non-dialect source modules -> Transitive import dependents
            mod_name = self._file_to_module(p)
            if mod_name in self.test_dependents:
                for dep_test_file in self.test_dependents[mod_name]:
                    if dep_test_file in self.by_file:
                        selected.update(self.by_file[dep_test_file])
                        triggers["tier4_transitive"].append(dep_test_file)

            if p in self.file_to_tests:
                selected.update(self.file_to_tests[p])

        sorted_tests = sorted(selected)
        total_duration = sum(self.test_durations[t] for t in sorted_tests if t < len(self.test_durations))

        return {
            "selected_tests": sorted_tests,
            "count": len(sorted_tests),
            "estimated_runtime_ms": total_duration,
            "triggers": triggers,
        }
