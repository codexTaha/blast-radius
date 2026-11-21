"""Stage 3.2 — Full-Fidelity Multi-Granularity Impact Resolver.

Resolves Git unified diff patches into minimal, 100%-recall test selections using:
1. Tokenize-verified Python '#' comment filtering (with multiline-string & PEP 263 guards)
2. Direct test-method, test-helper (Validator / helpers.py), and open-traced fixture resolution
3. Factory-aware & metaclass-safe AST function-body dynamic coverage (in-process + multiprocess)
4. Exact runtime MRO class-dispatch coverage (Expr, Dialect, Tokenizer, Parser, Generator, TypeAnnotator)
5. Full module dynamic coverage + package-aware static import-graph fallback
"""

from __future__ import annotations

import json
import marshal
from pathlib import Path
from typing import Any

from src.diff_parser import FileDiff, Hunk, ParsedPatch, parse_patch

SPECIAL_NON_BODY_METHODS = frozenset({
    "__init_subclass__",
    "__reduce__",
    "__reduce_ex__",
    "__getstate__",
    "__setstate__",
})


def _strip_strings_and_comments_for_balance(line: str) -> list[int]:
    """Return [paren_delta, bracket_delta, brace_delta] outside simple quotes and '#' comments."""
    p_d = b_d = c_d = 0
    in_quote: str | None = None
    escaped = False
    for ch in line:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if in_quote is not None:
            if ch == in_quote:
                in_quote = None
            continue
        if ch in ("'", '"'):
            in_quote = ch
            continue
        if ch == "#":
            break
        if ch == "(":
            p_d += 1
        elif ch == ")":
            p_d -= 1
        elif ch == "[":
            b_d += 1
        elif ch == "]":
            b_d -= 1
        elif ch == "{":
            c_d += 1
        elif ch == "}":
            c_d -= 1
    return [p_d, b_d, c_d]


def _hunk_has_balanced_delimiters(h: Hunk) -> bool:
    add_tot = [0, 0, 0]
    del_tot = [0, 0, 0]
    for _, txt in h.added_lines:
        if "===" in txt:
            return False
        d = _strip_strings_and_comments_for_balance(txt)
        add_tot[0] += d[0]
        add_tot[1] += d[1]
        add_tot[2] += d[2]
    for _, txt in h.deleted_lines:
        d = _strip_strings_and_comments_for_balance(txt)
        del_tot[0] += d[0]
        del_tot[1] += d[1]
        del_tot[2] += d[2]
    return add_tot == del_tot


