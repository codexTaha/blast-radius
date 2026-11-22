#!/usr/bin/env python3
"""Stage 2.2 — Enhanced AST Symbol, Factory-Function & Multiline-String Extraction.

Extracts:
1. Line-range indexed symbol mappings for every source and test file:
   - exact decorator start (`start`), header line (`def_line`), executable `body_start`
   - symbol type (`class`, `function`, `method`)
   - in-file `qualname` (matching Python `cls.__qualname__`)
   - `is_factory`: True if the function returns a nested function/lambda OR is called
     at module/class import time anywhere across the repository.
2. Multiline string literal line intervals per Python file (via `tokenize`).

Produces:
- artifacts/file_symbols.json
- artifacts/symbol_index.json
- artifacts/multiline_strings.json
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import sys
import tokenize
from pathlib import Path
from typing import Any


REGISTRY_NAMES = frozenset({
    "TRANSFORMS",
    "FUNCTIONS",
    "FUNCTION_PARSERS",
    "NO_PAREN_FUNCTION_PARSERS",
    "KEYWORDS",
    "NUMERIC_ESCAPES",
    "arg_types",
})


def file_to_module_name(rel_path: str) -> str:
    parts = list(Path(rel_path).parts)
    if parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def collect_import_time_called_names(trees: list[ast.AST]) -> set[str]:
    called: set[str] = set()

    def scan(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                # Default argument expressions in FunctionDef DO execute at import/class-def time!
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


def extract_symbols_from_ast(
    node: ast.AST,
    parent_qname: str,
    rel_qual_prefix: str,
    file_rel_path: str,
    import_called_names: set[str],
    symbols_list: list[dict[str, Any]],
    symbol_index: dict[str, Any],
) -> None:
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

        elif not file_rel_path.startswith("tests/") and isinstance(node, ast.ClassDef) and isinstance(child, (ast.Assign, ast.AnnAssign)):
            target_names: list[str] = []
            if isinstance(child, ast.Assign):
                for t in child.targets:
                    if isinstance(t, ast.Name):
                        target_names.append(t.id)
            elif isinstance(child, ast.AnnAssign):
                if isinstance(child.target, ast.Name):
                    target_names.append(child.target.id)

            for attr_name in target_names:
                attr_qname = f"{parent_qname}.{attr_name}" if parent_qname else attr_name
                attr_rel_qual = f"{rel_qual_prefix}.{attr_name}" if rel_qual_prefix else attr_name
                attr_end = getattr(child, "end_lineno", child.lineno)
                attr_sym = {
                    "name": attr_name,
                    "qualname": attr_rel_qual,
                    "type": "attribute",
                    "start": child.lineno,
                    "end": attr_end,
                }
                symbols_list.append(attr_sym)

                val_node = getattr(child, "value", None)
                if attr_name in REGISTRY_NAMES and isinstance(val_node, ast.Dict):
                    for k, v in zip(val_node.keys, val_node.values):
                        if k is not None:
                            try:
                                if isinstance(k, ast.Constant):
                                    key_name = str(k.value)
                                elif isinstance(k, ast.Attribute):
                                    key_name = ast.unparse(k)
                                elif isinstance(k, ast.Name):
                                    key_name = k.id
                                else:
                                    key_name = ast.unparse(k)
                            except Exception:
                                continue

                            k_start = k.lineno
                            v_end = getattr(v, "end_lineno", getattr(k, "end_lineno", k.lineno))
                            key_rel_qual = f"{attr_rel_qual}[{key_name}]"
                            key_sym = {
                                "name": key_name,
                                "qualname": key_rel_qual,
                                "type": "dict_key",
                                "start": k_start,
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
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any], dict[str, list[list[int]]]]:
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

    return file_symbols, symbol_index, multiline_strings


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract line-range symbol maps from SQLGlot.")
    parser.add_argument("--repo", type=Path, default=Path("sqlglot"), help="SQLGlot repository checkout")
    parser.add_argument("--file-symbols-out", type=Path, default=Path("artifacts/file_symbols.json"))
    parser.add_argument("--symbol-index-out", type=Path, default=Path("artifacts/symbol_index.json"))
    parser.add_argument(
        "--multiline-strings-out", type=Path, default=Path("artifacts/multiline_strings.json")
    )
    args = parser.parse_args()

    print(f"Extracting AST symbols from {args.repo} ...", file=sys.stderr)
    file_symbols, symbol_index, multiline_strings = process_repository(args.repo)

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

    fac_cnt = sum(1 for s in symbol_index.values() if s.get("is_factory"))
    print(
        f"Symbol extraction complete: {len(file_symbols)} files, {len(symbol_index)} symbols "
        f"({fac_cnt} factory/import-called), {len(multiline_strings)} files with multiline strings.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
