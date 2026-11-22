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
HIGH_FREQ_STAGE2_CAP = 220
RESCUE_COST_SAVING_MS = 1500.0
STMT_LINE_COST_SAVING_MS = 100.0
HEAVY_SENTINEL_BITS: tuple[tuple[int, int], ...] = (
    (1, 1490),  # test_max_nodes (28.817 s)
    (2, 1106),  # test_execute_tpcds (21.454 s)
    (4, 1224),  # test_generator_memory (6.686 s)
)
_COMPREHENSION_LOCALS = (".<locals>.<genexpr>", ".<locals>.<listcomp>", ".<locals>.<setcomp>", ".<locals>.<dictcomp>")


def _decode_sentinel_mask(val: Any) -> int | None:
    if isinstance(val, list) and len(val) == 1:
        x = val[0]
        if isinstance(x, int) and not isinstance(x, bool) and -8 <= x <= -1:
            return -x - 1
    return None


def _is_sentinel_list(val: Any) -> bool:
    return isinstance(val, list) and any(
        not isinstance(x, int) or isinstance(x, bool) or x < 0 for x in val
    )


def _estimate_sim_cost_ms(tests: set[int], durations: list[int]) -> float:
    if not tests:
        return 0.0
    durs = sorted((durations[t] for t in tests), reverse=True)
    total = sum(durs)
    mx = durs[0]
    best = float("inf")
    for k in range(1, min(8, len(durs)) + 1):
        loads = [0] * k
        for d in durs:
            m_idx = min(range(k), key=loads.__getitem__)
            loads[m_idx] += d
        cost = max(loads[0], mx, total / k) + 2000.0 * k
        if cost < best:
            best = cost
    return best


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


