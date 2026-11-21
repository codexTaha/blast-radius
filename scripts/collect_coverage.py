#!/usr/bin/env python3
"""Stage 2.5 — Full-Fidelity Ground-Truth Dynamic Coverage & Class-Dispatch Collector.

Runs Pytest inside the exact competition Python 3.13 environment with:
1. In-process ProcessPoolExecutor monkeypatch (eliminates multiprocessing spawn blind spots
   in test_optimizer.py and test_executor.py).
2. PEP 669 sys.monitoring (PY_START) across sqlglot/, sqlglotc/, and tests/.
3. Runtime class/MRO dispatch hooks on Expr, Dialect, Tokenizer, JSONPathTokenizer,
   Parser, Generator, and TypeAnnotator (captures table-driven / inherited class attributes
   like arg_types, KEYWORDS, FUNCTIONS, TRANSFORMS, TIME_MAPPING, EXPRESSION_METADATA).
4. File-open tracking (builtins.open / io.open) for fixtures, CSVs, and AST-parsed files.
5. unittest setUpClass attribution across all test methods of the class.

Produces artifacts/test_coverage.json.
"""

from __future__ import annotations

import argparse
import builtins
import concurrent.futures
import inspect
import io
import json
import os
import sys
import time
import unittest
from collections import defaultdict
from concurrent.futures import Future
from pathlib import Path
from typing import Any


class InProcessExecutor:
    """Synchronous in-process replacement for ProcessPoolExecutor during coverage collection."""

    def __init__(
        self,
        max_workers: int | None = None,
        mp_context: Any = None,
        initializer: Any = None,
        initargs: tuple[Any, ...] = (),
        max_tasks_per_child: int | None = None,
    ) -> None:
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Future[Any]:
        fut: Future[Any] = Future()
        try:
            res = fn(*args, **kwargs)
        except BaseException as exc:
            fut.set_exception(exc)
        else:
            fut.set_result(res)
        return fut

    def map(self, fn: Any, *iterables: Any, timeout: float | None = None, chunksize: int = 1) -> Any:
        return map(fn, *iterables)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        pass

    def __enter__(self) -> InProcessExecutor:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        self.shutdown()
        return False


# Patch ProcessPoolExecutor BEFORE pytest or any test module imports it
concurrent.futures.ProcessPoolExecutor = InProcessExecutor  # type: ignore[misc,assignment]

if not hasattr(sys, "monitoring"):
    raise RuntimeError("sys.monitoring is required (Python 3.12+).")

import pytest

TOOL_ID = sys.monitoring.COVERAGE_ID
TRACKED_PREFIXES = ("sqlglot/", "sqlglotc/", "tests/")


