#!/usr/bin/env python3
"""Stage 2.2 — Enhanced AST Symbol, Per-Attribute, Per-Key, Factory & Multiline-String Extraction.

Extracts:
1. Line-range indexed symbol mappings for every source and test file:
   - classes (`type="class"`), functions/methods (`type="function"/"method"`),
   - class/module registry & setting attributes (`type="attribute"`),
   - individual dictionary registry keys (`type="dict_key"`).
2. Safe runtime attribute reader maps and cross-class attribute/registry spread links
   (`artifacts/attr_metadata.json`).
3. Multiline string literal line intervals per Python file (`artifacts/multiline_strings.json`).
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import sys
import tokenize
from collections import defaultdict
from pathlib import Path
from typing import Any

INIT_OR_META_METHODS = frozenset({
    "__new__",
    "__init_subclass__",
    "__init__",
    "_init_core",
    "_build_dispatch",
})


def file_to_module_name(rel_path: str) -> str:
    parts = list(Path(rel_path).parts)
    if parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def canon_ast_key(k: ast.expr) -> str | None:
    """Canonicalize an AST dictionary key to match runtime TrackedDict keys."""
    if isinstance(k, ast.Constant) and isinstance(k.value, str):
        return f"s:{k.value}"
    if isinstance(k, ast.Attribute):
        if isinstance(k.value, ast.Name):
            if k.value.id in ("exp", "expressions"):
                return f"e:{k.attr}"
            if k.value.id == "TokenType":
                return f"t:TokenType.{k.attr}"
            if k.value.id == "DType":
                return f"dt:{k.attr}"
        else:
            unparsed_base = ast.unparse(k.value)
            if unparsed_base in ("exp.DataType.Type", "exp.DType", "DataType.Type"):
                return f"dt:{k.attr}"
    if isinstance(k, ast.Name) and k.id and k.id[0].isupper():
        return f"e:{k.id}"
    if isinstance(k, ast.Tuple) and all(
        isinstance(elt, ast.Constant) and isinstance(elt.value, str) for elt in k.elts
    ):
        return "u:" + "|".join(elt.value for elt in k.elts)  # type: ignore[arg-type]
    return None


_STDLIB_MOD_ALIASES = frozenset({
    "t", "typing", "re", "os", "sys", "pathlib", "logging", "warnings", "functools", "itertools",
})
_BUILTIN_CONTAINER_METHODS = frozenset({
    "items", "keys", "values", "get", "update", "append", "extend", "copy",
    "setdefault", "pop", "join", "split", "strip", "lower", "upper", "replace", "format",
})


def collect_import_time_called_names(trees: list[ast.AST]) -> set[str]:
    called: set[str] = set()

    def scan(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for dec in child.decorator_list:
                        scan(dec)
                    for d in child.args.defaults:
                        scan(d)
                    for kd in child.args.kw_defaults:
                        if kd is not None:
                            scan(kd)
                continue
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Name):
                    called.add(child.func.id)
                elif isinstance(child.func, ast.Attribute):
                    if (
                        not (isinstance(child.func.value, ast.Name) and child.func.value.id in _STDLIB_MOD_ALIASES)
                        and child.func.attr not in _BUILTIN_CONTAINER_METHODS
                    ):
                        called.add(child.func.attr)
            scan(child)

    for t in trees:
        scan(t)
    return called


def collect_direct_calls_in_func(fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Collect function names called directly in `fn_node`'s body (excluding nested def/lambda bodies)."""
    calls: set[str] = set()

    def _scan(n: ast.AST) -> None:
        for ch in ast.iter_child_nodes(n):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(ch, ast.Call):
                if isinstance(ch.func, ast.Name):
                    calls.add(ch.func.id)
                elif isinstance(ch.func, ast.Attribute):
                    calls.add(ch.func.attr)
            _scan(ch)

    _scan(fn_node)
    return calls


