#!/usr/bin/env python3
"""Stage 2.7 — Additional Resource & Dialect Mapping.

Constructs comprehensive dialect inheritance, descendants, test linkages,
optimizer fixture bindings, and core engine sensitivity mappings.

Produces artifacts/dialect_maps.json.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any


def extract_dialect_maps(
    repo_path: Path,
    test_index: dict[str, Any],
    fixture_deps: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dialects_dir = repo_path / "sqlglot" / "dialects"
    generators_dir = repo_path / "sqlglot" / "generators"
    parsers_dir = repo_path / "sqlglot" / "parsers"
    typing_dir = repo_path / "sqlglot" / "typing"
    by_file = test_index["by_file"]

    file_to_dialect: dict[str, str] = {}
    dialect_to_files: dict[str, list[str]] = defaultdict(list)
    dialect_bases: dict[str, str] = {}

    # 1. Parse dialect classes and direct inheritance from sqlglot/dialects/*.py
    for py_file in sorted(dialects_dir.glob("*.py")):
        if py_file.name in ("__init__.py", "dialect.py"):
            continue
        dialect_name = py_file.stem
        rel_path = py_file.relative_to(repo_path).as_posix()
        file_to_dialect[rel_path] = dialect_name
        dialect_to_files[dialect_name].append(rel_path)

        try:
            tree = ast.parse(py_file.read_bytes(), filename=rel_path)
            for node in tree.body:
                if isinstance(node, ast.ClassDef) and node.name.lower() == dialect_name.replace("_", ""):
                    for b in node.bases:
                        base_name = ast.unparse(b).lower()
                        if base_name not in ("dialect", "dialects"):
                            dialect_bases[dialect_name] = base_name
        except Exception as e:
            print(f"Warning: Failed to parse dialect {rel_path}: {e}", file=sys.stderr)

    # 2. Also register corresponding generators, parsers, and typing files
    for d in sorted(dialect_to_files.keys()):
        gen_path = generators_dir / f"{d}.py"
        if gen_path.exists():
            rel_gen = gen_path.relative_to(repo_path).as_posix()
            file_to_dialect[rel_gen] = d
            dialect_to_files[d].append(rel_gen)

        par_path = parsers_dir / f"{d}.py"
        if par_path.exists():
            rel_par = par_path.relative_to(repo_path).as_posix()
            file_to_dialect[rel_par] = d
            dialect_to_files[d].append(rel_par)

        typ_path = typing_dir / f"{d}.py"
        if typ_path.exists():
            rel_typ = typ_path.relative_to(repo_path).as_posix()
            file_to_dialect[rel_typ] = d
            dialect_to_files[d].append(rel_typ)

        test_file = f"tests/dialects/test_{d}.py"
        if test_file in by_file:
            file_to_dialect[test_file] = d
            dialect_to_files[d].append(test_file)

        dialect_to_files[d].sort()

    # 3. Build direct and transitive descendants
    direct_children: dict[str, set[str]] = defaultdict(set)
    for child, parent in dialect_bases.items():
        direct_children[parent].add(child)

    transitive_descendants: dict[str, list[str]] = {}
    for d in sorted(dialect_to_files):
        visited: set[str] = set()
        queue = deque([d])
        while queue:
            curr = queue.popleft()
            for child in sorted(direct_children.get(curr, set())):
                if child not in visited:
                    visited.add(child)
                    queue.append(child)
        transitive_descendants[d] = sorted(visited)

    # 4. Map dialects to optimizer fixture dependencies
    dialect_to_opt_fixtures: dict[str, list[str]] = defaultdict(list)
    dialect_to_opt_tests: dict[str, list[int]] = defaultdict(list)

    fixture_to_tests = fixture_deps.get("fixture_to_tests", {}) if fixture_deps else {}

    for fix_file in sorted((repo_path / "tests" / "fixtures" / "optimizer").glob("*.sql")):
        rel_fix = fix_file.relative_to(repo_path).as_posix()
        try:
            content = fix_file.read_text(encoding="utf-8")
            mentioned_dialects = sorted(set(re.findall(r"#\s*dialect:\s*([a-zA-Z0-9_]+)", content)))
            for md in mentioned_dialects:
                d_lower = md.lower()
                if d_lower in dialect_to_files:
                    dialect_to_opt_fixtures[d_lower].append(rel_fix)
                    opt_tests = fixture_to_tests.get(rel_fix, [])
                    dialect_to_opt_tests[d_lower].extend(opt_tests)
        except Exception as e:
            print(f"Warning: Failed to parse fixture {fix_file}: {e}", file=sys.stderr)

    sorted_opt_fixtures: dict[str, list[str]] = {}
    sorted_opt_tests: dict[str, list[int]] = {}
    for d in sorted(dialect_to_files):
        sorted_opt_fixtures[d] = sorted(set(dialect_to_opt_fixtures[d]))
        sorted_opt_tests[d] = sorted(set(dialect_to_opt_tests[d]))

    # 5. Map each dialect to its test files and test numbers
    dialect_to_test_files: dict[str, list[str]] = {}
    dialect_to_tests: dict[str, list[int]] = {}

    for d in sorted(dialect_to_files):
        test_files = set()
        candidate_test = f"tests/dialects/test_{d}.py"
        if candidate_test in by_file:
            test_files.add(candidate_test)

        # Also add test files for all descendant dialects
        for child in transitive_descendants[d]:
            child_test = f"tests/dialects/test_{child}.py"
            if child_test in by_file:
                test_files.add(child_test)

        dialect_to_test_files[d] = sorted(test_files)

        # Collect all direct dialect tests + descendant dialect tests + optimizer fixture tests
        tests = set()
        for tf in test_files:
            tests.update(by_file[tf])
        tests.update(sorted_opt_tests[d])
        dialect_to_tests[d] = sorted(tests)

    # 6. Core foundational engine files with broad system impact
    core_engine_files = [
        "sqlglot/__init__.py",
        "sqlglot/expressions.py",
        "sqlglot/expressions/core.py",
        "sqlglot/parser.py",
        "sqlglot/parsers/base.py",
        "sqlglot/generator.py",
        "sqlglot/generators/python.py",
        "sqlglot/tokens.py",
        "sqlglot/dialects/dialect.py",
        "sqlglot/optimizer/optimizer.py",
        "sqlglot/executor/executor.py",
    ]

    return {
        "metadata": {
            "total_dialects": len(dialect_to_files),
            "subclassed_dialects": len(dialect_bases),
            "core_engine_files": len(core_engine_files),
            "mapped_files": len(file_to_dialect),
        },
        "dialect_to_files": {d: dialect_to_files[d] for d in sorted(dialect_to_files)},
        "file_to_dialect": dict(sorted(file_to_dialect.items())),
        "dialect_inheritance": dict(sorted(dialect_bases.items())),
        "dialect_descendants": transitive_descendants,
        "dialect_to_test_files": dialect_to_test_files,
        "dialect_to_optimizer_fixtures": sorted_opt_fixtures,
        "dialect_to_optimizer_tests": sorted_opt_tests,
        "dialect_to_tests": dialect_to_tests,
        "core_engine_files": sorted(core_engine_files),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build dialect hierarchy and core resource maps.")
    parser.add_argument("--repo", type=Path, default=Path("sqlglot"))
    parser.add_argument("--test-index", type=Path, default=Path("artifacts/test_index.json"))
    parser.add_argument("--fixture-deps", type=Path, default=Path("artifacts/fixture_dependencies.json"))
    parser.add_argument("--out", type=Path, default=Path("artifacts/dialect_maps.json"))
    args = parser.parse_args()

    print(f"Loading test index from {args.test_index} ...", file=sys.stderr)
    test_index = json.loads(args.test_index.read_text(encoding="utf-8"))

    fixture_deps = None
    if args.fixture_deps.exists():
        print(f"Loading fixture dependencies from {args.fixture_deps} ...", file=sys.stderr)
        fixture_deps = json.loads(args.fixture_deps.read_text(encoding="utf-8"))

    print(f"Analyzing dialects and resources in {args.repo} ...", file=sys.stderr)
    result = extract_dialect_maps(args.repo, test_index, fixture_deps)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    meta = result["metadata"]
    print(f"Dialect & resource mapping complete!", file=sys.stderr)
    print(f"  Total dialects:        {meta['total_dialects']}", file=sys.stderr)
    print(f"  Subclassed dialects:   {meta['subclassed_dialects']}", file=sys.stderr)
    print(f"  Mapped dialect files:  {meta['mapped_files']}", file=sys.stderr)
    print(f"  Core engine files:     {meta['core_engine_files']}", file=sys.stderr)
    print(f"  Output path:           {args.out} ({args.out.stat().st_size / 1024:.1f} KB)", file=sys.stderr)


if __name__ == "__main__":
    main()
