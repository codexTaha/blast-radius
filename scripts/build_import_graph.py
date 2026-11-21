#!/usr/bin/env python3
"""Stage 2.3 — Module Import Graph & Dependency Closure.

Constructs forward imports, direct reverse dependencies, and transitive
dependency closures for all SQLGlot modules and tests.

Produces:
- artifacts/module_imports.json: {module: [directly imported modules]}
- artifacts/module_dependents.json: {
    direct_dependents: {module: [modules that import it]},
    transitive_dependents: {module: [transitive dependent modules]},
    test_dependents: {module: [dependent test file paths]}
  }
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any


def resolve_import(
    src_mod: str,
    is_pkg: bool,
    imp: dict[str, Any],
    all_known_modules: dict[str, str],
) -> str | None:
    level = imp.get("level", 0)
    mod = imp.get("module")
    name = imp.get("name")

    if level == 0:
        base_mod = mod
    else:
        pkg_parts = src_mod.split(".") if is_pkg else src_mod.split(".")[:-1]
        if level <= len(pkg_parts):
            base_parts = pkg_parts[: len(pkg_parts) - level + 1]
            base_mod = ".".join(base_parts)
            if mod:
                base_mod = f"{base_mod}.{mod}"
        else:
            return None

    if not base_mod:
        if name and name in all_known_modules:
            return name
        return None

    # Check if base_mod.name is a known submodule
    if name and f"{base_mod}.{name}" in all_known_modules:
        return f"{base_mod}.{name}"
    if base_mod in all_known_modules:
        return base_mod

    # Handle aliases like sqlglot.exp -> sqlglot.expressions
    if base_mod == "sqlglot.exp":
        return "sqlglot.expressions"
    if base_mod.startswith("sqlglot.exp."):
        return base_mod.replace("sqlglot.exp.", "sqlglot.expressions.", 1)

    return None


def build_graphs(inventory: dict[str, Any]) -> tuple[dict[str, list[str]], dict[str, Any]]:
    source_modules = inventory["source_modules"]
    test_modules = inventory["test_modules"]
    all_modules = {**source_modules, **test_modules}

    all_known_modules: dict[str, str] = {}
    for path, data in all_modules.items():
        all_known_modules[data["module"]] = path
        if data.get("is_package"):
            all_known_modules[data["module"]] = path

    direct_imports: dict[str, set[str]] = defaultdict(set)
    direct_dependents: dict[str, set[str]] = defaultdict(set)

    for path, data in all_modules.items():
        src_mod = data["module"]
        is_pkg = data.get("is_package", False)
        for imp in data["imports"]:
            target_mod = resolve_import(src_mod, is_pkg, imp, all_known_modules)
            if target_mod and target_mod != src_mod:
                direct_imports[src_mod].add(target_mod)
                direct_dependents[target_mod].add(src_mod)

    # Compute transitive closure for each module
    transitive_dependents: dict[str, set[str]] = {}
    test_dependents: dict[str, set[str]] = {}

    for mod in all_known_modules:
        visited: set[str] = set()
        queue = deque([mod])
        while queue:
            curr = queue.popleft()
            for dep in direct_dependents.get(curr, set()):
                if dep not in visited:
                    visited.add(dep)
                    queue.append(dep)

        transitive_dependents[mod] = visited

        # Extract dependent test file paths
        test_files = set()
        for dep in visited:
            if dep in all_known_modules and all_known_modules[dep].startswith("tests/"):
                test_files.add(all_known_modules[dep])
        test_dependents[mod] = test_files

    # Dialect-specific association
    # In SQLGlot, dialect tests are located at tests/dialects/test_<dialect>.py
    # or tests/test_dialects.py. We link dialect modules explicitly.
    dialect_to_tests: dict[str, list[str]] = defaultdict(list)
    for path, data in source_modules.items():
        if path.startswith("sqlglot/dialects/") and not path.endswith("__init__.py"):
            dialect_name = Path(path).stem
            # Direct dialect test file if exists
            candidate_test = f"tests/dialects/test_{dialect_name}.py"
            if candidate_test in test_modules:
                test_dependents[data["module"]].add(candidate_test)
                dialect_to_tests[dialect_name].append(candidate_test)
            # test_dialects.py always runs all dialect tests
            test_dependents[data["module"]].add("tests/test_dialects.py")
            dialect_to_tests[dialect_name].append("tests/test_dialects.py")

    # Convert sets to sorted lists for deterministic serialization
    forward_graph = {k: sorted(v) for k, v in sorted(direct_imports.items())}
    reverse_graph = {
        "direct_dependents": {k: sorted(v) for k, v in sorted(direct_dependents.items())},
        "transitive_dependents": {k: sorted(v) for k, v in sorted(transitive_dependents.items())},
        "test_dependents": {k: sorted(v) for k, v in sorted(test_dependents.items())},
        "dialect_to_tests": {k: sorted(v) for k, v in sorted(dialect_to_tests.items())},
    }

    return forward_graph, reverse_graph


def main() -> None:
    parser = argparse.ArgumentParser(description="Construct module import and dependency closure graphs.")
    parser.add_argument("--inventory", type=Path, default=Path("artifacts/repo_inventory.json"))
    parser.add_argument("--imports-out", type=Path, default=Path("artifacts/module_imports.json"))
    parser.add_argument("--dependents-out", type=Path, default=Path("artifacts/module_dependents.json"))
    args = parser.parse_args()

    print(f"Loading inventory from {args.inventory} ...", file=sys.stderr)
    with open(args.inventory, "r", encoding="utf-8") as f:
        inventory = json.load(f)

    print("Building dependency graphs ...", file=sys.stderr)
    forward_graph, reverse_graph = build_graphs(inventory)

    args.imports_out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.imports_out, "w", encoding="utf-8") as f:
        json.dump(forward_graph, f, indent=2)

    with open(args.dependents_out, "w", encoding="utf-8") as f:
        json.dump(reverse_graph, f, indent=2)

    print(f"Dependency graphs complete!", file=sys.stderr)
    print(f"  Forward imports:      {len(forward_graph)} modules ({args.imports_out.stat().st_size / 1024:.1f} KB)", file=sys.stderr)
    print(f"  Reverse dependents:   {len(reverse_graph['direct_dependents'])} modules", file=sys.stderr)
    print(f"  Transitive closures:  {len(reverse_graph['transitive_dependents'])} modules", file=sys.stderr)
    print(f"  Test dependent maps:  {len(reverse_graph['test_dependents'])} modules ({args.dependents_out.stat().st_size / 1024:.1f} KB)", file=sys.stderr)


if __name__ == "__main__":
    main()
