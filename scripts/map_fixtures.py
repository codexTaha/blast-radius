#!/usr/bin/env python3
"""Stage 2.6 — Fixture Dependency Mapping.

Maps all static .sql and data fixtures under tests/fixtures/ directly to
the specific Pytest test numbers and test files that load them via
check_file, load_sql_fixtures, load_sql_fixture_pairs, or class-level setups.

Produces artifacts/fixture_dependencies.json.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any


def map_fixtures(repo_path: Path, test_index: dict[str, Any]) -> dict[str, Any]:
    id_to_num = test_index["by_id"]
    fixtures_dir = repo_path / "tests" / "fixtures"

    all_fixtures = [
        f.relative_to(repo_path).as_posix()
        for f in fixtures_dir.rglob("*")
        if f.is_file()
    ]

    # Precompute alias lookup sets for each fixture path
    fixture_aliases: dict[str, set[str]] = {}
    for f in all_fixtures:
        stem = Path(f).stem
        name = Path(f).name
        rel_optimizer = f.replace("tests/fixtures/optimizer/", "")
        rel_fixtures = f.replace("tests/fixtures/", "")
        fixture_aliases[f] = {stem, name, rel_optimizer, rel_fixtures, f}

    fixture_to_tests: dict[str, set[int]] = {f: set() for f in all_fixtures}
    fixture_to_files: dict[str, set[str]] = {f: set() for f in all_fixtures}

    # AST scan of all test files under tests/
    for py_file in sorted((repo_path / "tests").rglob("*.py")):
        rel_py = py_file.relative_to(repo_path).as_posix()
        try:
            tree = ast.parse(py_file.read_bytes(), filename=rel_py)
        except Exception as e:
            print(f"Warning: Failed to parse {rel_py}: {e}", file=sys.stderr)
            continue

        for class_node in tree.body:
            if not isinstance(class_node, ast.ClassDef):
                continue
            class_name = class_node.name

            class_fixtures: set[str] = set()

            # Check class-level statements & setUpClass
            for item in class_node.body:
                if isinstance(item, ast.FunctionDef) and item.name in ("setUpClass", "setUp"):
                    for call_node in ast.walk(item):
                        if isinstance(call_node, ast.Call) and call_node.args:
                            if isinstance(call_node.args[0], ast.Constant):
                                arg_str = str(call_node.args[0].value)
                                for f, aliases in fixture_aliases.items():
                                    if arg_str in aliases or arg_str.removesuffix(".sql") in aliases:
                                        class_fixtures.add(f)

            # Check individual test methods
            for item in class_node.body:
                if not isinstance(item, ast.FunctionDef):
                    continue
                method_name = item.name
                node_id = f"{rel_py}::{class_name}::{method_name}"
                test_num = id_to_num.get(node_id)

                method_fixtures: set[str] = set(class_fixtures)

                for call_node in ast.walk(item):
                    if isinstance(call_node, ast.Call) and call_node.args:
                        func_name = ""
                        if isinstance(call_node.func, ast.Attribute):
                            func_name = call_node.func.attr
                        elif isinstance(call_node.func, ast.Name):
                            func_name = call_node.func.id

                        if func_name in ("check_file", "load_sql_fixtures", "load_sql_fixture_pairs"):
                            if isinstance(call_node.args[0], ast.Constant):
                                arg_str = str(call_node.args[0].value)
                                for f, aliases in fixture_aliases.items():
                                    if arg_str in aliases or arg_str.removesuffix(".sql") in aliases:
                                        method_fixtures.add(f)

                if test_num is not None:
                    for f in method_fixtures:
                        fixture_to_tests[f].add(test_num)
                        fixture_to_files[f].add(rel_py)

    # Convert sets to sorted lists
    clean_fixture_to_tests = {k: sorted(v) for k, v in sorted(fixture_to_tests.items()) if v}
    clean_fixture_to_files = {k: sorted(v) for k, v in sorted(fixture_to_files.items()) if v}

    return {
        "metadata": {
            "total_fixtures": len(all_fixtures),
            "mapped_fixtures": len(clean_fixture_to_tests),
        },
        "fixture_to_tests": clean_fixture_to_tests,
        "fixture_to_files": clean_fixture_to_files,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Map test fixture files to test numbers.")
    parser.add_argument("--repo", type=Path, default=Path("sqlglot"), help="SQLGlot repository checkout")
    parser.add_argument("--test-index", type=Path, default=Path("artifacts/test_index.json"))
    parser.add_argument("--out", type=Path, default=Path("artifacts/fixture_dependencies.json"))
    args = parser.parse_args()

    print(f"Loading test index from {args.test_index} ...", file=sys.stderr)
    test_index = json.loads(args.test_index.read_text(encoding="utf-8"))

    print(f"Mapping fixtures from {args.repo}/tests/fixtures ...", file=sys.stderr)
    result = map_fixtures(args.repo, test_index)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    meta = result["metadata"]
    print(f"Fixture mapping complete!", file=sys.stderr)
    print(f"  Total fixtures:  {meta['total_fixtures']}", file=sys.stderr)
    print(f"  Mapped fixtures: {meta['mapped_fixtures']}", file=sys.stderr)
    print(f"  Output path:     {args.out} ({args.out.stat().st_size / 1024:.1f} KB)", file=sys.stderr)


if __name__ == "__main__":
    main()