class DynamicCoverageCollector:
    def __init__(self, repo_prefix: str, id_to_number: dict[str, int]):
        self.repo_prefix = repo_prefix
        self.id_to_number = id_to_number

        self.active: bool = False
        self.current_test_number: int | None = None
        self.current_files: set[str] = set()
        self.current_symbols: set[str] = set()
        self.current_classes: set[type] = set()

        self.in_setup_class: type | None = None
        self.setup_class_files: dict[type, set[str]] = defaultdict(set)
        self.setup_class_symbols: dict[type, set[str]] = defaultdict(set)
        self.setup_class_classes: dict[type, set[type]] = defaultdict(set)
        self.class_to_test_numbers: dict[type, list[int]] = defaultdict(list)

        self.test_to_files: dict[int, set[str]] = defaultdict(set)
        self.test_to_symbols: dict[int, set[str]] = defaultdict(set)
        self.test_to_class_keys: dict[int, set[str]] = defaultdict(set)

        self.passed_tests: list[int] = []
        self.failed_tests: list[int] = []

        self._file_cache: dict[str, str | None] = {}
        self._class_mro_cache: dict[type, tuple[set[str], set[str]]] = {}

        sys.monitoring.use_tool_id(TOOL_ID, "blast_radius_coverage")
        sys.monitoring.register_callback(TOOL_ID, sys.monitoring.events.PY_START, self.on_py_start)

    def _rel_repo_path(self, path_str: str) -> str | None:
        if path_str in self._file_cache:
            return self._file_cache[path_str]
        try:
            abs_p = os.path.abspath(path_str)
        except Exception:
            self._file_cache[path_str] = None
            return None
        if abs_p.startswith(self.repo_prefix + "/"):
            rel = abs_p[len(self.repo_prefix) + 1 :]
            if rel.startswith(TRACKED_PREFIXES):
                self._file_cache[path_str] = rel
                return rel
        self._file_cache[path_str] = None
        return None

    def record_file(self, path_str: str) -> None:
        if not self.active:
            return
        rel = self._rel_repo_path(path_str)
        if rel is not None:
            if self.in_setup_class is not None:
                self.setup_class_files[self.in_setup_class].add(rel)
            else:
                self.current_files.add(rel)

    def record_class(self, cls: type) -> None:
        if not self.active:
            return
        if self.in_setup_class is not None:
            self.setup_class_classes[self.in_setup_class].add(cls)
        else:
            if cls not in self.current_classes:
                self.current_classes.add(cls)

    def resolve_class_mro(self, cls: type) -> tuple[set[str], set[str]]:
        cached = self._class_mro_cache.get(cls)
        if cached is not None:
            return cached

        files: set[str] = set()
        class_keys: set[str] = set()
        for base in getattr(cls, "__mro__", ()):
            mod_name = getattr(base, "__module__", "")
            if not mod_name or not mod_name.startswith("sqlglot"):
                continue
            mod = sys.modules.get(mod_name)
            mod_file = getattr(mod, "__file__", None) if mod else None
            if mod_file:
                rel = self._rel_repo_path(mod_file)
                if rel:
                    files.add(rel)
                    qname = getattr(base, "__qualname__", base.__name__)
                    class_keys.add(f"{rel}::{qname}")

        res = (files, class_keys)
        self._class_mro_cache[cls] = res
        return res

    def on_py_start(self, code: Any, instruction_offset: int) -> None:
        fn = code.co_filename
        if fn in self._file_cache:
            rel = self._file_cache[fn]
        else:
            rel = self._rel_repo_path(fn)
        if rel is not None:
            sym = f"{rel}::{code.co_name}"
            if self.in_setup_class is not None:
                self.setup_class_files[self.in_setup_class].add(rel)
                self.setup_class_symbols[self.in_setup_class].add(sym)
            else:
                self.current_files.add(rel)
                self.current_symbols.add(sym)

    def install_sqlglot_hooks(self, repo_path: Path) -> None:
        import sqlglot
        from sqlglot import exp, generator, parser, tokens
        from sqlglot.dialects.dialect import Dialect
        from sqlglot.jsonpath import JSONPathTokenizer
        from sqlglot.optimizer.annotate_types import TypeAnnotator

        collector = self

        # 1. Expr.__init__
        orig_expr_init = exp.Expr.__init__

        def hooked_expr_init(self_obj: Any, *args: Any, **kwargs: Any) -> None:
            if collector.active:
                cls = type(self_obj)
                if collector.in_setup_class is not None:
                    collector.setup_class_classes[collector.in_setup_class].add(cls)
                elif cls not in collector.current_classes:
                    collector.current_classes.add(cls)
            orig_expr_init(self_obj, *args, **kwargs)

        exp.Expr.__init__ = hooked_expr_init  # type: ignore[method-assign]

        # 2. Dialect.__init__ & Dialect.get_or_raise
        orig_dialect_init = Dialect.__init__

        def hooked_dialect_init(self_obj: Any, *args: Any, **kwargs: Any) -> None:
            collector.record_class(type(self_obj))
            orig_dialect_init(self_obj, *args, **kwargs)

        Dialect.__init__ = hooked_dialect_init  # type: ignore[method-assign]

        orig_get_or_raise = Dialect.get_or_raise.__func__  # type: ignore[attr-defined]

        @classmethod  # type: ignore[misc]
        def hooked_get_or_raise(cls: Any, dialect: Any = None) -> Any:
            res = orig_get_or_raise(cls, dialect)
            collector.record_class(type(res))
            return res

        Dialect.get_or_raise = hooked_get_or_raise  # type: ignore[method-assign]

        # 3. Tokenizer & JSONPathTokenizer
        for tok_cls in (tokens.Tokenizer, JSONPathTokenizer):
            orig_tok_init = tok_cls.__init__

            def make_tok_init(orig: Any) -> Any:
                def _hook(self_obj: Any, *args: Any, **kwargs: Any) -> None:
                    collector.record_class(type(self_obj))
                    orig(self_obj, *args, **kwargs)
                return _hook

            tok_cls.__init__ = make_tok_init(orig_tok_init)  # type: ignore[method-assign]

            orig_tokenize = tok_cls.tokenize

            def make_tokenize(orig: Any) -> Any:
                def _hook(self_obj: Any, *args: Any, **kwargs: Any) -> Any:
                    collector.record_class(type(self_obj))
                    return orig(self_obj, *args, **kwargs)
                return _hook

            tok_cls.tokenize = make_tokenize(orig_tokenize)  # type: ignore[method-assign]

        # 4. Parser.__init__ & Parser.reset
        orig_parser_init = parser.Parser.__init__

        def hooked_parser_init(self_obj: Any, *args: Any, **kwargs: Any) -> None:
            collector.record_class(type(self_obj))
            orig_parser_init(self_obj, *args, **kwargs)

        parser.Parser.__init__ = hooked_parser_init  # type: ignore[method-assign]

        orig_parser_reset = parser.Parser.reset

        def hooked_parser_reset(self_obj: Any, *args: Any, **kwargs: Any) -> None:
            collector.record_class(type(self_obj))
            orig_parser_reset(self_obj, *args, **kwargs)

        parser.Parser.reset = hooked_parser_reset  # type: ignore[method-assign]

        # 5. Generator.__init__ & Generator.generate
        orig_gen_init = generator.Generator.__init__

        def hooked_gen_init(self_obj: Any, *args: Any, **kwargs: Any) -> None:
            collector.record_class(type(self_obj))
            orig_gen_init(self_obj, *args, **kwargs)

        generator.Generator.__init__ = hooked_gen_init  # type: ignore[method-assign]

        orig_gen_generate = generator.Generator.generate

        def hooked_gen_generate(self_obj: Any, *args: Any, **kwargs: Any) -> Any:
            collector.record_class(type(self_obj))
            return orig_gen_generate(self_obj, *args, **kwargs)

        generator.Generator.generate = hooked_gen_generate  # type: ignore[method-assign]

        # 6. TypeAnnotator.__init__ (also credit sqlglot/typing/<dialect>.py across dialect MRO)
        orig_ta_init = TypeAnnotator.__init__

        def hooked_ta_init(self_obj: Any, *args: Any, **kwargs: Any) -> None:
            collector.record_class(type(self_obj))
            orig_ta_init(self_obj, *args, **kwargs)
            dialect_obj = getattr(self_obj, "dialect", None)
            if dialect_obj is not None and collector.active:
                collector.record_class(type(dialect_obj))
                for base in type(dialect_obj).__mro__:
                    mod = getattr(base, "__module__", "")
                    if mod.startswith("sqlglot.dialects."):
                        d_name = mod.rsplit(".", 1)[-1]
                        typing_rel = f"sqlglot/typing/{d_name}.py"
                        if (repo_path / typing_rel).exists():
                            if collector.in_setup_class is not None:
                                collector.setup_class_files[collector.in_setup_class].add(typing_rel)
                            else:
                                collector.current_files.add(typing_rel)

        TypeAnnotator.__init__ = hooked_ta_init  # type: ignore[method-assign]

        # 7. File-open hook (builtins.open and io.open)
        orig_open = builtins.open

        def hooked_open(file: Any, *args: Any, **kwargs: Any) -> Any:
            if collector.active and isinstance(file, (str, os.PathLike)):
                try:
                    collector.record_file(os.fspath(file))
                except Exception:
                    pass
            return orig_open(file, *args, **kwargs)

        builtins.open = hooked_open
        io.open = hooked_open

    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        wrapped_classes: set[type] = set()
        for item in items:
            tnum = self.id_to_number.get(item.nodeid)
            cls = getattr(item, "cls", None)
            if cls is not None and tnum is not None:
                self.class_to_test_numbers[cls].append(tnum)
                if cls not in wrapped_classes and issubclass(cls, unittest.TestCase):
                    wrapped_classes.add(cls)
                    if "setUpClass" in cls.__dict__:
                        orig_sc = cls.__dict__["setUpClass"]
                        orig_fn = orig_sc.__func__ if isinstance(orig_sc, classmethod) else orig_sc
                        collector = self

                        def make_sc(target_cls: type, fn: Any) -> Any:
                            @classmethod  # type: ignore[misc]
                            def _wrapped(c: type) -> Any:
                                prev_sc = collector.in_setup_class
                                prev_active = collector.active
                                collector.in_setup_class = target_cls
                                collector.active = True
                                sys.monitoring.set_events(TOOL_ID, sys.monitoring.events.PY_START)
                                try:
                                    return fn(c)
                                finally:
                                    collector.in_setup_class = prev_sc
                                    collector.active = prev_active
                                    if not prev_active:
                                        sys.monitoring.set_events(TOOL_ID, sys.monitoring.events.NO_EVENTS)
                            return _wrapped

                        setattr(cls, "setUpClass", make_sc(cls, orig_fn))

    def pytest_runtest_setup(self, item: pytest.Item) -> None:
        self.current_test_number = self.id_to_number.get(item.nodeid)
        self.current_files = set()
        self.current_symbols = set()
        self.current_classes = set()

        if self.current_test_number is not None:
            self.active = True
            sys.monitoring.set_events(TOOL_ID, sys.monitoring.events.PY_START)

    def pytest_runtest_teardown(self, item: pytest.Item) -> None:
        self.active = False
        sys.monitoring.set_events(TOOL_ID, sys.monitoring.events.NO_EVENTS)

        tnum = self.current_test_number
        if tnum is not None:
            files = set(self.current_files)
            symbols = set(self.current_symbols)
            class_keys: set[str] = set()

            for cls in self.current_classes:
                c_files, c_keys = self.resolve_class_mro(cls)
                files.update(c_files)
                class_keys.update(c_keys)

            self.test_to_files[tnum].update(files)
            self.test_to_symbols[tnum].update(symbols)
            self.test_to_class_keys[tnum].update(class_keys)

        self.current_test_number = None

    def finalize_setup_classes(self) -> None:
        for cls, tnums in self.class_to_test_numbers.items():
            for base in cls.__mro__:
                if base not in self.setup_class_files and base not in self.setup_class_classes and base not in self.setup_class_symbols:
                    continue
                sc_files = set(self.setup_class_files.get(base, set()))
                sc_syms = set(self.setup_class_symbols.get(base, set()))
                sc_ckeys: set[str] = set()
                for c in self.setup_class_classes.get(base, set()):
                    cf, ck = self.resolve_class_mro(c)
                    sc_files.update(cf)
                    sc_ckeys.update(ck)
                for tnum in tnums:
                    self.test_to_files[tnum].update(sc_files)
                    self.test_to_symbols[tnum].update(sc_syms)
                    self.test_to_class_keys[tnum].update(sc_ckeys)

    def pytest_runtest_logreport(self, report: Any) -> None:
        if report.when == "call":
            num = self.id_to_number.get(report.nodeid)
            if num is not None:
                if report.passed:
                    self.passed_tests.append(num)
                elif report.failed:
                    self.failed_tests.append(num)


