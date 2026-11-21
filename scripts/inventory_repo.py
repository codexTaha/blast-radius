#!/usr/bin/env python3
"""Stage 2.1 — Repository Inventory and AST Entity Extraction.

Analyzes the pinned SQLGlot repository to extract:
- Source and test Python modules
- Classes, inheritance, and methods
- Top-level functions
- Static imports (absolute & relative)
- Re-exports (__all__)
- Dynamic import and registry patterns
- Test fixtures and resources
Generates artifacts/repo_inventory.json.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from pathlib import Path
from typing import Any


class SymbolExtractor(ast.NodeVisitor):
    def __init__(self, module_name: str, file_path: str):
        self.module_name = module_name
        self.file_path = file_path
        self.classes: list[dict[str, Any]] = []
        self.functions: list[dict[str, Any]] = []
        self.imports: list[dict[str, Any]] = []
        self.reexports: list[str] = []
        self.dynamic_patterns: list[dict[str, Any]] = []
        self._current_class: str | None = None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        prev_class = self._current_class
        qualified_name = f"{self.module_name}.{node.name}"
        if prev_class:
            qualified_name = f"{prev_class}.{node.name}"

        bases = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                bases.append(ast.unparse(base))
            else:
                bases.append(ast.unparse(base))

        class_info: dict[str, Any] = {
            "name": node.name,
            "qualified_name": qualified_name,
            "bases": bases,
            "lineno": node.lineno,
            "end_lineno": getattr(node, "end_lineno", node.lineno),
            "methods": [],
        }

        self._current_class = qualified_name
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                method_info = {
                    "name": item.name,
                    "qualified_name": f"{qualified_name}.{item.name}",
                    "lineno": item.lineno,
                    "end_lineno": getattr(item, "end_lineno", item.lineno),
                    "is_async": isinstance(item, ast.AsyncFunctionDef),
                }
                class_info["methods"].append(method_info)
            elif isinstance(item, ast.ClassDef):
                self.visit(item)

        self.classes.append(class_info)
        self._current_class = prev_class

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if self._current_class is None:
            self.functions.append({
                "name": node.name,
                "qualified_name": f"{self.module_name}.{node.name}",
                "lineno": node.lineno,
                "end_lineno": getattr(node, "end_lineno", node.lineno),
                "is_async": False,
            })
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if self._current_class is None:
            self.functions.append({
                "name": node.name,
                "qualified_name": f"{self.module_name}.{node.name}",
                "lineno": node.lineno,
                "end_lineno": getattr(node, "end_lineno", node.lineno),
                "is_async": True,
            })
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append({
                "type": "import",
                "name": alias.name,
                "alias": alias.asname,
                "module": None,
                "level": 0,
                "lineno": node.lineno,
            })

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self.imports.append({
                "type": "from",
                "name": alias.name,
                "alias": alias.asname,
                "module": node.module,
                "level": node.level or 0,
                "lineno": node.lineno,
            })

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "__all__":
                if isinstance(node.value, (ast.List, ast.Tuple, ast.Set)):
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            self.reexports.append(elt.value)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # Detect dynamic import or registry patterns
        call_repr = ""
        if isinstance(node.func, ast.Name):
            call_repr = node.func.id
        elif isinstance(node.func, ast.Attribute):
            call_repr = ast.unparse(node.func)

        if call_repr in ("importlib.import_module", "__import__", "Dialect.get_or_raise", "Dialect.get"):
            args_repr = [ast.unparse(arg) for arg in node.args]
            self.dynamic_patterns.append({
                "pattern": call_repr,
                "args": args_repr,
                "lineno": node.lineno,
            })
        self.generic_visit(node)


def file_to_module_name(rel_path: str) -> str:
    parts = list(Path(rel_path).parts)
    if parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def inventory_repository(repo_path: Path) -> dict[str, Any]:
    source_dir = repo_path / "sqlglot"
    tests_dir = repo_path / "tests"

    source_modules: dict[str, Any] = {}
    test_modules: dict[str, Any] = {}
    fixtures: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []

    # 1. Process source modules
    for py_file in sorted(source_dir.rglob("*.py")):
        rel_path = py_file.relative_to(repo_path).as_posix()
        mod_name = file_to_module_name(rel_path)
        try:
            tree = ast.parse(py_file.read_bytes(), filename=rel_path)
            extractor = SymbolExtractor(mod_name, rel_path)
            extractor.visit(tree)
            source_modules[rel_path] = {
                "path": rel_path,
                "module": mod_name,
                "is_package": py_file.name == "__init__.py",
                "classes": extractor.classes,
                "functions": extractor.functions,
                "imports": extractor.imports,
                "reexports": extractor.reexports,
                "dynamic_patterns": extractor.dynamic_patterns,
            }
        except Exception as e:
            parse_errors.append({"path": rel_path, "error": str(e)})

    # 2. Process test modules
    for py_file in sorted(tests_dir.rglob("*.py")):
        rel_path = py_file.relative_to(repo_path).as_posix()
        mod_name = file_to_module_name(rel_path)
        try:
            tree = ast.parse(py_file.read_bytes(), filename=rel_path)
            extractor = SymbolExtractor(mod_name, rel_path)
            extractor.visit(tree)
            test_modules[rel_path] = {
                "path": rel_path,
                "module": mod_name,
                "is_conftest": py_file.name == "conftest.py",
                "classes": extractor.classes,
                "functions": extractor.functions,
                "imports": extractor.imports,
                "reexports": extractor.reexports,
                "dynamic_patterns": extractor.dynamic_patterns,
            }
        except Exception as e:
            parse_errors.append({"path": rel_path, "error": str(e)})

    # 3. Process fixtures
    fixtures_dir = tests_dir / "fixtures"
    if fixtures_dir.exists():
        for f in sorted(fixtures_dir.rglob("*")):
            if f.is_file():
                rel_path = f.relative_to(repo_path).as_posix()
                fixtures.append({
                    "path": rel_path,
                    "extension": f.suffix,
                    "size_bytes": f.stat().st_size,
                })

    # Summary statistics
    total_classes = sum(len(m["classes"]) for m in source_modules.values())
    total_methods = sum(len(c["methods"]) for m in source_modules.values() for c in m["classes"])
    total_functions = sum(len(m["functions"]) for m in source_modules.values())
    total_imports = sum(len(m["imports"]) for m in source_modules.values())

    return {
        "metadata": {
            "sqlglot_commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
            "source_modules_count": len(source_modules),
            "test_modules_count": len(test_modules),
            "fixtures_count": len(fixtures),
            "parse_errors_count": len(parse_errors),
            "total_classes": total_classes,
            "total_methods": total_methods,
            "total_functions": total_functions,
            "total_imports": total_imports,
        },
        "source_modules": source_modules,
        "test_modules": test_modules,
        "fixtures": fixtures,
        "parse_errors": parse_errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract repository inventory and AST symbols.")
    parser.add_argument("--repo", type=Path, default=Path("sqlglot"), help="Path to sqlglot checkout")
    parser.add_argument("--out", type=Path, default=Path("artifacts/repo_inventory.json"), help="Output artifact path")
    args = parser.parse_args()

    print(f"Inventorying repository at: {args.repo} ...", file=sys.stderr)
    result = inventory_repository(args.repo)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    meta = result["metadata"]
    print(f"Inventory complete! Output written to {args.out}", file=sys.stderr)
    print(f"  Source modules: {meta['source_modules_count']}", file=sys.stderr)
    print(f"  Test modules:   {meta['test_modules_count']}", file=sys.stderr)
    print(f"  Classes:        {meta['total_classes']}", file=sys.stderr)
    print(f"  Methods:        {meta['total_methods']}", file=sys.stderr)
    print(f"  Functions:      {meta['total_functions']}", file=sys.stderr)
    print(f"  Imports:        {meta['total_imports']}", file=sys.stderr)
    print(f"  Fixtures:       {meta['fixtures_count']}", file=sys.stderr)
    print(f"  Parse errors:   {meta['parse_errors_count']}", file=sys.stderr)


if __name__ == "__main__":
    main()