def extract_stmt_units_from_func(fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[list[int]]:
    """Extract statement-unit [start_line, end_line] spans inside a function body (excluding nested defs)."""
    units: list[list[int]] = []

    def _add_nested_boolop_units(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return
        if isinstance(node, ast.BoolOp) and len(node.values) >= 2:
            v0_end = getattr(node.values[0], "end_lineno", node.values[0].lineno)
            for v in node.values[1:]:
                if isinstance(v, ast.BoolOp):
                    v_s = v.lineno
                    v_e = getattr(v, "end_lineno", v_s)
                    if v_s > v0_end and v_e > v_s:
                        units.append([v_s, v_e])
        for ch in ast.iter_child_nodes(node):
            _add_nested_boolop_units(ch)

    def _visit_stmts(stmts: list[ast.stmt]) -> None:
        for st in stmts:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                dec_s = min((d.lineno for d in st.decorator_list), default=st.lineno)
                b_s = st.body[0].lineno if st.body else st.lineno
                units.append([dec_s, max(dec_s, b_s - 1)])
                continue
            if isinstance(st, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
                b_s = st.body[0].lineno if st.body else st.lineno
                units.append([st.lineno, max(st.lineno, b_s - 1)])
                if isinstance(st, (ast.If, ast.While)):
                    _add_nested_boolop_units(st.test)
                _visit_stmts(st.body)
                if hasattr(st, "orelse") and st.orelse:
                    _visit_stmts(st.orelse)
                continue
            if isinstance(st, ast.Try) or (hasattr(ast, "TryStar") and isinstance(st, ast.TryStar)):
                _visit_stmts(st.body)
                for h in st.handlers:
                    hb_s = h.body[0].lineno if h.body else h.lineno
                    units.append([h.lineno, max(h.lineno, hb_s - 1)])
                    _visit_stmts(h.body)
                if st.orelse:
                    _visit_stmts(st.orelse)
                if st.finalbody:
                    _visit_stmts(st.finalbody)
                continue
            if hasattr(ast, "Match") and isinstance(st, ast.Match):
                c_s = st.cases[0].pattern.lineno if st.cases else st.lineno
                units.append([st.lineno, max(st.lineno, c_s - 1)])
                for mc in st.cases:
                    _visit_stmts(mc.body)
                continue
            end_ln = getattr(st, "end_lineno", st.lineno)
            units.append([st.lineno, end_ln])
            _add_nested_boolop_units(st)

    _visit_stmts(fn_node.body)
    deduped = sorted({(u[0], u[1]) for u in units})
    return [[u_s, u_e] for u_s, u_e in deduped]


def function_returns_callable(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    inner_defs = {
        n.name
        for n in ast.walk(node)
        if n is not node and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for sub in ast.walk(node):
        if isinstance(sub, ast.Return) and sub.value is not None:
            if isinstance(sub.value, ast.Lambda):
                return True
            if isinstance(sub.value, ast.Name) and sub.value.id in inner_defs:
                return True
    return False


def iter_child_stmts(node: ast.AST) -> list[ast.stmt]:
    out: list[ast.stmt] = []
    for attr in ("body", "orelse", "finalbody"):
        stmts = getattr(node, attr, None)
        if isinstance(stmts, list):
            for s in stmts:
                if isinstance(s, (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor)):
                    out.extend(iter_child_stmts(s))
                elif isinstance(s, ast.stmt):
                    out.append(s)
    handlers = getattr(node, "handlers", None)
    if isinstance(handlers, list):
        for h in handlers:
            out.extend(iter_child_stmts(h))
    return out


def _extract_dict_node(val: ast.expr | None) -> ast.Dict | None:
    if isinstance(val, ast.Dict):
        return val
    if isinstance(val, ast.BinOp) and isinstance(val.op, ast.BitOr) and isinstance(val.right, ast.Dict):
        return val.right
    if (
        isinstance(val, ast.DictComp)
        and len(val.generators) == 1
        and isinstance(val.generators[0].iter, ast.Call)
        and isinstance(val.generators[0].iter.func, ast.Attribute)
        and val.generators[0].iter.func.attr == "items"
        and isinstance(val.generators[0].iter.func.value, ast.Dict)
    ):
        return val.generators[0].iter.func.value
    return None


def extract_symbols_from_ast(
    node: ast.AST,
    parent_qname: str,
    rel_qual_prefix: str,
    file_rel_path: str,
    import_called_names: set[str],
    symbols_list: list[dict[str, Any]],
    symbol_index: dict[str, Any],
    func_stmt_units: dict[str, list[list[int]]],
    parent_import_calls: set[str] | None = None,
) -> None:
    is_test_file = file_rel_path.startswith("tests/")
    for child in iter_child_stmts(node):
        if isinstance(child, ast.ClassDef):
            qname = f"{parent_qname}.{child.name}" if parent_qname else child.name
            rel_qual = f"{rel_qual_prefix}.{child.name}" if rel_qual_prefix else child.name
            dec_start = min((d.lineno for d in child.decorator_list), default=child.lineno)
            body_start = child.body[0].lineno if child.body else child.lineno
            end_line = getattr(child, "end_lineno", child.lineno)
            sym_info = {
                "name": child.name,
                "qualname": rel_qual,
                "type": "class",
                "start": dec_start,
                "body_start": body_start,
                "end": end_line,
            }
            symbols_list.append(sym_info)
            symbol_index[qname] = {"file": file_rel_path, "start": dec_start, "end": end_line, "is_factory": False}
            extract_symbols_from_ast(
                child,
                qname,
                rel_qual,
                file_rel_path,
                import_called_names,
                symbols_list,
                symbol_index,
                func_stmt_units,
                None,
            )

        elif not is_test_file and isinstance(node, (ast.ClassDef, ast.Module)) and isinstance(child, (ast.Assign, ast.AnnAssign)):
            targets: list[str] = []
            if isinstance(child, ast.Assign):
                if len(child.targets) == 1 and isinstance(child.targets[0], ast.Name):
                    targets.append(child.targets[0].id)
            elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                targets.append(child.target.id)

            for attr_name in targets:
                if isinstance(node, ast.Module) and attr_name != "EXPRESSION_METADATA":
                    continue
                attr_qname = f"{parent_qname}.{attr_name}" if parent_qname else attr_name
                attr_rel_qual = f"{rel_qual_prefix}.{attr_name}" if rel_qual_prefix else attr_name
                val_node = getattr(child, "value", None)
                val_start = getattr(val_node, "lineno", child.lineno) if val_node is not None else child.lineno
                end_line = getattr(child, "end_lineno", child.lineno)
                dict_node = _extract_dict_node(val_node)
                has_dict = dict_node is not None
                attr_sym = {
                    "qualname": attr_rel_qual,
                    "type": "attribute",
                    "start": child.lineno,
                    "body_start": val_start,
                    "end": end_line,
                }
                symbols_list.append(attr_sym)

                if dict_node is not None:
                    for dk, dv in zip(dict_node.keys, dict_node.values):
                        if dk is None or dv is None:
                            continue
                        ck = canon_ast_key(dk)
                        if ck is None:
                            continue
                        k_start = dk.lineno
                        v_start = dv.lineno
                        v_end = getattr(dv, "end_lineno", getattr(dk, "end_lineno", k_start))
                        key_rel_qual = f"{attr_rel_qual}[{ck}]"
                        key_sym = {
                            "qualname": key_rel_qual,
                            "type": "dict_key",
                            "start": k_start,
                            "body_start": v_start,
                            "end": v_end,
                        }
                        symbols_list.append(key_sym)

        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            is_async = isinstance(child, ast.AsyncFunctionDef)
            is_method = bool(rel_qual_prefix) and rel_qual_prefix.split(".")[-1][0].isupper()
            sym_type = "method" if is_method else "function"
            if is_async:
                sym_type = f"async_{sym_type}"

            qname = f"{parent_qname}.{child.name}" if parent_qname else child.name
            rel_qual = f"{rel_qual_prefix}.{child.name}" if rel_qual_prefix else child.name
            dec_start = min((d.lineno for d in child.decorator_list), default=child.lineno)
            body_start = child.body[0].lineno if child.body else child.lineno
            end_line = getattr(child, "end_lineno", child.lineno)
            has_self_arg = bool(child.args.args) and child.args.args[0].arg == "self"
            called_in_enclosing_factory = bool(parent_import_calls and child.name in parent_import_calls)
            is_fac = (
                (not has_self_arg and child.name in import_called_names)
                or function_returns_callable(child)
                or called_in_enclosing_factory
            )
            if not is_fac and not is_test_file:
                func_stmt_units[f"{file_rel_path}::{rel_qual}"] = extract_stmt_units_from_func(child)
            sym_info = {
                "name": child.name,
                "qualname": rel_qual,
                "type": sym_type,
                "start": dec_start,
                "body_start": body_start,
                "end": end_line,
                "is_factory": is_fac,
            }
            symbols_list.append(sym_info)
            symbol_index[qname] = {"file": file_rel_path, "start": dec_start, "end": end_line, "is_factory": is_fac}
            child_import_calls = collect_direct_calls_in_func(child) if is_fac else None
            extract_symbols_from_ast(
                child,
                f"{qname}.<locals>",
                f"{rel_qual}.<locals>",
                file_rel_path,
                import_called_names,
                symbols_list,
                symbol_index,
                func_stmt_units,
                child_import_calls,
            )


def analyze_attribute_usage(
    parsed_files: list[tuple[str, str, ast.AST]],
    import_called_names: set[str],
    file_symbols: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Identify safe runtime-only attributes, their reader symbols/sites, and cross-class attribute aliases."""
    unsafe_attrs: set[str] = set()
    attr_readers: dict[str, set[str]] = defaultdict(set)
    attr_read_sites: dict[str, set[tuple[str, int]]] = defaultdict(set)

    # Map simple and module-prefixed class names -> list of "rel_path::qualname"
    class_name_to_ckeys: dict[str, list[str]] = defaultdict(list)
    defined_only_attrs: set[str] = set()
    leaf_only_classes: set[str] = set()
    for rel_path, syms in file_symbols.items():
        if rel_path.startswith("tests/"):
            continue
        stem = Path(rel_path).stem
        for s in syms:
            if s["type"] == "class":
                ck = f"{rel_path}::{s['qualname']}"
                leaf_only_classes.add(s["name"])
                class_name_to_ckeys[s["name"]].append(ck)
                class_name_to_ckeys[s["qualname"]].append(ck)
                class_name_to_ckeys[f"{stem}.{s['name']}"].append(ck)
                class_name_to_ckeys[f"{stem}.{s['qualname']}"].append(ck)
                if rel_path.startswith("sqlglot/expressions/"):
                    class_name_to_ckeys[f"exp.{s['name']}"].append(ck)
                    class_name_to_ckeys[f"exp.{s['qualname']}"].append(ck)
            elif s["type"] == "attribute":
                qual = s.get("qualname") or s.get("qname", "")
                defined_only_attrs.add(qual.rsplit(".", 1)[-1])
    candidate_target_attrs = defined_only_attrs | leaf_only_classes

    cross_attr_deps: dict[str, set[str]] = defaultdict(set)
    test_attr_refs: dict[str, set[str]] = defaultdict(set)

    for rel_path, _, tree in parsed_files:
        is_test = rel_path.startswith("tests/")

        def visit_node(
            child: ast.AST,
            runtime_sym: str | None,
            cur_cls_qual: str | None,
            cur_func_qual: str | None = None,
            cur_assign_attr: str | None = None,
        ) -> None:
            if isinstance(child, ast.ClassDef):
                next_cls = f"{cur_cls_qual}.{child.name}" if cur_cls_qual else child.name
                for dec in child.decorator_list:
                    visit_node(dec, None, cur_cls_qual, None, None)
                for b in child.bases:
                    visit_node(b, None, cur_cls_qual, None, None)
                for stmt in child.body:
                    visit_node(stmt, None, next_cls, None, None)
                return

            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in child.decorator_list:
                    visit_node(dec, runtime_sym, cur_cls_qual, cur_func_qual, cur_assign_attr)
                for d in child.args.defaults:
                    visit_node(d, runtime_sym, cur_cls_qual, cur_func_qual, cur_assign_attr)
                for kd in child.args.kw_defaults:
                    if kd is not None:
                        visit_node(kd, runtime_sym, cur_cls_qual, cur_func_qual, cur_assign_attr)

                if cur_func_qual:
                    next_func_qual = f"{cur_func_qual}.<locals>.{child.name}"
                elif cur_cls_qual:
                    next_func_qual = f"{cur_cls_qual}.{child.name}"
                else:
                    next_func_qual = child.name

                has_self = bool(child.args.args) and child.args.args[0].arg == "self"
                is_direct_import_fac = (
                    not has_self and cur_func_qual is None and child.name in import_called_names
                ) or function_returns_callable(child)
                if child.name in INIT_OR_META_METHODS or is_direct_import_fac:
                    next_sym = None
                else:
                    next_sym = f"{rel_path}::{child.name}"
                for stmt in child.body:
                    visit_node(stmt, next_sym, cur_cls_qual, next_func_qual, None)
                return

            if isinstance(child, ast.Lambda):
                for d in child.args.defaults:
                    visit_node(d, runtime_sym, cur_cls_qual, cur_func_qual, cur_assign_attr)
                for kd in child.args.kw_defaults:
                    if kd is not None:
                        visit_node(kd, runtime_sym, cur_cls_qual, cur_func_qual, cur_assign_attr)
                next_sym = f"{rel_path}::<lambda>"
                next_fq = f"{cur_func_qual}.<locals>.<lambda>" if cur_func_qual else None
                visit_node(child.body, next_sym, cur_cls_qual, next_fq, None)
                return

            if (
                isinstance(child, (ast.Assign, ast.AnnAssign))
                and cur_cls_qual is not None
                and cur_func_qual is None
                and runtime_sym is None
            ):
                assign_name: str | None = None
                if isinstance(child, ast.Assign) and len(child.targets) == 1 and isinstance(child.targets[0], ast.Name):
                    assign_name = child.targets[0].id
                elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                    assign_name = child.target.id
                if assign_name is not None and child.value is not None:
                    visit_node(child.value, None, cur_cls_qual, None, assign_name)
                    return

            # Guard 4A-1: Same-class bare attribute load at class/module definition time (e.g. *ALTERABLES, JODA_TIME_MAPPING)
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load) and runtime_sym is None:
                unsafe_attrs.add(child.id)

            # Check cross-class attribute references in class bodies or test files
            if isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load):
                attr_name = child.attr
                unparsed_base = ast.unparse(child.value)
                is_tracked_cross_ref = False
                if unparsed_base in class_name_to_ckeys:
                    for src_ck in class_name_to_ckeys[unparsed_base]:
                        if is_test:
                            test_attr_refs[f"{src_ck}.{attr_name}"].add(rel_path)
                            is_tracked_cross_ref = True
                        elif cur_cls_qual is not None:
                            dst_ck = f"{rel_path}::{cur_cls_qual}"
                            if dst_ck != src_ck:
                                cross_attr_deps[f"{src_ck}.{attr_name}"].add(dst_ck)
                                if runtime_sym is None and cur_assign_attr == attr_name:
                                    is_tracked_cross_ref = True

                if runtime_sym is None:
                    if not is_tracked_cross_ref:
                        unsafe_attrs.add(attr_name)
                else:
                    attr_readers[attr_name].add(runtime_sym)
                    if attr_name in candidate_target_attrs:
                        fq_site = f"{rel_path}::{cur_func_qual}" if cur_func_qual else runtime_sym
                        attr_read_sites[attr_name].add((fq_site, getattr(child, "lineno", 0)))

            elif (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id in ("getattr", "hasattr", "setattr")
            ):
                if (
                    len(child.args) >= 2
                    and isinstance(child.args[1], ast.Constant)
                    and isinstance(child.args[1].value, str)
                ):
                    g_attr = child.args[1].value
                    if runtime_sym is None:
                        unsafe_attrs.add(g_attr)
                    else:
                        attr_readers[g_attr].add(runtime_sym)
                        if g_attr in candidate_target_attrs:
                            fq_site = f"{rel_path}::{cur_func_qual}" if cur_func_qual else runtime_sym
                            attr_read_sites[g_attr].add((fq_site, getattr(child, "lineno", 0)))

            for sub_child in ast.iter_child_nodes(child):
                visit_node(sub_child, runtime_sym, cur_cls_qual, cur_func_qual, cur_assign_attr)

        for top_child in ast.iter_child_nodes(tree):
            visit_node(top_child, None, None, None, None)

    # Propagate _Dialect.__new__ derived attribute dependencies (TIME_MAPPING -> INVERSE_TIME_MAPPING / INVERSE_TIME_TRIE)
    for k in list(cross_attr_deps.keys()):
        if k.endswith((".INVERSE_TIME_MAPPING", ".INVERSE_TIME_TRIE", ".TIME_TRIE")):
            base_ck = k.rsplit(".", 1)[0]
            cross_attr_deps[f"{base_ck}.TIME_MAPPING"].update(cross_attr_deps[k])

    # Transitively close cross_attr_deps across multi-hop inheritance/spread chains (e.g. Hive -> Spark2 -> Spark)
    changed = True
    while changed:
        changed = False
        for full_aid, dst_set in list(cross_attr_deps.items()):
            if "." not in full_aid:
                continue
            aname = full_aid.rsplit(".", 1)[-1]
            extra: set[str] = set()
            for dst_ck in dst_set:
                extra.update(cross_attr_deps.get(f"{dst_ck}.{aname}", set()))
                if aname == "TIME_MAPPING":
                    extra.update(cross_attr_deps.get(f"{dst_ck}.INVERSE_TIME_MAPPING", set()))
                    extra.update(cross_attr_deps.get(f"{dst_ck}.INVERSE_TIME_TRIE", set()))
            new_items = extra - dst_set
            if new_items:
                dst_set.update(new_items)
                changed = True

    safe_attr_readers = {
        attr: sorted(syms)
        for attr, syms in sorted(attr_readers.items())
        if attr not in unsafe_attrs and attr in candidate_target_attrs
    }
    safe_attr_read_sites = {
        attr: [list(x) for x in sorted(sites)]
        for attr, sites in sorted(attr_read_sites.items())
        if attr not in unsafe_attrs
        and (
            (attr in defined_only_attrs and len(sites) <= 60)
            or (attr in leaf_only_classes and len(sites) <= 10)
        )
    }
    return {
        "safe_attr_readers": safe_attr_readers,
        "safe_attr_read_sites": safe_attr_read_sites,
        "unsafe_attrs": sorted(unsafe_attrs),
        "cross_attr_deps": {k: sorted(v) for k, v in sorted(cross_attr_deps.items())},
        "test_attr_refs": {k: sorted(v) for k, v in sorted(test_attr_refs.items())},
        "invariant_arg_types_keys": find_non_null_constructed_arg_types_keys(parsed_files),
        "proven_safe_none_attrs": find_proven_safe_none_self_attrs(parsed_files),
    }


SAFE_NONE_PRIMITIVE_TYPES = frozenset({"str", "int", "float", "bool", "bytes", "range"})
FORBIDDEN_NONE_TYPE_NAMES = frozenset({"Any", "object", "reverse_key"})


def find_proven_safe_none_self_attrs(
    parsed_files: list[tuple[str, str, ast.AST]],
) -> dict[str, list[str]]:
    """Identify `file::ClassName` -> sorted list of `self.<attr>` names statically proven safe for `is None` <-> `== None`.

    An attribute `self.<attr>` on `ClassName` is proven safe iff:
    1. It has an explicit scalar type annotation `T`, `T | None`, or `Optional[T]` where `T` is either a closed
       Python scalar primitive (`str`, `int`, `float`, `bool`, `bytes`, `range`) or an internal SQLGlot class defined
       in `sqlglot/` whose `__eq__` is either standard `object.__eq__` or `Expr`/`Dialect` (which guard `None` in O(1)).
    2. `T` is never `Any`, `t.Any`, `object`, `reverse_key`, or any container/callable type.
    3. No method in `ClassName` assigns an unannotated or non-safe-annotated parameter to `self.<attr>`.
    """
    safe_internal_classes: set[str] = set()
    for rel_path, _, tree in parsed_files:
        if not rel_path.startswith("sqlglot/"):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name in FORBIDDEN_NONE_TYPE_NAMES:
                    continue
                eq_methods = [
                    n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == "__eq__"
                ]
                if not eq_methods or node.name in ("Expr", "Expression", "Dialect"):
                    safe_internal_classes.add(node.name)

    def _is_safe_type_node(node: ast.expr | None, allow_none: bool = False) -> bool:
        if node is None:
            return False
        if isinstance(node, ast.Constant):
            if node.value is None:
                return allow_none
            if isinstance(node.value, str):
                s = node.value.strip()
                if s in FORBIDDEN_NONE_TYPE_NAMES:
                    return False
                return s in SAFE_NONE_PRIMITIVE_TYPES or s in safe_internal_classes
            return False
        if isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NONE_TYPE_NAMES:
                return False
            return node.id in SAFE_NONE_PRIMITIVE_TYPES or node.id in safe_internal_classes
        if isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_NONE_TYPE_NAMES:
                return False
            if isinstance(node.value, ast.Name) and node.value.id in ("t", "typing"):
                return False
            return node.attr in safe_internal_classes
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return _is_safe_type_node(node.left, allow_none=True) and _is_safe_type_node(
                node.right, allow_none=True
            )
        if isinstance(node, ast.Subscript):
            base = ast.unparse(node.value)
            if base in ("t.Optional", "Optional", "typing.Optional"):
                return _is_safe_type_node(node.slice, allow_none=True)
        return False

    proven: dict[str, list[str]] = {}
    for rel_path, _, tree in parsed_files:
        if not rel_path.startswith("sqlglot/"):
            continue
        for node in getattr(tree, "body", []):
            if not isinstance(node, ast.ClassDef):
                continue
            if any("TypedDict" in ast.unparse(b) for b in node.bases):
                continue
            safe_attrs: set[str] = set()
            unsafe_attrs: set[str] = set()
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    if _is_safe_type_node(item.annotation):
                        safe_attrs.add(item.target.id)
                    else:
                        unsafe_attrs.add(item.target.id)
                elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    unsafe_params: set[str] = set()
                    for a in item.args.args + item.args.kwonlyargs:
                        if a.arg == "self":
                            continue
                        if not _is_safe_type_node(a.annotation):
                            unsafe_params.add(a.arg)
                    for sub in ast.walk(item):
                        if (
                            isinstance(sub, ast.AnnAssign)
                            and isinstance(sub.target, ast.Attribute)
                            and isinstance(sub.target.value, ast.Name)
                            and sub.target.value.id == "self"
                        ):
                            if _is_safe_type_node(sub.annotation):
                                if isinstance(sub.value, ast.Name) and sub.value.id in unsafe_params:
                                    unsafe_attrs.add(sub.target.attr)
                                else:
                                    safe_attrs.add(sub.target.attr)
                            else:
                                unsafe_attrs.add(sub.target.attr)
                        elif isinstance(sub, ast.Assign):
                            for t in sub.targets:
                                if (
                                    isinstance(t, ast.Attribute)
                                    and isinstance(t.value, ast.Name)
                                    and t.value.id == "self"
                                ):
                                    if isinstance(sub.value, ast.Name) and sub.value.id in unsafe_params:
                                        unsafe_attrs.add(t.attr)
            final_attrs = sorted(safe_attrs - unsafe_attrs)
            if final_attrs:
                proven[f"{rel_path}::{node.name}"] = final_attrs
    return proven


def _is_provably_non_null_expr(e: ast.expr, non_null_vars: set[str]) -> bool:
    if isinstance(e, ast.Compare):
        return True
    if isinstance(e, ast.Constant) and e.value is not None:
        return True
    if isinstance(e, ast.UnaryOp) and isinstance(e.op, ast.Not):
        return True
    if isinstance(e, ast.Call) and isinstance(e.func, ast.Name) and e.func.id in ("bool", "int", "str", "tuple"):
        return True
    if isinstance(e, ast.Name) and e.id in non_null_vars:
        return True
    return False


def find_non_null_constructed_arg_types_keys(
    parsed_files: list[tuple[str, str, ast.AST]],
) -> list[str]:
    """Identify leaf non-Func Expr.arg_types keys that are always passed a provably non-None/non-[] value at every construction site."""
    expr_classes: dict[str, tuple[str, list[str], list[str]]] = {}
    subclasses_of: set[str] = set()
    for rel_path, _, tree in parsed_files:
        if not rel_path.startswith("sqlglot/expressions/"):
            continue
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                bases = [ast.unparse(b) for b in node.bases]
                for b in bases:
                    subclasses_of.add(b.split(".")[-1])
                for item in node.body:
                    if (
                        isinstance(item, ast.Assign)
                        and len(item.targets) == 1
                        and isinstance(item.targets[0], ast.Name)
                        and item.targets[0].id == "arg_types"
                        and isinstance(item.value, ast.Dict)
                        and all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in item.value.keys)
                    ):
                        expr_classes[node.name] = (
                            rel_path,
                            bases,
                            [k.value for k in item.value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)],
                        )

    calls_to: dict[str, list[tuple[str, ast.Call, set[str]]]] = {c: [] for c in expr_classes}
    for rel_path, _, tree in parsed_files:
        for fn_node in ast.walk(tree):
            if isinstance(fn_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                var_assigns: dict[str, list[ast.expr]] = {}
                for sub in ast.walk(fn_node):
                    if isinstance(sub, ast.NamedExpr) and isinstance(sub.target, ast.Name):
                        var_assigns.setdefault(sub.target.id, []).append(sub.value)
                    elif isinstance(sub, ast.Assign):
                        for t in sub.targets:
                            if isinstance(t, ast.Name):
                                var_assigns.setdefault(t.id, []).append(sub.value)
                non_null_vars = {
                    v
                    for v, vals in var_assigns.items()
                    if vals and all(_is_provably_non_null_expr(val, set()) for val in vals)
                }
                for sub in ast.walk(fn_node):
                    if isinstance(sub, ast.Call):
                        fn = sub.func
                        cname = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else None)
                        if cname in calls_to:
                            nn_keys = {
                                kw.arg
                                for kw in sub.keywords
                                if kw.arg and _is_provably_non_null_expr(kw.value, non_null_vars)
                            }
                            calls_to[cname].append((rel_path, sub, nn_keys))

    out: list[str] = []
    for cname, (rel_p, bases, keys) in sorted(expr_classes.items()):
        if cname in subclasses_of or any("Func" in b for b in bases):
            continue
        csites = calls_to[cname]
        if not csites or any(fp.startswith("tests/") for fp, _, _ in csites):
            continue
        if all(not c.args and all(kw.arg is not None for kw in c.keywords) for _, c, _ in csites):
            common_nn = set(keys)
            for _, _, nnk in csites:
                common_nn &= nnk
            for k in sorted(common_nn):
                out.append(f"{rel_p}::{cname}.arg_types[s:{k}]")
    return out


def extract_multiline_strings(raw_bytes: bytes) -> list[list[int]]:
    intervals: list[list[int]] = []
    try:
        for tok in tokenize.tokenize(io.BytesIO(raw_bytes).readline):
            if tok.type == tokenize.STRING:
                s_row = tok.start[0]
                e_row = tok.end[0]
                if e_row > s_row:
                    intervals.append([s_row, e_row])
    except Exception:
        pass
    return intervals


def process_repository(
    repo_path: Path,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any], dict[str, list[list[int]]], dict[str, Any]]:
    file_symbols: dict[str, list[dict[str, Any]]] = {}
    symbol_index: dict[str, Any] = {}
    multiline_strings: dict[str, list[list[int]]] = {}

    targets = sorted(
        p
        for sub in ("sqlglot", "sqlglotc", "tests")
        if (repo_path / sub).exists()
        for p in (repo_path / sub).rglob("*.py")
    )

    parsed_files: list[tuple[str, str, ast.AST]] = []
    for py_file in targets:
        rel_path = py_file.relative_to(repo_path).as_posix()
        mod_name = file_to_module_name(rel_path)
        raw_bytes = py_file.read_bytes()

        mls = extract_multiline_strings(raw_bytes)
        if mls:
            multiline_strings[rel_path] = mls

        try:
            tree = ast.parse(raw_bytes, filename=rel_path)
            parsed_files.append((rel_path, mod_name, tree))
        except Exception as e:
            print(f"Warning: Failed to parse {rel_path}: {e}", file=sys.stderr)

    sqlglot_trees = [t for rel, _, t in parsed_files if rel.startswith("sqlglot/")]
    import_called_names = collect_import_time_called_names(sqlglot_trees)
    cross_imported_names: set[str] = set()
    for t in sqlglot_trees:
        for n in ast.walk(t):
            if isinstance(n, ast.ImportFrom):
                for alias in n.names:
                    cross_imported_names.add(alias.name)
                    if alias.asname:
                        cross_imported_names.add(alias.asname)
    func_stmt_units: dict[str, list[list[int]]] = {}

    for rel_path, mod_name, tree in parsed_files:
        symbols: list[dict[str, Any]] = []
        local_import_called = collect_import_time_called_names([tree])
        effective_import_called = local_import_called | {
            n for n in import_called_names if not n.startswith("_") or n in cross_imported_names
        }
        extract_symbols_from_ast(
            tree, mod_name, "", rel_path, effective_import_called, symbols, symbol_index, func_stmt_units
        )
        symbols.sort(key=lambda s: (s["start"], -(s["end"] - s["start"])))
        file_symbols[rel_path] = symbols

    attr_meta = analyze_attribute_usage(parsed_files, import_called_names, file_symbols)
    attr_meta["func_stmt_units"] = func_stmt_units

    return file_symbols, symbol_index, multiline_strings, attr_meta


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract line-range symbol maps from SQLGlot.")
    parser.add_argument("--repo", type=Path, default=Path("sqlglot"), help="SQLGlot repository checkout")
    parser.add_argument("--file-symbols-out", type=Path, default=Path("artifacts/file_symbols.json"))
    parser.add_argument("--symbol-index-out", type=Path, default=Path("artifacts/symbol_index.json"))
    parser.add_argument(
        "--multiline-strings-out", type=Path, default=Path("artifacts/multiline_strings.json")
    )
    parser.add_argument(
        "--attr-meta-out", type=Path, default=Path("artifacts/attr_metadata.json")
    )
    args = parser.parse_args()

    print(f"Extracting AST symbols from {args.repo} ...", file=sys.stderr)
    file_symbols, symbol_index, multiline_strings, attr_meta = process_repository(args.repo)

    args.file_symbols_out.parent.mkdir(parents=True, exist_ok=True)
    args.file_symbols_out.write_text(
        json.dumps(file_symbols, separators=(",", ":")), encoding="utf-8"
    )
    args.symbol_index_out.write_text(
        json.dumps(symbol_index, separators=(",", ":")), encoding="utf-8"
    )
    args.multiline_strings_out.write_text(
        json.dumps(multiline_strings, separators=(",", ":")), encoding="utf-8"
    )
    args.attr_meta_out.write_text(
        json.dumps(attr_meta, separators=(",", ":")), encoding="utf-8"
    )

    fac_cnt = sum(1 for s in symbol_index.values() if s.get("is_factory"))
    print(
        f"Symbol extraction complete: {len(file_symbols)} files, {len(symbol_index)} symbols "
        f"({fac_cnt} factory/import-called), {len(attr_meta['safe_attr_readers'])} safe runtime attrs.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