class ImpactResolver:
    """Multi-granularity test impact prediction engine."""

    def __init__(self, bundle_or_path: Path | str | dict[str, Any]) -> None:
        if isinstance(bundle_or_path, dict):
            bundle = bundle_or_path
        else:
            bundle_path = Path(bundle_or_path)
            marshal_path = bundle_path.with_suffix(".marshal")
            if marshal_path.exists():
                bundle = marshal.loads(marshal_path.read_bytes())
            else:
                bundle = json.loads(bundle_path.read_text(encoding="utf-8"))

        self.test_durations: list[int] = bundle["test_durations"]
        self.by_file: dict[str, list[int]] = bundle["by_file"]
        self.by_id: dict[str, int] = bundle.get("by_id", {})
        self.fixture_to_tests: dict[str, list[int]] = bundle["fixture_to_tests"]
        self.dialect_to_tests: dict[str, list[int]] = bundle["dialect_to_tests"]
        self.file_to_dialect: dict[str, str] = bundle["file_to_dialect"]
        self.core_engine_files: set[str] = set(bundle["core_engine_files"])
        self.file_to_tests: dict[str, list[int]] = bundle["file_to_tests"]
        self.symbol_to_tests: dict[str, list[int]] = bundle["symbol_to_tests"]
        self.class_to_tests: dict[str, list[int]] = bundle.get("class_to_tests", {})
        self.file_symbols: dict[str, list[list[Any]]] = bundle["file_symbols"]
        self.multiline_strings: dict[str, list[list[int]]] = bundle.get("multiline_strings", {})
        self.test_dependents: dict[str, list[str]] = bundle["test_dependents"]

    @staticmethod
    def _file_to_module(path: str) -> str:
        """Convert repository file path (including package __init__.py) to Python module name."""
        p = path[:-3] if path.endswith(".py") else path
        if p.endswith("/__init__"):
            p = p[:-9]
        elif p == "__init__":
            return ""
        return p.replace("/", ".")

    @staticmethod
    def _symbol_to_key(path: str, qname: str) -> str:
        """Convert AST symbol qualified name to runtime bytecode symbol key."""
        func_name = qname.split(".")[-1]
        return f"{path}::{func_name}"

    def _is_safe_comment_only(self, f: FileDiff, path: str) -> bool:
        """Verify file diff is strictly a Python '#' comment outside all multiline string literals."""
        if not f.is_comment_only or not path.endswith(".py"):
            return False
        if path.startswith("tests/fixtures/"):
            return False
        mls = self.multiline_strings.get(path)
        if mls:
            for h in f.hunks:
                for del_ln, _ in h.deleted_lines:
                    if any(s_row <= del_ln <= e_row for s_row, e_row in mls):
                        return False
            for prev_ln, next_ln in f.insertion_anchors:
                if any(s_row <= prev_ln and next_ln <= e_row for s_row, e_row in mls):
                    return False
            for ln in f.modified_lines:
                if any(s_row < ln < e_row for s_row, e_row in mls):
                    return False
        return True

    def _is_syntactically_clean_py(self, f: FileDiff) -> bool:
        if f.is_new or f.is_deleted or f.is_rename or f.is_binary or not f.modified_ranges:
            return False
        return all(_hunk_has_balanced_delimiters(h) for h in f.hunks)

    def _effective_spans(self, f: FileDiff) -> list[tuple[int, int]]:
        spans = list(f.modified_ranges)
        if f.insertion_anchors:
            spans.extend(f.insertion_anchors)
        return spans

    def _add_file_closure(self, p: str, selected: set[int], triggers: dict[str, list[str]]) -> None:
        before_cnt = len(selected)

        if p in self.file_to_tests and self.file_to_tests[p]:
            selected.update(self.file_to_tests[p])
            triggers["tier4_dynamic_files"].append(p)

        if p in self.file_to_dialect:
            d = self.file_to_dialect[p]
            if d in self.dialect_to_tests:
                selected.update(self.dialect_to_tests[d])
                if d not in triggers["tier2_dialects"]:
                    triggers["tier2_dialects"].append(d)

        if p.startswith("sqlglot/typing/"):
            selected.update(self.by_file.get("tests/test_typing.py", []))

        if p.startswith("sqlglot/expressions/"):
            selected.update(self.by_file.get("tests/test_expressions.py", []))
            selected.update(self.by_file.get("tests/test_typing.py", []))
            selected.update(self.by_file.get("tests/test_serde.py", []))

        if p.startswith("sqlglot/optimizer/"):
            selected.update(self.by_file.get("tests/test_optimizer.py", []))
            selected.update(self.by_file.get("tests/test_executor.py", []))
            triggers["tier4_dynamic_files"].append("tests/test_optimizer.py")
            triggers["tier4_dynamic_files"].append("tests/test_executor.py")

        # Static import graph fallback when dynamic coverage is absent or file is __init__.py
        if p not in self.file_to_tests or not self.file_to_tests[p] or p.endswith("__init__.py"):
            mod_name = self._file_to_module(p)
            if mod_name and mod_name in self.test_dependents:
                for dep_test_file in self.test_dependents[mod_name]:
                    if dep_test_file in self.by_file:
                        selected.update(self.by_file[dep_test_file])
                        triggers["tier5_fallback"].append(dep_test_file)

        # Ultimate safety guard for unprofiled sqlglot/ or sqlglotc/ code files
        if len(selected) == before_cnt and p.startswith(("sqlglot/", "sqlglotc/")):
            if p.startswith("sqlglotc/"):
                selected.update(self.by_file.get("tests/test_sqlglotc.py", []))
            elif p != "sqlglot/__main__.py" and not p.endswith(".typed"):
                for fallback_tf in (
                    "tests/dialects/test_dialect.py",
                    "tests/test_transpile.py",
                    "tests/test_parser.py",
                    "tests/test_generator.py",
                    "tests/test_expressions.py",
                ):
                    selected.update(self.by_file.get(fallback_tf, []))
                triggers["tier5_fallback"].append("core_safety_net")

    def resolve(self, patch: str | ParsedPatch) -> list[int]:
        """Resolve a patch into a sorted list of selected test numbers."""
        return self.resolve_with_metadata(patch)["selected_tests"]

    def resolve_with_metadata(self, patch: str | ParsedPatch) -> dict[str, Any]:
        """Resolve patch and return selected test numbers with attribution metadata."""
        parsed = parse_patch(patch) if isinstance(patch, str) else patch

        if parsed.is_empty:
            return {
                "selected_tests": [],
                "reason": "empty_patch",
                "estimated_runtime_ms": 0,
            }

        if all(self._is_safe_comment_only(f, f.old_path if (f.is_rename and f.old_path) else f.path) for f in parsed.files):
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
            p = f.old_path if (f.is_rename and f.old_path) else f.path

            if self._is_safe_comment_only(f, p):
                continue

            # Tier 0 & Tier 1: Test files, test helpers, and fixtures
            if p.startswith("tests/") or p in self.fixture_to_tests:
                before_t = len(selected)
                if p in self.fixture_to_tests:
                    selected.update(self.fixture_to_tests[p])
                    triggers["tier1_fixtures"].append(p)

                if p.endswith(".py"):
                    # Try exact test-method scoping if p is a pure leaf test file
                    scoped_methods = False
                    file_suite = self.by_file.get(p, [])
                    dyn_cov = self.file_to_tests.get(p, [])
                    if (
                        file_suite
                        and self._is_syntactically_clean_py(f)
                        and set(dyn_cov).issubset(set(file_suite))
                    ):
                        syms = self.file_symbols.get(p, [])
                        method_nums: set[int] = set()
                        all_in_test_methods = True
                        for r_s, r_e in self._effective_spans(f):
                            matched_m = False
                            for s_entry in syms:
                                s_start, s_body, s_end, qual, kflag = s_entry
                                if kflag == 0 and ".test_" in f".{qual}" and s_body <= r_s and r_e <= s_end:
                                    node_id = f"{p}::{qual.replace('.', '::')}"
                                    tnum = self.by_id.get(node_id)
                                    if tnum is not None:
                                        method_nums.add(tnum)
                                        sym_k = self._symbol_to_key(p, qual)
                                        method_nums.update(self.symbol_to_tests.get(sym_k, []))
                                        matched_m = True
                                        break
                            if not matched_m:
                                all_in_test_methods = False
                                break
                        if all_in_test_methods and method_nums:
                            selected.update(method_nums)
                            triggers["tier0_test_files"].append(p)
                            scoped_methods = True

                    if not scoped_methods:
                        if p in self.by_file:
                            selected.update(self.by_file[p])
                            triggers["tier0_test_files"].append(p)
                        if p in self.file_to_tests:
                            selected.update(self.file_to_tests[p])
                        mod_name = self._file_to_module(p)
                        if mod_name in self.test_dependents:
                            for dep_tf in self.test_dependents[mod_name]:
                                selected.update(self.by_file.get(dep_tf, []))
                else:
                    if p in self.file_to_tests:
                        selected.update(self.file_to_tests[p])
                        if p not in triggers["tier1_fixtures"]:
                            triggers["tier1_fixtures"].append(p)
                    if p.startswith("tests/fixtures/optimizer/tpc-"):
                        selected.update(self.by_file.get("tests/test_executor.py", []))
                        selected.update(self.by_file.get("tests/test_optimizer.py", []))

                if len(selected) == before_t:
                    if "optimizer" in p:
                        selected.update(self.by_file.get("tests/test_optimizer.py", []))
                        selected.update(self.by_file.get("tests/test_executor.py", []))
                    elif "jsonpath" in p:
                        selected.update(self.by_file.get("tests/test_jsonpath.py", []))
                    else:
                        for tf_tests in self.by_file.values():
                            selected.update(tf_tests)
                continue

            # Source file resolution
            if p == "sqlglot/serde.py" or not p.endswith(".py") or not self._is_syntactically_clean_py(f):
                self._add_file_closure(p, selected, triggers)
                continue

            syms = self.file_symbols.get(p, [])
            if not syms:
                self._add_file_closure(p, selected, triggers)
                continue

            # Multi-granularity AST span resolution (innermost symbol first)
            spans = self._effective_spans(f)
            needs_file_closure = False
            all_tokenizer_only = True

            for r_s, r_e in spans:
                enclosing = [s for s in syms if s[0] <= r_s and r_e <= s[2]]
                if not enclosing:
                    needs_file_closure = True
                    all_tokenizer_only = False
                    break

                # Sort innermost first (smallest line span)
                enclosing.sort(key=lambda s: (s[2] - s[0]))
                span_resolved = False

                for entry in enclosing:
                    s_start, s_body, s_end, qual, kflag = entry[0], entry[1], entry[2], entry[3], entry[4]

                    # Level A: Strictly inside non-factory function/method body
                    if kflag == 0 and s_body <= r_s and r_e <= s_end:
                        leaf = qual.split(".")[-1]
                        if not qual.startswith("_Dialect") and leaf not in SPECIAL_NON_BODY_METHODS:
                            sym_key = f"{p}::{leaf}"
                            sym_tests = self.symbol_to_tests.get(sym_key)
                            if sym_tests:
                                selected.update(sym_tests)
                                if sym_key not in triggers["tier3_symbols"]:
                                    triggers["tier3_symbols"].append(sym_key)
                                if p.startswith("sqlglot/typing/"):
                                    selected.update(self.by_file.get("tests/test_typing.py", []))
                                if p in self.file_to_dialect:
                                    d = self.file_to_dialect[p]
                                    selected.update(self.by_file.get(f"tests/dialects/test_{d}.py", []))
                                    if d not in triggers["tier2_dialects"]:
                                        triggers["tier2_dialects"].append(d)
                                if not (".Tokenizer." in f".{qual}." or ".JSONPathTokenizer." in f".{qual}."):
                                    all_tokenizer_only = False
                                span_resolved = True
                                break

                    # Level B: Strictly inside class body (excluding class header/decorators)
                    elif kflag == 2 and s_start < r_s and r_e <= s_end and qual != "_Dialect":
                        ckey = f"{p}::{qual}"
                        cls_tests = self.class_to_tests.get(ckey)
                        is_tok_cls = qual.endswith(("Tokenizer", "JSONPathTokenizer")) or ".Tokenizer" in qual or ".JSONPathTokenizer" in qual
                        if not is_tok_cls:
                            all_tokenizer_only = False

                        if cls_tests:
                            selected.update(cls_tests)
                            if ckey not in triggers["tier3_symbols"]:
                                triggers["tier3_symbols"].append(ckey)
                            if p.startswith("sqlglot/expressions/"):
                                selected.update(self.by_file.get("tests/test_expressions.py", []))
                                selected.update(self.by_file.get("tests/test_typing.py", []))
                                selected.update(self.by_file.get("tests/test_serde.py", []))
                            if p in self.file_to_dialect:
                                d = self.file_to_dialect[p]
                                selected.update(self.by_file.get(f"tests/dialects/test_{d}.py", []))
                                if is_tok_cls:
                                    selected.update(self.by_file.get("tests/dialects/test_dialect.py", []))
                                else:
                                    selected.update(self.dialect_to_tests.get(d, []))
                                    if d not in triggers["tier2_dialects"]:
                                        triggers["tier2_dialects"].append(d)
                            span_resolved = True
                            break

                if not span_resolved:
                    needs_file_closure = True
                    all_tokenizer_only = False
                    break

            if all_tokenizer_only and not needs_file_closure and p in self.file_to_dialect:
                d = self.file_to_dialect[p]
                tok_tag = f"{d}::tokenizer_only"
                if tok_tag not in triggers["tier2_dialects"]:
                    triggers["tier2_dialects"].append(tok_tag)

            if needs_file_closure:
                self._add_file_closure(p, selected, triggers)

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
