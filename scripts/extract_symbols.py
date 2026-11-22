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
                    called.add(child.func.attr)
            scan(child)

    for t in trees:
        scan(t)
    return called


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
    return None


def extract_symbols_from_ast(
    node: ast.AST,
    parent_qname: str,
    rel_qual_prefix: str,
    file_rel_path: str,
    import_called_names: set[str],
    symbols_list: list[dict[str, Any]],
    symbol_index: dict[str, Any],
) -> None:
    is_test_file = file_rel_path.startswith("tests/")
    for child in iter_child_stmts(node):
        if isinstance(child, ast.ClassDef):
            qname = f"{parent_qname}.{child.name}" if parent_qname else child.name
            rel_qual = f"{rel_qual_prefix}.{child.name}" if rel_qual_prefix else child.name
            dec_start = min((d.lineno for d in child.decorator_list), default=child.lineno)
            body_start = child.body[0].lineno if child.body else child.lineno
            end_line = getattr(child, "end_lineno", child.lineno)
            bases = [ast.unparse(b) for b in child.bases]
            decorators = [ast.unparse(d) for d in child.decorator_list]
            sym_info = {
                "name": child.name,
                "qname": qname,
                "qualname": rel_qual,
                "type": "class",
                "file": file_rel_path,
                "parent": parent_qname,
                "start": dec_start,
                "def_line": child.lineno,
                "body_start": body_start,
                "end": end_line,
                "is_factory": False,
                "bases": bases,
                "decorators": decorators,
            }
            symbols_list.append(sym_info)
            symbol_index[qname] = sym_info
            extract_symbols_from_ast(
                child, qname, rel_qual, file_rel_path, import_called_names, symbols_list, symbol_index
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
            decorators = [ast.unparse(d) for d in child.decorator_list]
            has_self_arg = bool(child.args.args) and child.args.args[0].arg == "self"
            is_fac = (
                (not has_self_arg and child.name in import_called_names)
                or function_returns_callable(child)
                or ".<locals>" in rel_qual_prefix
            )
            sym_info = {
                "name": child.name,
                "qname": qname,
                "qualname": rel_qual,
                "type": sym_type,
                "file": file_rel_path,
                "parent": parent_qname,
                "start": dec_start,
                "def_line": child.lineno,
                "body_start": body_start,
                "end": end_line,
                "is_factory": is_fac,
                "decorators": decorators,
            }
            symbols_list.append(sym_info)
            symbol_index[qname] = sym_info
            extract_symbols_from_ast(
                child,
                f"{qname}.<locals>",
                f"{rel_qual}.<locals>",
                file_rel_path,
                import_called_names,
                symbols_list,
                symbol_index,
            )


def analyze_attribute_usage(
    parsed_files: list[tuple[str, str, ast.AST]],
    import_called_names: set[str],
    file_symbols: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Identify safe runtime-only attributes, their reader symbols, and cross-class attribute aliases."""
    unsafe_attrs: set[str] = set()
    attr_readers: dict[str, set[str]] = defaultdict(set)

    # Map simple class name -> list of "rel_path::qualname"
    class_name_to_ckeys: dict[str, list[str]] = defaultdict(list)
    for rel_path, syms in file_symbols.items():
        if rel_path.startswith("tests/"):
            continue
        for s in syms:
            if s["type"] == "class":
                ck = f"{rel_path}::{s['qualname']}"
                class_name_to_ckeys[s["name"]].append(ck)
                class_name_to_ckeys[s["qualname"]].append(ck)

    cross_attr_deps: dict[str, set[str]] = defaultdict(set)
    test_attr_refs: dict[str, set[str]] = defaultdict(set)

    for rel_path, _, tree in parsed_files:
        is_test = rel_path.startswith("tests/")

        def walk_node(node: ast.AST, runtime_sym: str | None, cur_cls_qual: str | None) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    next_cls = f"{cur_cls_qual}.{child.name}" if cur_cls_qual else child.name
                    for dec in child.decorator_list:
                        walk_node(dec, None, cur_cls_qual)
                    for b in child.bases:
                        walk_node(b, None, cur_cls_qual)
                    walk_node(child, None, next_cls)
                    continue

                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for dec in child.decorator_list:
                        walk_node(dec, runtime_sym, cur_cls_qual)
                    for d in child.args.defaults:
                        walk_node(d, runtime_sym, cur_cls_qual)
                    for kd in child.args.kw_defaults:
                        if kd is not None:
                            walk_node(kd, runtime_sym, cur_cls_qual)

                    has_self = bool(child.args.args) and child.args.args[0].arg == "self"
                    is_direct_import_fac = (not has_self and child.name in import_called_names) or function_returns_callable(child)
                    if child.name in INIT_OR_META_METHODS or is_direct_import_fac:
                        next_sym = None
                    else:
                        next_sym = f"{rel_path}::{child.name}"
                    for stmt in child.body:
                        walk_node(stmt, next_sym, cur_cls_qual)
                    continue

                if isinstance(child, ast.Lambda):
                    next_sym = f"{rel_path}::<lambda>"
                    walk_node(child.body, next_sym, cur_cls_qual)
                    continue

                # Check cross-class attribute references in class bodies or test files
                if isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load):
                    attr_name = child.attr
                    unparsed_base = ast.unparse(child.value)
                    # Record cross-class reference if base matches a known SQLGlot class
                    if unparsed_base in class_name_to_ckeys:
                        for src_ck in class_name_to_ckeys[unparsed_base]:
                            if is_test:
                                test_attr_refs[f"{src_ck}.{attr_name}"].add(rel_path)
                            elif cur_cls_qual is not None:
                                dst_ck = f"{rel_path}::{cur_cls_qual}"
                                if dst_ck != src_ck:
                                    cross_attr_deps[f"{src_ck}.{attr_name}"].add(dst_ck)

                    if runtime_sym is None:
                        unsafe_attrs.add(attr_name)
                    else:
                        attr_readers[attr_name].add(runtime_sym)

                elif isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id in ("getattr", "hasattr", "setattr"):
                    if len(child.args) >= 2 and isinstance(child.args[1], ast.Constant) and isinstance(child.args[1].value, str):
                        g_attr = child.args[1].value
                        if runtime_sym is None:
                            unsafe_attrs.add(g_attr)
                        else:
                            attr_readers[g_attr].add(runtime_sym)

                walk_node(child, runtime_sym, cur_cls_qual)

        walk_node(tree, None, None)

    safe_attr_readers = {
        attr: sorted(syms)
        for attr, syms in sorted(attr_readers.items())
        if attr not in unsafe_attrs
    }
    return {
        "safe_attr_readers": safe_attr_readers,
        "unsafe_attrs": sorted(unsafe_attrs),
        "cross_attr_deps": {k: sorted(v) for k, v in sorted(cross_attr_deps.items())},
        "test_attr_refs": {k: sorted(v) for k, v in sorted(test_attr_refs.items())},
    }


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

    import_called_names = collect_import_time_called_names([t for _, _, t in parsed_files])

    for rel_path, mod_name, tree in parsed_files:
        symbols: list[dict[str, Any]] = []
        extract_symbols_from_ast(
            tree, mod_name, "", rel_path, import_called_names, symbols, symbol_index
        )
        symbols.sort(key=lambda s: (s["start"], -(s["end"] - s["start"])))
        file_symbols[rel_path] = symbols

    attr_meta = analyze_attribute_usage(parsed_files, import_called_names, file_symbols)

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
