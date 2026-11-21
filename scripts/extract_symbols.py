#!/usr/bin/env python3
"""Stage 2.2 — AST and Symbol Extraction.

Extracts line-range indexed symbol mappings for every source and test file.
Produces:
- artifacts/file_symbols.json: {file_path: [symbols sorted by line range]}
- artifacts/symbol_index.json: {qualified_name: symbol details}
Supports fast line-to-symbol resolution for unified diff hunks.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from pathlib import Path
from typing import Any


def file_to_module_name(rel_path: str) -> str:
    parts = list(Path(rel_path).parts)
    if parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def extract_symbols_from_ast(
    node: ast.AST,
    parent_qname: str,
    file_rel_path: str,
    symbols_list: list[dict[str, Any]],
    symbol_index: dict[str, Any],
) -> None:
    for child in getattr(node, "body", []):
        if isinstance(child, ast.ClassDef):
            qname = f"{parent_qname}.{child.name}"
            bases = [ast.unparse(b) for b in child.bases]
            decorators = [ast.unparse(d) for d in child.decorator_list]
            sym_info = {
                "name": child.name,
                "qname": qname,
                "type": "class",
                "file": file_rel_path,
                "parent": parent_qname,
                "start": child.lineno,
                "end": getattr(child, "end_lineno", child.lineno),
                "bases": bases,
                "decorators": decorators,
            }
            symbols_list.append(sym_info)
            symbol_index[qname] = sym_info
            # Recurse into class body (for inner classes and methods)
            extract_symbols_from_ast(child, qname, file_rel_path, symbols_list, symbol_index)

        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            is_async = isinstance(child, ast.AsyncFunctionDef)
            # Determine if method or function
            is_method = "." in parent_qname and parent_qname.split(".")[-1][0].isupper()
            sym_type = "method" if is_method else "function"
            if is_async:
                sym_type = f"async_{sym_type}"

            qname = f"{parent_qname}.{child.name}"
            decorators = [ast.unparse(d) for d in child.decorator_list]
            sym_info = {
                "name": child.name,
                "qname": qname,
                "type": sym_type,
                "file": file_rel_path,
                "parent": parent_qname,
                "start": child.lineno,
                "end": getattr(child, "end_lineno", child.lineno),
                "decorators": decorators,
            }
            symbols_list.append(sym_info)
            symbol_index[qname] = sym_info
            # Recurse into function body (for nested functions/classes)
            extract_symbols_from_ast(child, qname, file_rel_path, symbols_list, symbol_index)


def process_repository(repo_path: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    file_symbols: dict[str, list[dict[str, Any]]] = {}
    symbol_index: dict[str, Any] = {}

    # Traverse all python files under sqlglot/ and tests/
    targets = sorted(list((repo_path / "sqlglot").rglob("*.py")) + list((repo_path / "tests").rglob("*.py")))

    for py_file in targets:
        rel_path = py_file.relative_to(repo_path).as_posix()
        mod_name = file_to_module_name(rel_path)

        try:
            tree = ast.parse(py_file.read_bytes(), filename=rel_path)
            symbols: list[dict[str, Any]] = []
            extract_symbols_from_ast(tree, mod_name, rel_path, symbols, symbol_index)

            # Sort symbols by start line, then by depth (inner first)
            symbols.sort(key=lambda s: (s["start"], -s["end"]))
            file_symbols[rel_path] = symbols
        except Exception as e:
            print(f"Warning: Failed to parse {rel_path}: {e}", file=sys.stderr)

    return file_symbols, symbol_index


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract line-range symbol maps from SQLGlot.")
    parser.add_argument("--repo", type=Path, default=Path("sqlglot"), help="SQLGlot repository checkout")
    parser.add_argument("--file-symbols-out", type=Path, default=Path("artifacts/file_symbols.json"))
    parser.add_argument("--symbol-index-out", type=Path, default=Path("artifacts/symbol_index.json"))
    args = parser.parse_args()

    print(f"Extracting AST symbols from {args.repo} ...", file=sys.stderr)
    file_symbols, symbol_index = process_repository(args.repo)

    args.file_symbols_out.parent.mkdir(parents=True, exist_ok=True)

    with open(args.file_symbols_out, "w", encoding="utf-8") as f:
        json.dump(file_symbols, f, indent=2)

    with open(args.symbol_index_out, "w", encoding="utf-8") as f:
        json.dump(symbol_index, f, indent=2)

    total_files = len(file_symbols)
    total_symbols = len(symbol_index)
    print(f"Symbol extraction complete!", file=sys.stderr)
    print(f"  Indexed files:   {total_files}", file=sys.stderr)
    print(f"  Indexed symbols: {total_symbols}", file=sys.stderr)
    print(f"  Output files:    {args.file_symbols_out} ({args.file_symbols_out.stat().st_size / 1024:.1f} KB), {args.symbol_index_out} ({args.symbol_index_out.stat().st_size / 1024:.1f} KB)", file=sys.stderr)


if __name__ == "__main__":
    main()
