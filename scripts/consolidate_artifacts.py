#!/usr/bin/env python3
"""Stage 2.8 — Artifact Consolidation and Minification.

Builds a compact, zero-overhead runtime bundle `artifacts/runtime_index.json`
containing full-fidelity in-process + multiprocessing coverage, MRO class-dispatch maps,
per-key registry maps, safe runtime attribute reader maps, factory-aware AST symbol spans,
and multiline-string guards.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

HIGH_FREQ_KEY_SENTINEL_CAP = 45
HIGH_FREQ_STMT_SENTINEL_CAP = 86


def _compress_key_and_word_maps(
    raw_key_to_tests: dict[str, list[int]],
    known_keys_by_attr: dict[str, set[str]],
) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """Split into exact per-(class, attr, key) for known keys + global word_to_tests for newly-added keys."""
    compact_keys: dict[str, list[int]] = {}
    word_sets: dict[str, set[int]] = defaultdict(set)
    high_freq_words: set[str] = set()

    for k, v in raw_key_to_tests.items():
        if not v:
            continue
        lb = k.rfind("[")
        if lb == -1 or not k.endswith("]"):
            continue
        ck = k[lb + 1 : -1]
        prefix = k[:lb]
        attr_name = prefix.rsplit(".", 1)[-1]

        if ck in known_keys_by_attr.get(attr_name, set()):
            if v == [-1] or len(v) > HIGH_FREQ_KEY_SENTINEL_CAP:
                compact_keys[k] = [-1]
            else:
                compact_keys[k] = v
        else:
            # Record in global word_to_tests so newly-added keys resolve via class_to_tests[cls] & word_to_tests[w]
            w: str | None = None
            if ck.startswith("s:"):
                w = ck[2:].upper()
            elif ck.startswith("w:"):
                w = ck[2:].upper()
            if w and len(w) <= 30 and "\n" not in w and "'" not in w and '"' not in w:
                if w in high_freq_words:
                    continue
                if v == [-1]:
                    high_freq_words.add(w)
                    word_sets.pop(w, None)
                else:
                    s = word_sets[w]
                    s.update(v)
                    if len(s) > HIGH_FREQ_KEY_SENTINEL_CAP:
                        high_freq_words.add(w)
                        word_sets.pop(w, None)

    word_to_tests: dict[str, list[int]] = {w: sorted(s) for w, s in sorted(word_sets.items())}
    for w in sorted(high_freq_words):
        word_to_tests[w] = [-1]
    return compact_keys, word_to_tests


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

    am_path = artifacts_dir / "attr_metadata.json"
    am = json.loads(am_path.read_text(encoding="utf-8")) if am_path.exists() else {}

    # Compact file symbols:
    # file -> list of [start, body_start, end, qualname, kind_flag]
    # kind_flag: 0 = regular function/method, 1 = factory/import-called function,
    #            2 = class, 3 = attribute, 4 = dict_key
    compact_fs: dict[str, list[list[Any]]] = {}
    class_symbol_keys: set[str] = set()
    defined_attr_names: set[str] = set()
    known_keys_by_attr: dict[str, set[str]] = defaultdict(set)

    for f, syms in fs.items():
        if syms:
            entries: list[list[Any]] = []
            for s in syms:
                qual = s.get("qualname") or s["qname"]
                stype = s.get("type")
                if stype == "class":
                    kflag = 2
                    class_symbol_keys.add(f"{f}::{s['name']}")
                elif stype == "attribute":
                    kflag = 3
                    defined_attr_names.add(qual.rsplit(".", 1)[-1])
                elif stype == "dict_key":
                    kflag = 4
                    lb = qual.rfind("[")
                    if lb != -1 and qual.endswith("]"):
                        raw_ck = qual[lb + 1 : -1]
                        aname = qual[:lb].rsplit(".", 1)[-1]
                        if raw_ck.startswith("s:") and aname != "NUMERIC_ESCAPES":
                            known_keys_by_attr[aname].add(f"s:{raw_ck[2:].upper()}")
                            if aname in ("SHOW_PARSERS", "SET_PARSERS"):
                                fw = raw_ck[2:].strip().upper().split(" ")[0]
                                if fw:
                                    known_keys_by_attr[aname].add(f"w:{fw}")
                        else:
                            known_keys_by_attr[aname].add(raw_ck)
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

    # Filter out one-time class-body import execution artifacts and non-test helper methods from symbol_to_tests
    clean_symbol_to_tests = {
        k: v
        for k, v in tc["symbol_to_tests"].items()
        if k not in class_symbol_keys and (not k.startswith("tests/") or ("::test_" in k and len(v) > 1))
    }

    raw_qual_syms: dict[str, list[int]] = tc.get("qual_symbol_to_tests", {})
    clean_qual_symbol_to_tests: dict[str, list[int]] = {}
    for k, v in raw_qual_syms.items():
        if k in class_symbol_keys or not v:
            continue
        if v == [-1] or len(v) > HIGH_FREQ_STMT_SENTINEL_CAP:
            clean_qual_symbol_to_tests[k] = [-1]
        else:
            clean_qual_symbol_to_tests[k] = v

    # Build precomputed statement-unit coverage: "file::qualname" -> [[u_start, u_end, tests_or_minus1], ...]
    raw_line_to_tests: dict[str, dict[str, list[int]]] = tc.get("line_to_tests", {})
    func_stmt_units: dict[str, list[list[int]]] = am.get("func_stmt_units", {})
    func_stmt_cov: dict[str, list[list[Any]]] = {}
    kept_func_lines: dict[str, set[str]] = defaultdict(set)

    for fq_key, units in sorted(func_stmt_units.items()):
        if not units or "::" not in fq_key:
            continue
        rel_f, _ = fq_key.split("::", 1)
        f_lines = raw_line_to_tests.get(rel_f, {})
        out_units: list[list[Any]] = []
        for u_s, u_e in units:
            u_tests: set[int] = set()
            overflow = False
            for ln in range(u_s, u_e + 1):
                s_ln = str(ln)
                kept_func_lines[rel_f].add(s_ln)
                lt = f_lines.get(s_ln)
                if lt == [-1]:
                    overflow = True
                    break
                if lt:
                    u_tests.update(lt)
                    if len(u_tests) > HIGH_FREQ_STMT_SENTINEL_CAP:
                        overflow = True
                        break
            out_units.append([u_s, u_e, [-1] if overflow else sorted(u_tests)])
        # Omit straight-line functions where every statement unit equals function-level coverage
        q_base = clean_qual_symbol_to_tests.get(fq_key, [])
        if any(u[2] != q_base for u in out_units):
            func_stmt_cov[fq_key] = out_units

    # Prune raw line_to_tests in test_coverage.json to only function-body lines so repo stays compact
    pruned_line_to_tests: dict[str, dict[str, list[int]]] = {}
    for rel_f, allowed_lns in sorted(kept_func_lines.items()):
        f_lines = raw_line_to_tests.get(rel_f, {})
        sub = {
            s_ln: ([-1] if (v == [-1] or len(v) > HIGH_FREQ_STMT_SENTINEL_CAP) else v)
            for s_ln, v in f_lines.items()
            if s_ln in allowed_lns and v
        }
        if sub:
            pruned_line_to_tests[rel_f] = sub

    class_to_tests: dict[str, list[int]] = tc.get("class_to_tests", {})
    active_gen_to_tests: dict[str, list[int]] = tc.get("active_gen_to_tests", {})

    # Only retain symbol_to_tests entries needed at runtime (tests/*, escape helpers, and [-1] fallbacks)
    needed_sym_keys: set[str] = {
        "sqlglot/generator.py::escape_str",
        "sqlglot/generator.py::bytestring_sql",
    }
    for qk, qv in clean_qual_symbol_to_tests.items():
        if qv == [-1] and "::" in qk:
            rf, rq = qk.split("::", 1)
            needed_sym_keys.add(f"{rf}::{rq.split('.')[-1]}")
    runtime_symbol_to_tests = {
        k: v
        for k, v in clean_symbol_to_tests.items()
        if (k.startswith("tests/") and "::test_" in k) or k in needed_sym_keys
    }
    expr_sets: dict[str, set[int]] = defaultdict(set)
    for ckey, tnums in class_to_tests.items():
        if ckey.startswith(("sqlglot/expressions/", "sqlglot/jsonpath.py::")):
            ename = ckey.split("::")[-1].split(".")[-1]
            expr_sets[ename].update(tnums)
    expr_to_tests = {k: sorted(v) for k, v in sorted(expr_sets.items())}

    # Build safe runtime attribute -> reader test numbers
    safe_attr_readers: dict[str, list[str]] = am.get("safe_attr_readers", {})
    attr_reader_tests: dict[str, list[int]] = {}
    for attr_name in sorted(defined_attr_names):
        if attr_name in safe_attr_readers:
            tset: set[int] = set()
            for rsym in safe_attr_readers[attr_name]:
                tset.update(clean_symbol_to_tests.get(rsym, []))
            if len(tset) <= 250:
                attr_reader_tests[attr_name] = sorted(tset)

    # Compress key_to_tests + global word_to_tests
    raw_key_to_tests: dict[str, list[int]] = tc.get("key_to_tests", {})
    existing_word_to_tests: dict[str, list[int]] = tc.get("word_to_tests", {})
    compact_key_to_tests, new_word_to_tests = _compress_key_and_word_maps(raw_key_to_tests, known_keys_by_attr)
    for inv_k in am.get("invariant_arg_types_keys", []):
        compact_key_to_tests[inv_k] = []
    merged_words = dict(existing_word_to_tests)
    merged_words.update(new_word_to_tests)
    if (
        len(compact_key_to_tests) < len(raw_key_to_tests)
        or "word_to_tests" not in tc
        or pruned_line_to_tests != raw_line_to_tests
        or len(clean_symbol_to_tests) < len(tc["symbol_to_tests"])
    ):
        tc["symbol_to_tests"] = clean_symbol_to_tests
        tc["key_to_tests"] = compact_key_to_tests
        tc["word_to_tests"] = merged_words
        tc["qual_symbol_to_tests"] = clean_qual_symbol_to_tests
        tc["line_to_tests"] = pruned_line_to_tests
        tc["metadata"]["covered_registry_keys"] = len(compact_key_to_tests)
        (artifacts_dir / "test_coverage.json").write_text(
            json.dumps(tc, separators=(",", ":"), ensure_ascii=False), encoding="utf-8"
        )

    test_durations = [t["duration_ms"] for t in ti["by_number"]]

    runtime_bundle = {
        "metadata": {
            "total_tests": len(test_durations),
            "total_files": len(ti["by_file"]),
            "total_fixtures": len(fd["fixture_to_tests"]),
            "total_dialects": len(dm["dialect_to_files"]),
            "total_symbols_indexed": len(clean_symbol_to_tests),
            "total_qual_symbols_indexed": len(clean_qual_symbol_to_tests),
            "total_stmt_funcs_indexed": len(func_stmt_cov),
            "total_classes_indexed": len(class_to_tests),
            "total_registry_keys_indexed": len(compact_key_to_tests),
            "total_safe_attrs_indexed": len(attr_reader_tests),
            "version": "2.2",
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
        "symbol_to_tests": runtime_symbol_to_tests,
        "qual_symbol_to_tests": clean_qual_symbol_to_tests,
        "func_stmt_cov": func_stmt_cov,
        "class_to_tests": class_to_tests,
        "active_gen_to_tests": active_gen_to_tests,
        "expr_to_tests": expr_to_tests,
        "key_to_tests": compact_key_to_tests,
        "word_to_tests": merged_words,
        "attr_reader_tests": attr_reader_tests,
        "cross_attr_deps": am.get("cross_attr_deps", {}),
        "test_attr_refs": am.get("test_attr_refs", {}),
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
    if artifacts_size_mb > 18.0:
        print("ERROR: Artifacts exceed 18 MB threshold!", file=sys.stderr)
        sys.exit(1)
    print("PASS: Artifacts are well within budget.", file=sys.stderr)


if __name__ == "__main__":
    main()