def main() -> None:
    ap = argparse.ArgumentParser(description="Collect full-fidelity ground-truth coverage.")
    ap.add_argument("--repo", type=Path, default=Path("/workspace/sqlglot"), help="SQLGlot repository root")
    ap.add_argument("--test-index", type=Path, default=Path("/workspace/artifacts/test_index.json"))
    ap.add_argument("--out", type=Path, default=Path("/workspace/artifacts/test_coverage.json"))
    args = ap.parse_args()

    repo_root = args.repo.resolve()
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    print(f"Loading test index from {args.test_index} ...", file=sys.stderr)
    test_index = json.loads(args.test_index.read_text(encoding="utf-8"))
    id_to_number: dict[str, int] = test_index["by_id"]

    collector = DynamicCoverageCollector(str(repo_root), id_to_number)
    collector.install_sqlglot_hooks(repo_root)

    test_dirs = [str(repo_root / "tests")]
    print(f"Running test suite in {test_dirs} (in-process ProcessPool + MRO hooks) ...", file=sys.stderr)
    t0 = time.time()

    pytest_args = [
        "-q",
        "--tb=short",
        "-o",
        "filterwarnings=ignore",
        *test_dirs,
    ]

    exit_code = pytest.main(pytest_args, plugins=[collector])
    collector.finalize_setup_classes()
    elapsed = time.time() - t0

    print(f"Pytest finished in {elapsed:.2f} s with exit code {exit_code}", file=sys.stderr)
    print(f"  Tracked tests: {len(collector.test_to_files)} / {len(id_to_number)}", file=sys.stderr)
    print(f"  Passed tests:  {len(collector.passed_tests)}", file=sys.stderr)
    print(f"  Failed tests:  {len(collector.failed_tests)}", file=sys.stderr)

    # Add deterministic static bridges for subprocess-isolated test_lazy_load (#214)
    # test_lazy_load runs `python -c "import sqlglot; assert len(sqlglot.Dialect._classes) == 1"`
    lazy_load_num = id_to_number.get("tests/dialects/test_dialect.py::TestDialect::test_lazy_load")
    if lazy_load_num is not None:
        # Run a quick clean subprocess once to find exact modules imported by `import sqlglot`
        import subprocess

        probe = (
            "import sys, os, sqlglot; "
            f"rp = {str(repo_root)!r} + '/'; "
            "print('\\n'.join(sorted(os.path.abspath(getattr(m, '__file__', ''))[len(rp):] "
            "for m in list(sys.modules.values()) if getattr(m, '__file__', None) "
            "and os.path.abspath(m.__file__).startswith(rp))))"
        )
        proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=False)
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line:
                collector.test_to_files[lazy_load_num].add(line)

    # Explicit static bridges for meta-tests inspecting modules/packages
    typing_test_num = id_to_number.get("tests/test_typing.py::TestTyping::test_no_redundant_expression_metadata")
    if typing_test_num is not None:
        for p in (repo_root / "sqlglot" / "typing").glob("*.py"):
            collector.test_to_files[typing_test_num].add(p.relative_to(repo_root).as_posix())
        for p in (repo_root / "sqlglot" / "expressions").glob("*.py"):
            collector.test_to_files[typing_test_num].add(p.relative_to(repo_root).as_posix())

    for nodeid, num in id_to_number.items():
        if nodeid.startswith("tests/test_sqlglotc.py::"):
            for rel_cfg in ("sqlglotc/setup.py", "sqlglotc/pyproject.toml", "sqlglotc/MANIFEST.in", "pyproject.toml", "setup.py"):
                if (repo_root / rel_cfg).exists():
                    collector.test_to_files[num].add(rel_cfg)
        elif nodeid.startswith(("tests/test_dialect_entry_points.py::", "tests/test_dialect_imports.py::")):
            for p in (repo_root / "sqlglot" / "dialects").glob("*.py"):
                collector.test_to_files[num].add(p.relative_to(repo_root).as_posix())

    # Invert mappings
    file_to_tests: dict[str, list[int]] = defaultdict(list)
    symbol_to_tests: dict[str, list[int]] = defaultdict(list)
    class_to_tests: dict[str, list[int]] = defaultdict(list)

    for num, files in collector.test_to_files.items():
        for f in files:
            file_to_tests[f].append(num)

    for num, syms in collector.test_to_symbols.items():
        for s in syms:
            symbol_to_tests[s].append(num)

    for num, ckeys in collector.test_to_class_keys.items():
        for ck in ckeys:
            class_to_tests[ck].append(num)

    file_to_tests_sorted = {k: sorted(set(v)) for k, v in sorted(file_to_tests.items())}
    symbol_to_tests_sorted = {k: sorted(set(v)) for k, v in sorted(symbol_to_tests.items())}
    class_to_tests_sorted = {k: sorted(set(v)) for k, v in sorted(class_to_tests.items())}

    result = {
        "metadata": {
            "sqlglot_commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
            "coverage_duration_seconds": round(elapsed, 2),
            "total_tracked_tests": len(collector.test_to_files),
            "covered_source_files": len(file_to_tests_sorted),
            "covered_symbols": len(symbol_to_tests_sorted),
            "covered_classes": len(class_to_tests_sorted),
            "passed_tests_count": len(collector.passed_tests),
            "failed_tests_count": len(collector.failed_tests),
        },
        "file_to_tests": file_to_tests_sorted,
        "symbol_to_tests": symbol_to_tests_sorted,
        "class_to_tests": class_to_tests_sorted,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, separators=(",", ":"))

    print(f"Coverage map written to {args.out} ({args.out.stat().st_size / (1024*1024):.2f} MB)", file=sys.stderr)
    print(f"  Covered files:   {len(file_to_tests_sorted)}", file=sys.stderr)
    print(f"  Covered symbols: {len(symbol_to_tests_sorted)}", file=sys.stderr)
    print(f"  Covered classes: {len(class_to_tests_sorted)}", file=sys.stderr)


if __name__ == "__main__":
    main()