def build_runtime_bundle(
    artifacts_dir: Path,
    repo_root: Path = Path("."),
    heavy_gt_path: Path | None = None,
) -> dict[str, Any]:
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

    if heavy_gt_path is not None and heavy_gt_path.exists():
        hg_data = json.loads(heavy_gt_path.read_text(encoding="utf-8"))
        heavy_lines_raw: dict[str, dict[str, list[int]]] = hg_data.get("lines", {})
        heavy_qsyms_raw: dict[str, list[str]] = hg_data.get("qsyms", {})
    else:
        heavy_lines_raw = tc.get("heavy_lines", {})
        heavy_qsyms_raw = tc.get("heavy_qsyms", {})

    # Compact file symbols:
    # file -> list of [start, body_start, end, qualname, kind_flag]
    # kind_flag: 0 = regular function/method, 1 = factory/import-called function,
    #            2 = class, 3 = attribute, 4 = dict_key
    compact_fs: dict[str, list[list[Any]]] = {}
    class_symbol_keys: set[str] = set()
    defined_attr_names: set[str] = set()
    leaf_class_names: set[str] = set()
    method_leaf_names: set[str] = set()
    valid_func_qkeys: set[str] = set()
    func_spans_by_qkey: dict[str, tuple[int, int]] = {}
    known_keys_by_attr: dict[str, set[str]] = defaultdict(set)

    for f, syms in fs.items():
        if syms:
            entries: list[list[Any]] = []
            for s in syms:
                qual = s.get("qualname") or s["qname"]
                stype = s.get("type")
                sname = s.get("name") or qual.split(".")[-1]
                if stype == "class":
                    kflag = 2
                    class_symbol_keys.add(f"{f}::{sname}")
                    if not f.startswith("tests/") and s["end"] - s["start"] <= 3:
                        leaf_class_names.add(sname)
                elif stype == "attribute":
                    kflag = 3
                    if "." in qual:
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
                    fq = f"{f}::{qual}"
                    valid_func_qkeys.add(fq)
                    func_spans_by_qkey[fq] = (s["start"], s["end"])
                else:
                    kflag = 0
                    method_leaf_names.add(qual.split(".")[-1])
                    fq = f"{f}::{qual}"
                    valid_func_qkeys.add(fq)
                    func_spans_by_qkey[fq] = (s["start"], s["end"])
                entries.append([
                    s["start"],
                    s.get("body_start", s["start"]),
                    s["end"],
                    qual,
                    kflag,
                ])
            compact_fs[f] = entries

    test_durations = [t["duration_ms"] for t in ti["by_number"]]
    heavy_test_set = {1490, 1106, 1224, 1107, 1123, 1428, 1440}
    raw_line_to_tests: dict[str, dict[str, list[int]]] = tc.get("line_to_tests", {})
    max_nodes_lines_raw: dict[str, list[int]] = tc.get("max_nodes_lines", {})
    max_nodes_lines_set: dict[str, set[int]] = {
        rf: set(lns) for rf, lns in max_nodes_lines_raw.items()
    }

    raw_qual_syms: dict[str, list[int]] = tc.get("qual_symbol_to_tests", {})
    heavy_qsym_sets: dict[int, set[str]] = {}
    heavy_line_sets: dict[int, dict[str, set[int]]] = {}
    for _, htid in HEAVY_SENTINEL_BITS:
        hk = str(htid)
        hq_set = set(heavy_qsyms_raw.get(hk, []))
        for qk, qv in raw_qual_syms.items():
            if not _is_sentinel_list(qv) and htid in qv:
                hq_set.add(qk)
        heavy_qsym_sets[htid] = hq_set

        hl_map: dict[str, set[int]] = defaultdict(set)
        for rf, lns in heavy_lines_raw.get(hk, {}).items():
            hl_map[rf].update(lns)
        for rf, l_dict in raw_line_to_tests.items():
            for s_ln, lt in l_dict.items():
                if not _is_sentinel_list(lt) and htid in lt:
                    try:
                        hl_map[rf].add(int(s_ln))
                    except ValueError:
                        pass
        if htid == 1490:
            for rf, lns in max_nodes_lines_raw.items():
                hl_map[rf].update(lns)
        heavy_line_sets[htid] = {rf: s for rf, s in hl_map.items() if s}

    # Materialize deterministic sorted heavy maps back so Pass 1 and Pass 2 see identical inputs
    if heavy_gt_path is not None or heavy_lines_raw or heavy_qsyms_raw:
        heavy_qsyms_raw = {
            str(htid): sorted(heavy_qsym_sets[htid])
            for _, htid in HEAVY_SENTINEL_BITS
            if heavy_qsym_sets[htid]
        }
        heavy_lines_raw = {
            str(htid): {rf: sorted(s) for rf, s in sorted(heavy_line_sets[htid].items())}
            for _, htid in HEAVY_SENTINEL_BITS
            if heavy_line_sets[htid]
        }

    heavy_qsyms_loaded: dict[int, bool] = {
        htid: bool(heavy_qsym_sets[htid]) for _, htid in HEAVY_SENTINEL_BITS
    }
    heavy_lines_loaded: dict[int, bool] = {
        htid: bool(heavy_line_sets[htid]) for _, htid in HEAVY_SENTINEL_BITS
    }

    _src_line_cache: dict[str, list[str]] = {}

    def _is_header_line(rel_f: str, ln: int) -> bool:
        lines = _src_line_cache.get(rel_f)
        if lines is None:
            fpath = repo_root / rel_f
            if not fpath.exists() and (repo_root / "sqlglot" / rel_f).exists():
                fpath = repo_root / "sqlglot" / rel_f
            try:
                lines = fpath.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                lines = []
            _src_line_cache[rel_f] = lines
        if 1 <= ln <= len(lines):
            return lines[ln - 1].lstrip().startswith(("def ", "async def ", "class ", "@"))
        return True

    def _htid_touches_lines(htid: int, rel_f: str, lines_iter: Any, f_lines: dict[str, list[int]]) -> bool:
        if not heavy_lines_loaded.get(htid, False):
            return True
        hl = heavy_line_sets[htid].get(rel_f, ())
        for ln in lines_iter:
            if ln in hl:
                return True
            lt = f_lines.get(str(ln))
            if lt and not _is_sentinel_list(lt) and htid in lt:
                return True
        return False

    # Filter out one-time class-body import execution artifacts and non-test helper methods from symbol_to_tests
    clean_symbol_to_tests = {
        k: v
        for k, v in tc["symbol_to_tests"].items()
        if k not in class_symbol_keys and (not k.startswith("tests/") or ("::test_" in k and len(v) > 1))
    }

    clean_qual_symbol_to_tests: dict[str, list[int]] = {}
    for k, v in raw_qual_syms.items():
        if k in class_symbol_keys or not v:
            continue
        is_valid_q = k in valid_func_qkeys or (
            ".<locals>." in k
            and not k.endswith(_COMPREHENSION_LOCALS)
            and k.split(".<locals>.", 1)[0] in valid_func_qkeys
        )
        if not is_valid_q:
            continue
        if _is_sentinel_list(v) or len(v) > HIGH_FREQ_STAGE2_CAP:
            excl_mask = _decode_sentinel_mask(v) or 0
            rf = k.split("::", 1)[0]
            rf_lines = raw_line_to_tests.get(rf, {})
            s_start, s_end = func_spans_by_qkey.get(k, (0, 0))
            locals_prefix = k + ".<locals>."
            for bit, htid in HEAVY_SENTINEL_BITS:
                if excl_mask & bit:
                    continue
                if not heavy_qsyms_loaded.get(htid, False) or not heavy_lines_loaded.get(htid, False):
                    continue
                if not _is_sentinel_list(v) and htid in v:
                    continue
                hq_set = heavy_qsym_sets[htid]
                if k in hq_set or any(hq.startswith(locals_prefix) for hq in hq_set):
                    continue
                if s_end >= s_start > 0 and _htid_touches_lines(htid, rf, range(s_start, s_end + 1), rf_lines):
                    continue
                excl_mask |= bit
            clean_qual_symbol_to_tests[k] = [-(1 + excl_mask)]
        else:
            clean_qual_symbol_to_tests[k] = v

    class_to_tests: dict[str, list[int]] = tc.get("class_to_tests", {})
    active_gen_to_tests: dict[str, list[int]] = tc.get("active_gen_to_tests", {})

    def _get_func_entry_set(fq_key: str) -> set[int]:
        qv = clean_qual_symbol_to_tests.get(fq_key)
        if qv is None:
            return set()
        q_mask = _decode_sentinel_mask(qv)
        if q_mask is not None:
            rf, rq = fq_key.split("::", 1)
            leaf = rq.split(".")[-1]
            ft = set(clean_symbol_to_tests.get(f"{rf}::{leaf}", []))
            if "." in rq and ".<locals>." not in rq:
                ck = f"{rf}::{rq.rsplit('.', 1)[0]}"
                if ck in class_to_tests:
                    ft &= set(class_to_tests[ck])
            if q_mask:
                ft -= {htid for bit, htid in HEAVY_SENTINEL_BITS if (q_mask & bit)}
            return ft
        return set(qv)

    # Build precomputed statement-unit coverage: "file::qualname" -> [[u_start, u_end, tests_or_sentinel], ...]
    # and intra-unit continuation line table: "file" -> {"lineno": tests_or_sentinel}
    func_stmt_units: dict[str, list[list[int]]] = am.get("func_stmt_units", {})
    func_stmt_cov: dict[str, list[list[Any]]] = {}
    all_func_stmt_cov: dict[str, list[list[Any]]] = {}
    stmt_line_cov: dict[str, dict[str, list[int]]] = defaultdict(dict)
    kept_func_lines: dict[str, set[str]] = defaultdict(set)
    kept_220_lines: dict[str, set[str]] = defaultdict(set)

    for fq_key, units in sorted(func_stmt_units.items()):
        if not units or "::" not in fq_key:
            continue
        rel_f, _ = fq_key.split("::", 1)
        f_lines = raw_line_to_tests.get(rel_f, {})
        if not any(str(ln) in f_lines for u_s, u_e in units for ln in range(u_s, u_e + 1)):
            continue
        func_tests = _get_func_entry_set(fq_key)
        f_cost = _estimate_sim_cost_ms(func_tests, test_durations) if func_tests else 0.0
        f_heavy = func_tests & heavy_test_set
        q_mask = _decode_sentinel_mask(clean_qual_symbol_to_tests.get(fq_key)) or 0
        out_units: list[list[Any]] = []
        unit_meta: list[tuple[int, int, set[int]]] = []
        all_unit_lns: set[int] = set()

        for u_s, u_e in units:
            # Guard 4A-2: Record all lines in [u_s, u_e] before any early overflow break
            for ln in range(u_s, u_e + 1):
                kept_func_lines[rel_f].add(str(ln))
                all_unit_lns.add(ln)
            u_tests: set[int] = set()
            overflow_220 = False
            for ln in range(u_s, u_e + 1):
                s_ln = str(ln)
                lt = f_lines.get(s_ln)
                if _is_sentinel_list(lt):
                    overflow_220 = True
                    break
                if lt:
                    u_tests.update(lt)
                    if len(u_tests) > HIGH_FREQ_STAGE2_CAP:
                        overflow_220 = True
                        break

            keep_exact_unit = False
            if not overflow_220:
                if len(u_tests) <= HIGH_FREQ_STMT_SENTINEL_CAP:
                    keep_exact_unit = True
                elif func_tests:
                    u_eff = u_tests & func_tests
                    if len(u_eff) < len(func_tests):
                        if (f_heavy - (u_eff & heavy_test_set)) or (_estimate_sim_cost_ms(u_eff, test_durations) + RESCUE_COST_SAVING_MS <= f_cost):
                            keep_exact_unit = True
                            for ln in range(u_s, u_e + 1):
                                kept_220_lines[rel_f].add(str(ln))

            if keep_exact_unit:
                u_val: list[int] = sorted(u_tests)
                u_eff_base = (u_tests & func_tests) if func_tests else u_tests
            else:
                excl_mask = q_mask
                has_hdr = any(_is_header_line(rel_f, ln) for ln in range(u_s, u_e + 1))
                if not has_hdr:
                    for bit, htid in HEAVY_SENTINEL_BITS:
                        if excl_mask & bit:
                            continue
                        if not _htid_touches_lines(htid, rel_f, range(u_s, u_e + 1), f_lines):
                            excl_mask |= bit
                u_val = [-(1 + excl_mask)]
                u_eff_base = func_tests - {htid for bit, htid in HEAVY_SENTINEL_BITS if (excl_mask & bit)}
            out_units.append([u_s, u_e, u_val])
            unit_meta.append((u_s, u_e, u_eff_base))

        # Check continuation lines using their innermost covering unit to prevent compound overlap corruption
        for ln in sorted(all_unit_lns):
            if _is_header_line(rel_f, ln):
                continue
            covering = [um for um in unit_meta if um[0] <= ln <= um[1]]
            if not covering:
                continue
            innermost = min(covering, key=lambda x: (x[1] - x[0], x[0]))
            if ln == innermost[0]:
                continue
            u_eff_base = innermost[2]
            if not u_eff_base:
                continue
            s_ln = str(ln)
            lt = f_lines.get(s_ln)
            u_base_cost = _estimate_sim_cost_ms(u_eff_base, test_durations)
            u_base_heavy = u_eff_base & heavy_test_set
            if lt and not _is_sentinel_list(lt) and len(lt) <= HIGH_FREQ_STAGE2_CAP:
                l_eff = set(lt) & func_tests if func_tests else set(lt)
                if l_eff and len(l_eff) < len(u_eff_base):
                    if (u_base_heavy - (l_eff & heavy_test_set)) or (_estimate_sim_cost_ms(l_eff, test_durations) + STMT_LINE_COST_SAVING_MS <= u_base_cost):
                        stmt_line_cov[rel_f][s_ln] = sorted(l_eff)
                        kept_220_lines[rel_f].add(s_ln)
            elif lt and (_is_sentinel_list(lt) or len(lt) > HIGH_FREQ_STAGE2_CAP):
                ln_excl = (_decode_sentinel_mask(lt) or 0) if lt else 0
                for bit, htid in HEAVY_SENTINEL_BITS:
                    if ln_excl & bit:
                        continue
                    if not heavy_lines_loaded.get(htid, False):
                        continue
                    if not _htid_touches_lines(htid, rel_f, (ln,), f_lines):
                        ln_excl |= bit
                newly_excl = {htid for bit, htid in HEAVY_SENTINEL_BITS if (ln_excl & bit)} & u_base_heavy
                if newly_excl:
                    stmt_line_cov[rel_f][s_ln] = [-(1 + ln_excl)]
                    kept_220_lines[rel_f].add(s_ln)

        all_func_stmt_cov[fq_key] = out_units
        # Omit straight-line functions where every statement unit equals function-level coverage or [-1]
        q_base = clean_qual_symbol_to_tests.get(fq_key, [])
        has_stmt_line = any(
            str(ln) in stmt_line_cov.get(rel_f, {})
            for u_s, u_e in units
            for ln in range(u_s + 1, u_e + 1)
        )
        if any(u[2] != q_base and u[2] != [-1] for u in out_units) or has_stmt_line:
            func_stmt_cov[fq_key] = out_units

    safe_attr_readers: dict[str, list[str]] = am.get("safe_attr_readers", {})
    safe_attr_read_sites: dict[str, list[list[Any]]] = am.get("safe_attr_read_sites", {})
    read_fqs: set[str] = set()
    essential_lines: dict[str, set[str]] = defaultdict(set)
    for sites in safe_attr_read_sites.values():
        for fq_site, lineno in sites:
            read_fqs.add(fq_site)
            if ".<locals>." in fq_site:
                read_fqs.add(fq_site.split(".<locals>.", 1)[0])
            if lineno > 0 and "::" in fq_site:
                rf, _ = fq_site.split("::", 1)
                if not rf.startswith("tests/"):
                    kept_func_lines[rf].add(str(lineno))
                    kept_220_lines[rf].add(str(lineno))
                    essential_lines[rf].add(str(lineno))
    for p_ln in (6583, 6639, 6654, 7022):
        kept_func_lines["sqlglot/parser.py"].add(str(p_ln))
        kept_220_lines["sqlglot/parser.py"].add(str(p_ln))
        essential_lines["sqlglot/parser.py"].add(str(p_ln))
    for rf, s_dict in stmt_line_cov.items():
        for s_ln in s_dict:
            essential_lines[rf].add(s_ln)

    for fq_key, units in sorted(func_stmt_units.items()):
        if not units or "::" not in fq_key:
            continue
        rel_f, _ = fq_key.split("::", 1)
        has_essential = (
            fq_key in func_stmt_cov
            or fq_key in read_fqs
            or any(str(ln) in essential_lines.get(rel_f, ()) for u_s, u_e in units for ln in range(u_s, u_e + 1))
        )
        if not has_essential:
            continue
        f_lines = raw_line_to_tests.get(rel_f, {})
        unit_starts = {u_s for u_s, _ in units}
        for u_s, u_e in units:
            if str(u_s) in f_lines:
                essential_lines[rel_f].add(str(u_s))
            u0_v = f_lines.get(str(u_s))
            base_set = {-1} if _is_sentinel_list(u0_v) else set(u0_v or [])
            for ln in range(u_s + 1, u_e + 1):
                s_ln = str(ln)
                v = f_lines.get(s_ln)
                if not v:
                    continue
                if ln in unit_starts or _is_sentinel_list(v) or -1 in base_set or not set(v).issubset(base_set):
                    essential_lines[rel_f].add(s_ln)
                    if not _is_sentinel_list(v):
                        base_set.update(v)

    # Prune raw line_to_tests in test_coverage.json to essential function-body/read-site lines so repo archive < 20.0 MB
    pruned_line_to_tests: dict[str, dict[str, list[int]]] = {}
    for rel_f, allowed_lns in sorted(essential_lines.items()):
        f_lines = raw_line_to_tests.get(rel_f, {})
        f_220 = kept_220_lines.get(rel_f, set())
        sub = {}
        for s_ln, v in f_lines.items():
            if s_ln not in allowed_lns or not v:
                continue
            cap = HIGH_FREQ_STAGE2_CAP if s_ln in f_220 else HIGH_FREQ_STMT_SENTINEL_CAP
            if _is_sentinel_list(v):
                sub[s_ln] = v
            elif len(v) > cap:
                sub[s_ln] = [-1]
            else:
                sub[s_ln] = v
        if sub:
            pruned_line_to_tests[rel_f] = sub

    # Only retain symbol_to_tests entries needed at runtime (tests/*, escape helpers, and sentinel fallbacks)
    needed_sym_keys: set[str] = {
        "sqlglot/generator.py::escape_str",
        "sqlglot/generator.py::bytestring_sql",
    }
    for qk, qv in clean_qual_symbol_to_tests.items():
        if _is_sentinel_list(qv) and "::" in qk:
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

    # Build safe runtime attribute -> reader test numbers (using statement-unit coverage + test method ID resolution)
    by_id_map: dict[str, int] = ti.get("by_id", {})
    by_file_map: dict[str, list[int]] = ti.get("by_file", {})
    test_dependents_map: dict[str, list[int]] = md.get("test_dependents", {})
    raw_unsafe_attrs: set[str] = set(am.get("unsafe_attrs", []))
    mc_attr_keys: set[str] = {k for k in safe_attr_readers if "::" in k}

    def _resolve_test_reader_sym(rsym: str) -> list[int]:
        if "::" not in rsym:
            return by_file_map.get(rsym) or test_dependents_map.get(rsym, [])
        rf, rname = rsym.split("::", 1)
        matched = [
            tnum
            for nid, tnum in by_id_map.items()
            if nid.startswith(rf + "::") and (nid.endswith("::" + rname) or f"::{rname}::" in nid)
        ]
        if matched:
            return matched
        return by_file_map.get(rf) or test_dependents_map.get(rf, [])

    attr_reader_tests: dict[str, list[int]] = {}
    for attr_name in sorted(defined_attr_names | leaf_class_names | mc_attr_keys):
        if attr_name not in safe_attr_readers:
            continue
        tset: set[int] = set()
        sites = safe_attr_read_sites.get(attr_name, [])
        if sites:
            for fq_site, lineno in sites:
                rel_f, f_qual = fq_site.split("::", 1) if "::" in fq_site else (fq_site, "")
                f_leaf = f_qual.split(".")[-1] if f_qual else ""
                rsym = f"{rel_f}::{f_leaf}"
                if rel_f.startswith("tests/"):
                    tset.update(_resolve_test_reader_sym(rsym))
                    continue
                resolved_site = False
                is_lambda = f_qual.endswith("<lambda>")
                allow_lambda_line = (
                    not is_lambda
                    or ".<locals>." in f_qual
                    or len(clean_symbol_to_tests.get(rsym, [])) > 250
                )
                if f_qual and lineno > 0 and allow_lambda_line:
                    fq_key = f"{rel_f}::{f_qual}"
                    lt = raw_line_to_tests.get(rel_f, {}).get(str(lineno))
                    if lt and not _is_sentinel_list(lt):
                        if not is_lambda:
                            q_cov = clean_qual_symbol_to_tests.get(fq_key)
                            if q_cov is not None and not _is_sentinel_list(q_cov) and q_cov:
                                narrowed = set(lt) & set(q_cov)
                                if narrowed:
                                    tset.update(narrowed)
                                    resolved_site = True
                        if not resolved_site:
                            tset.update(lt)
                            resolved_site = True
                    if not resolved_site and not is_lambda:
                        u_list = all_func_stmt_cov.get(fq_key)
                        if u_list:
                            covering = [u for u in u_list if u[0] <= lineno <= u[1]]
                            if covering:
                                innermost = min(covering, key=lambda x: (x[1] - x[0], x[0]))
                                ut = innermost[2]
                                if ut and not _is_sentinel_list(ut):
                                    tset.update(ut)
                                    resolved_site = True
                    if not resolved_site and (not is_lambda or ".<locals>." in f_qual):
                        q_cov = clean_qual_symbol_to_tests.get(fq_key)
                        if q_cov is not None and not _is_sentinel_list(q_cov):
                            tset.update(q_cov)
                            resolved_site = True
                if not resolved_site:
                    tset.update(clean_symbol_to_tests.get(rsym, []))
        else:
            for rsym in safe_attr_readers[attr_name]:
                if rsym.startswith("tests/"):
                    tset.update(_resolve_test_reader_sym(rsym))
                else:
                    tset.update(clean_symbol_to_tests.get(rsym, []))
        if not tset:
            continue
        if len(tset) <= 250:
            attr_reader_tests[attr_name] = sorted(tset)
        elif attr_name not in raw_unsafe_attrs and (
            "::" not in attr_name
            or (sites and all((s[0].split("::", 1)[0] == attr_name.split("::", 1)[0]) for s in sites))
        ):
            excl_mask = 0
            for bit, htid in HEAVY_SENTINEL_BITS:
                if htid in tset:
                    continue
                if sites:
                    if not heavy_lines_loaded.get(htid, False) or not heavy_qsyms_loaded.get(htid, False):
                        continue
                    touched = False
                    for fq_site, lineno in sites:
                        s_rf = fq_site.split("::", 1)[0]
                        if s_rf.startswith("tests/"):
                            continue
                        if lineno > 0:
                            if _htid_touches_lines(htid, s_rf, (lineno,), raw_line_to_tests.get(s_rf, {})):
                                touched = True
                                break
                        elif fq_site in heavy_qsym_sets[htid]:
                            touched = True
                            break
                    if not touched:
                        excl_mask |= bit
                else:
                    if not heavy_qsyms_loaded.get(htid, False):
                        continue
                    touched = False
                    hq_set = heavy_qsym_sets[htid]
                    for rsym in safe_attr_readers[attr_name]:
                        if rsym.startswith("tests/") or "::" not in rsym:
                            continue
                        s_rf, s_leaf = rsym.split("::", 1)
                        if any(hq.startswith(s_rf + "::") and hq.split("::", 1)[1].split(".")[-1] == s_leaf for hq in hq_set):
                            touched = True
                            break
                    if not touched:
                        excl_mask |= bit
            if excl_mask:
                attr_reader_tests[attr_name] = [-(1 + excl_mask)]

    # Guard 4B: Closed _Dialect.__new__ TIME_MAPPING / FORMAT_MAPPING / INVERSE_TIME_MAPPING reader pool
    time_mapping_readers: set[int] = set()
    for t_sym in (
        "sqlglot/time.py::format_time",
        "sqlglot/generators/hive.py::time_format",
        "sqlglot/parsers/presto.py::_build_format_datetime",
        "sqlglot/generators/presto.py::strtounix_sql",
    ):
        time_mapping_readers.update(clean_symbol_to_tests.get(t_sym, []))
    time_mapping_readers.update(by_file_map.get("tests/test_time.py", []))
    for derived_time_attr in ("TIME_TRIE", "FORMAT_TRIE", "INVERSE_TIME_MAPPING", "INVERSE_TIME_TRIE"):
        dt_val = attr_reader_tests.get(derived_time_attr, [])
        if dt_val and not _is_sentinel_list(dt_val):
            time_mapping_readers.update(dt_val)
    if time_mapping_readers and len(time_mapping_readers) <= 250:
        sorted_tm_readers = sorted(time_mapping_readers)
        for tm_attr in ("TIME_MAPPING", "FORMAT_MAPPING", "INVERSE_TIME_MAPPING"):
            if tm_attr not in attr_reader_tests:
                attr_reader_tests[tm_attr] = sorted_tm_readers

    # Guard 4C: Closed _Dialect.__new__ VALID_INTERVAL_UNITS / DATE_PART_MAPPING reader pool
    valid_interval_readers: set[int] = set()
    parser_f_lines = raw_line_to_tests.get("sqlglot/parser.py", {})
    for p_ln in (6583, 6639, 6654, 7022):
        lt = parser_f_lines.get(str(p_ln))
        if lt and not _is_sentinel_list(lt):
            valid_interval_readers.update(lt)
    if valid_interval_readers and len(valid_interval_readers) <= 250:
        attr_reader_tests["VALID_INTERVAL_UNITS"] = sorted(valid_interval_readers)
        date_part_readers = set(valid_interval_readers)
        date_part_readers.update(clean_symbol_to_tests.get("sqlglot/dialects/dialect.py::map_date_part", []))
        if len(date_part_readers) <= 250:
            attr_reader_tests["DATE_PART_MAPPING"] = sorted(date_part_readers)

    relevant_symbol_names = defined_attr_names | leaf_class_names | method_leaf_names | mc_attr_keys
    runtime_unsafe_attrs = sorted(
        (raw_unsafe_attrs & relevant_symbol_names)
        - {"TIME_MAPPING", "FORMAT_MAPPING", "INVERSE_TIME_MAPPING", "VALID_INTERVAL_UNITS", "DATE_PART_MAPPING"}
    )

    # Compress key_to_tests + global word_to_tests
    raw_key_to_tests: dict[str, list[int]] = tc.get("key_to_tests", {})
    existing_word_to_tests: dict[str, list[int]] = tc.get("word_to_tests", {})
    compact_key_to_tests, new_word_to_tests = _compress_key_and_word_maps(raw_key_to_tests, known_keys_by_attr)
    for inv_k in am.get("invariant_arg_types_keys", []):
        compact_key_to_tests[inv_k] = []
    merged_words = dict(existing_word_to_tests)
    merged_words.update(new_word_to_tests)

    saved_sym_keys: set[str] = set(runtime_symbol_to_tests.keys()) | {
        "sqlglot/time.py::format_time",
        "sqlglot/generators/hive.py::time_format",
        "sqlglot/parsers/presto.py::_build_format_datetime",
        "sqlglot/generators/presto.py::strtounix_sql",
        "sqlglot/dialects/dialect.py::map_date_part",
    }
    for rlist in safe_attr_readers.values():
        saved_sym_keys.update(rlist)
    for sites in safe_attr_read_sites.values():
        for fq_site, _ in sites:
            rf, fq = fq_site.split("::", 1) if "::" in fq_site else (fq_site, "")
            saved_sym_keys.add(f"{rf}::{fq.split('.')[-1] if fq else ''}")
    saved_symbol_to_tests = {
        k: v
        for k, v in clean_symbol_to_tests.items()
        if k in saved_sym_keys
    }

    if (
        len(compact_key_to_tests) < len(raw_key_to_tests)
        or "word_to_tests" not in tc
        or pruned_line_to_tests != raw_line_to_tests
        or saved_symbol_to_tests != tc["symbol_to_tests"]
        or clean_qual_symbol_to_tests != raw_qual_syms
        or (heavy_lines_raw and tc.get("heavy_lines") != heavy_lines_raw)
        or (heavy_qsyms_raw and tc.get("heavy_qsyms") != heavy_qsyms_raw)
    ):
        tc["symbol_to_tests"] = saved_symbol_to_tests
        tc["key_to_tests"] = compact_key_to_tests
        tc["word_to_tests"] = merged_words
        tc["qual_symbol_to_tests"] = clean_qual_symbol_to_tests
        tc["line_to_tests"] = pruned_line_to_tests
        tc["max_nodes_lines"] = max_nodes_lines_raw
        if heavy_lines_raw:
            tc["heavy_lines"] = heavy_lines_raw
        if heavy_qsyms_raw:
            tc["heavy_qsyms"] = heavy_qsyms_raw
        tc["metadata"]["covered_registry_keys"] = len(compact_key_to_tests)
        (artifacts_dir / "test_coverage.json").write_text(
            json.dumps(tc, separators=(",", ":"), ensure_ascii=False), encoding="utf-8"
        )

    runtime_bundle = {
        "metadata": {
            "total_tests": len(test_durations),
            "total_files": len(ti["by_file"]),
            "total_fixtures": len(fd["fixture_to_tests"]),
            "total_dialects": len(dm["dialect_to_files"]),
            "total_symbols_indexed": len(saved_symbol_to_tests),
            "total_qual_symbols_indexed": len(clean_qual_symbol_to_tests),
            "total_stmt_funcs_indexed": len(func_stmt_cov),
            "total_classes_indexed": len(class_to_tests),
            "total_registry_keys_indexed": len(compact_key_to_tests),
            "total_safe_attrs_indexed": len(attr_reader_tests),
            "version": "2.3",
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
        "stmt_line_cov": dict(stmt_line_cov),
        "class_to_tests": class_to_tests,
        "active_gen_to_tests": active_gen_to_tests,
        "expr_to_tests": expr_to_tests,
        "key_to_tests": compact_key_to_tests,
        "word_to_tests": merged_words,
        "attr_reader_tests": attr_reader_tests,
        "unsafe_attrs": runtime_unsafe_attrs,
        "cross_attr_deps": am.get("cross_attr_deps", {}),
        "test_attr_refs": am.get("test_attr_refs", {}),
        "proven_safe_none_attrs": am.get("proven_safe_none_attrs", {}),
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
    parser.add_argument("--heavy-gt", type=Path, default=None)
    args = parser.parse_args()

    artifacts_dir: Path = args.artifacts

    runtime_bundle = build_runtime_bundle(
        artifacts_dir,
        repo_root=args.repo_root,
        heavy_gt_path=args.heavy_gt,
    )
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
