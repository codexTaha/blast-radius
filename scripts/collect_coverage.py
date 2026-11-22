#!/usr/bin/env python3
"""Stage 2.5 — Full-Fidelity Ground-Truth Dynamic Coverage, Per-Key Registry & Class-Dispatch Collector.

Runs Pytest inside the exact competition Python 3.13 environment with:
1. In-process ProcessPoolExecutor monkeypatch (eliminates multiprocessing spawn blind spots
   in test_optimizer.py and test_executor.py).
2. PEP 669 sys.monitoring (PY_START) across sqlglot/, sqlglotc/, and tests/.
3. Runtime class/MRO dispatch hooks on Expr, Dialect, Tokenizer, JSONPathTokenizer,
   Parser, Generator, and TypeAnnotator.
4. Per-key runtime registry tracking across Parser (FUNCTIONS, FUNCTION_PARSERS,
   NO_PAREN_FUNCTION_PARSERS, SHOW_PARSERS, SET_PARSERS, PROPERTY_PARSERS,
   CONSTRAINT_PARSERS, ALTER_PARSERS, STATEMENT_PARSERS, RANGE_PARSERS, etc.),
   Tokenizer (NUMERIC_ESCAPES, NUMERIC_LITERALS, KEYWORDS, SINGLE_TOKENS),
   and Generator (TYPE_MAPPING + active Generator.generate dispatch).
5. File-open tracking (builtins.open / io.open) for fixtures, CSVs, and AST-parsed files.
6. unittest setUpClass attribution across all test methods of the class.

Produces artifacts/test_coverage.json.
"""

from __future__ import annotations

import argparse
import builtins
import concurrent.futures
import enum
import io
import json
import os
import pickle
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
            p_args, p_kwargs = pickle.loads(pickle.dumps((args, kwargs)))
            res = fn(*p_args, **p_kwargs)
            res = pickle.loads(pickle.dumps(res))
        except BaseException as exc:
            fut.set_exception(exc)
        else:
            fut.set_result(res)
        return fut

    def map(self, fn: Any, *iterables: Any, timeout: float | None = None, chunksize: int = 1) -> Any:
        results = []
        for items in zip(*iterables):
            p_items = pickle.loads(pickle.dumps(items))
            r = fn(*p_items)
            results.append(pickle.loads(pickle.dumps(r)))
        return iter(results)

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

PARSER_TRACKED_DICTS = (
    "FUNCTIONS",
    "FUNCTION_PARSERS",
    "NO_PAREN_FUNCTION_PARSERS",
    "NO_PAREN_FUNCTIONS",
    "PROPERTY_PARSERS",
    "CONSTRAINT_PARSERS",
    "ALTER_PARSERS",
    "STATEMENT_PARSERS",
    "RANGE_PARSERS",
    "PLACEHOLDER_PARSERS",
    "UNARY_PARSERS",
    "QUERY_MODIFIER_PARSERS",
    "TYPE_PARSERS",
    "TYPE_LITERAL_PARSERS",
    "LAMBDAS",
    "COLUMN_OPERATORS",
    "JSON_OPERATORS",
)

TOKENIZER_TRACKED_DICTS = (
    "NUMERIC_ESCAPES",
    "NUMERIC_LITERALS",
)

GENERATOR_TRACKED_DICTS = (
    "TYPE_MAPPING",
)

HIGH_FREQ_KEY_CAP = 250


def _canon_runtime_key(k: Any) -> str | None:
    if isinstance(k, str):
        return f"s:{k}"
    if isinstance(k, enum.Enum):
        cls_name = type(k).__name__
        if cls_name == "TokenType":
            return f"t:TokenType.{k.name}"
        if cls_name in ("Type", "DType"):
            return f"dt:{k.name}"
        return f"en:{cls_name}.{k.name}"
    if isinstance(k, type):
        return f"e:{k.__name__}"
    if isinstance(k, tuple) and all(isinstance(x, str) for x in k):
        return "u:" + "|".join(k)
    return None


class TrackedDict(dict):  # type: ignore[type-arg]
    """Lightweight dict wrapper that records per-key runtime lookups during active tests."""

    __slots__ = ("_collector", "_owner_cls", "_attr_name")

    def __init__(self, collector: DynamicCoverageCollector, owner_cls: type, attr_name: str, data: Any) -> None:
        super().__init__(data)
        self._collector = collector
        self._owner_cls = owner_cls
        self._attr_name = attr_name

    def get(self, key: Any, default: Any = None) -> Any:
        if self._collector.active:
            self._collector.record_dict_key(self._owner_cls, self._attr_name, key)
        return super().get(key, default)

    def __getitem__(self, key: Any) -> Any:
        if self._collector.active:
            self._collector.record_dict_key(self._owner_cls, self._attr_name, key)
        return super().__getitem__(key)

    def __contains__(self, key: Any) -> bool:
        if self._collector.active:
            self._collector.record_dict_key(self._owner_cls, self._attr_name, key)
        return super().__contains__(key)


class DynamicCoverageCollector:
    def __init__(self, repo_prefix: str, id_to_number: dict[str, int]):
        self.repo_prefix = repo_prefix
        self.id_to_number = id_to_number

        self.active: bool = False
        self.current_test_number: int | None = None
        self.current_files: set[str] = set()
        self.current_symbols: set[str] = set()
        self.current_qual_symbols: set[str] = set()
        self.current_lines: dict[str, set[int]] = defaultdict(set)
        self.current_classes: set[type] = set()
        self.current_active_gens: set[type] = set()
        self.current_raw_keys: set[tuple[type, str, str]] = set()
        self.current_tok_words: dict[type, set[str]] = defaultdict(set)

        self.in_setup_class: type | None = None
        self.setup_class_files: dict[type, set[str]] = defaultdict(set)
        self.setup_class_symbols: dict[type, set[str]] = defaultdict(set)
        self.setup_class_qual_symbols: dict[type, set[str]] = defaultdict(set)
        self.setup_class_lines: dict[type, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
        self.setup_class_classes: dict[type, set[type]] = defaultdict(set)
        self.setup_class_active_gens: dict[type, set[type]] = defaultdict(set)
        self.setup_class_raw_keys: dict[type, set[tuple[type, str, str]]] = defaultdict(set)
        self.setup_class_tok_words: dict[type, dict[type, set[str]]] = defaultdict(lambda: defaultdict(set))
        self.class_to_test_numbers: dict[type, list[int]] = defaultdict(list)

        self.test_to_files: dict[int, set[str]] = defaultdict(set)
        self.test_to_symbols: dict[int, set[str]] = defaultdict(set)
        self.test_to_qual_symbols: dict[int, set[str]] = defaultdict(set)
        self.line_to_test_sets: dict[str, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))
        self.test_to_class_keys: dict[int, set[str]] = defaultdict(set)
        self.test_to_active_gen_keys: dict[int, set[str]] = defaultdict(set)
        self.key_to_test_sets: dict[str, set[int]] = defaultdict(set)

        self.passed_tests: list[int] = []
        self.failed_tests: list[int] = []

        self._file_cache: dict[str, str | None] = {}
        self._line_enabled_codes: set[Any] = set()
        self._class_mro_cache: dict[type, tuple[set[str], set[str]]] = {}
        self._class_mro_list_cache: dict[type, list[str]] = {}
        self._wrapped_dict_classes: set[tuple[type, str]] = set()

        sys.monitoring.use_tool_id(TOOL_ID, "blast_radius_coverage")
        sys.monitoring.register_callback(TOOL_ID, sys.monitoring.events.PY_START, self.on_py_start)
        sys.monitoring.register_callback(TOOL_ID, sys.monitoring.events.LINE, self.on_line)

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

    def ensure_wrapped_dicts(self, cls: type, attr_names: tuple[str, ...]) -> None:
        mod_name = getattr(cls, "__module__", "")
        if not mod_name or not mod_name.startswith("sqlglot"):
            return
        for attr in attr_names:
            pair = (cls, attr)
            if pair in self._wrapped_dict_classes:
                continue
            self._wrapped_dict_classes.add(pair)
            val = getattr(cls, attr, None)
            if isinstance(val, dict):
                if not isinstance(val, TrackedDict) or val._owner_cls is not cls:
                    setattr(cls, attr, TrackedDict(self, cls, attr, val))

    def record_dict_key(self, owner_cls: type, attr_name: str, key: Any) -> None:
        ck = _canon_runtime_key(key)
        if ck is not None:
            item = (owner_cls, attr_name, ck)
            if self.in_setup_class is not None:
                self.setup_class_raw_keys[self.in_setup_class].add(item)
            else:
                self.current_raw_keys.add(item)

    def record_raw_key(self, owner_cls: type, attr_name: str, canon_key: str) -> None:
        item = (owner_cls, attr_name, canon_key)
        if self.in_setup_class is not None:
            self.setup_class_raw_keys[self.in_setup_class].add(item)
        else:
            self.current_raw_keys.add(item)

    def record_tok_words(self, tok_cls: type, words: set[str]) -> None:
        if not words:
            return
        if self.in_setup_class is not None:
            self.setup_class_tok_words[self.in_setup_class][tok_cls].update(words)
        else:
            self.current_tok_words[tok_cls].update(words)

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

    def record_active_gen(self, cls: type) -> None:
        if not self.active:
            return
        if self.in_setup_class is not None:
            self.setup_class_active_gens[self.in_setup_class].add(cls)
        else:
            self.current_active_gens.add(cls)

    def resolve_class_mro(self, cls: type) -> tuple[set[str], set[str]]:
        cached = self._class_mro_cache.get(cls)
        if cached is not None:
            return cached

        files: set[str] = set()
        class_keys: set[str] = set()
        mro_list: list[str] = []
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
                    ck = f"{rel}::{qname}"
                    class_keys.add(ck)
                    mro_list.append(ck)

        res = (files, class_keys)
        self._class_mro_cache[cls] = res
        self._class_mro_list_cache[cls] = mro_list
        return res

    def _mro_class_keys(self, cls: type) -> list[str]:
        if cls not in self._class_mro_list_cache:
            self.resolve_class_mro(cls)
        return self._class_mro_list_cache[cls]

    def on_py_start(self, code: Any, instruction_offset: int) -> Any:
        if not self.active:
            return sys.monitoring.DISABLE
        fn = code.co_filename
        if fn in self._file_cache:
            rel = self._file_cache[fn]
        else:
            rel = self._rel_repo_path(fn)
        if rel is not None:
            sym = f"{rel}::{code.co_name}"
            qsym = f"{rel}::{code.co_qualname}"
            if self.in_setup_class is not None:
                self.setup_class_files[self.in_setup_class].add(rel)
                self.setup_class_symbols[self.in_setup_class].add(sym)
                self.setup_class_qual_symbols[self.in_setup_class].add(qsym)
            else:
                self.current_files.add(rel)
                self.current_symbols.add(sym)
                self.current_qual_symbols.add(qsym)
            if not rel.startswith("tests/") and code not in self._line_enabled_codes:
                self._line_enabled_codes.add(code)
                sys.monitoring.set_local_events(TOOL_ID, code, sys.monitoring.events.LINE)
        return sys.monitoring.DISABLE

    def on_line(self, code: Any, line_number: int) -> Any:
        if not self.active:
            return sys.monitoring.DISABLE
        rel = self._file_cache.get(code.co_filename)
        if rel is not None and line_number > 0:
            if self.in_setup_class is not None:
                self.setup_class_lines[self.in_setup_class][rel].add(line_number)
            else:
                self.current_lines[rel].add(line_number)
        return sys.monitoring.DISABLE

    def install_sqlglot_hooks(self, repo_path: Path) -> None:
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

        # 3. Tokenizer & JSONPathTokenizer (wrap NUMERIC_ESCAPES BEFORE orig __init__!)
        for tok_cls in (tokens.Tokenizer, JSONPathTokenizer):
            orig_tok_init = tok_cls.__init__

            def make_tok_init(orig: Any) -> Any:
                def _hook(self_obj: Any, *args: Any, **kwargs: Any) -> None:
                    cls = type(self_obj)
                    collector.ensure_wrapped_dicts(cls, TOKENIZER_TRACKED_DICTS)
                    collector.record_class(cls)
                    orig(self_obj, *args, **kwargs)
                return _hook

            tok_cls.__init__ = make_tok_init(orig_tok_init)  # type: ignore[method-assign]

            orig_tokenize = tok_cls.tokenize

            def make_tokenize(orig: Any) -> Any:
                def _hook(self_obj: Any, *args: Any, **kwargs: Any) -> Any:
                    cls = type(self_obj)
                    collector.record_class(cls)
                    res = orig(self_obj, *args, **kwargs)
                    if collector.active and isinstance(res, list):
                        words = {
                            tok.text.upper()
                            for tok in res
                            if getattr(tok, "text", None)
                        }
                        collector.record_tok_words(cls, words)
                    return res
                return _hook

            tok_cls.tokenize = make_tokenize(orig_tokenize)  # type: ignore[method-assign]

        # 4. Parser.__init__, Parser.reset & Parser._find_parser
        orig_parser_init = parser.Parser.__init__

        def hooked_parser_init(self_obj: Any, *args: Any, **kwargs: Any) -> None:
            cls = type(self_obj)
            collector.ensure_wrapped_dicts(cls, PARSER_TRACKED_DICTS)
            collector.record_class(cls)
            orig_parser_init(self_obj, *args, **kwargs)

        parser.Parser.__init__ = hooked_parser_init  # type: ignore[method-assign]

        orig_parser_reset = parser.Parser.reset

        def hooked_parser_reset(self_obj: Any, *args: Any, **kwargs: Any) -> None:
            cls = type(self_obj)
            collector.ensure_wrapped_dicts(cls, PARSER_TRACKED_DICTS)
            collector.record_class(cls)
            orig_parser_reset(self_obj, *args, **kwargs)

        parser.Parser.reset = hooked_parser_reset  # type: ignore[method-assign]

        orig_find_parser = parser.Parser._find_parser

        def hooked_find_parser(self_obj: Any, parsers_dict: Any, trie: Any) -> Any:
            if collector.active and getattr(self_obj, "_curr", None):
                attr_name = None
                if parsers_dict is getattr(self_obj, "SHOW_PARSERS", None):
                    attr_name = "SHOW_PARSERS"
                elif parsers_dict is getattr(self_obj, "SET_PARSERS", None):
                    attr_name = "SET_PARSERS"
                if attr_name is not None:
                    first_word = self_obj._curr.text.upper().split(" ")[0]
                    if first_word:
                        collector.record_raw_key(type(self_obj), attr_name, f"w:{first_word}")
            return orig_find_parser(self_obj, parsers_dict, trie)

        parser.Parser._find_parser = hooked_find_parser  # type: ignore[method-assign]

        # 5. Generator.__init__ & Generator.generate
        orig_gen_init = generator.Generator.__init__

        def hooked_gen_init(self_obj: Any, *args: Any, **kwargs: Any) -> None:
            cls = type(self_obj)
            collector.ensure_wrapped_dicts(cls, GENERATOR_TRACKED_DICTS)
            collector.record_class(cls)
            orig_gen_init(self_obj, *args, **kwargs)

        generator.Generator.__init__ = hooked_gen_init  # type: ignore[method-assign]

        orig_gen_generate = generator.Generator.generate

        def hooked_gen_generate(self_obj: Any, *args: Any, **kwargs: Any) -> Any:
            cls = type(self_obj)
            collector.ensure_wrapped_dicts(cls, GENERATOR_TRACKED_DICTS)
            collector.record_class(cls)
            collector.record_active_gen(cls)
            return orig_gen_generate(self_obj, *args, **kwargs)

        generator.Generator.generate = hooked_gen_generate  # type: ignore[method-assign]

        # 6. TypeAnnotator.__init__
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
                                sys.monitoring.restart_events()
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
        self.current_qual_symbols = set()
        self.current_lines = defaultdict(set)
        self.current_classes = set()
        self.current_active_gens = set()
        self.current_raw_keys = set()
        self.current_tok_words = defaultdict(set)

        if self.current_test_number is not None:
            self.active = True
            sys.monitoring.set_events(TOOL_ID, sys.monitoring.events.PY_START)
            sys.monitoring.restart_events()

    def _flush_keys_for_test(
        self,
        tnum: int,
        raw_keys: set[tuple[type, str, str]],
        tok_words: dict[type, set[str]],
    ) -> None:
        for owner_cls, attr_name, ck in raw_keys:
            for base_ckey in self._mro_class_keys(owner_cls):
                self.key_to_test_sets[f"{base_ckey}.{attr_name}[{ck}]"].add(tnum)

        for tok_cls, words in tok_words.items():
            mro_ckeys = self._mro_class_keys(tok_cls)
            for base_ckey in mro_ckeys:
                prefix = f"{base_ckey}.KEYWORDS[s:"
                for w in words:
                    full_k = f"{prefix}{w}]"
                    s = self.key_to_test_sets[full_k]
                    if len(s) <= HIGH_FREQ_KEY_CAP:
                        s.add(tnum)

    def pytest_runtest_teardown(self, item: pytest.Item) -> None:
        self.active = False
        sys.monitoring.set_events(TOOL_ID, sys.monitoring.events.NO_EVENTS)

        tnum = self.current_test_number
        if tnum is not None:
            files = set(self.current_files)
            symbols = set(self.current_symbols)
            class_keys: set[str] = set()
            active_gen_keys: set[str] = set()

            for cls in self.current_classes:
                c_files, c_keys = self.resolve_class_mro(cls)
                files.update(c_files)
                class_keys.update(c_keys)

            for gcls in self.current_active_gens:
                _, g_keys = self.resolve_class_mro(gcls)
                active_gen_keys.update(g_keys)

            self.test_to_files[tnum].update(files)
            self.test_to_symbols[tnum].update(symbols)
            self.test_to_qual_symbols[tnum].update(self.current_qual_symbols)
            for rel_f, lnums in self.current_lines.items():
                fmap = self.line_to_test_sets[rel_f]
                for ln in lnums:
                    s = fmap[ln]
                    if len(s) <= 220:
                        s.add(tnum)
            self.test_to_class_keys[tnum].update(class_keys)
            self.test_to_active_gen_keys[tnum].update(active_gen_keys)

            self._flush_keys_for_test(tnum, self.current_raw_keys, self.current_tok_words)

        self.current_test_number = None

    def finalize_setup_classes(self) -> None:
        for cls, tnums in self.class_to_test_numbers.items():
            for base in cls.__mro__:
                has_sc = (
                    base in self.setup_class_files
                    or base in self.setup_class_classes
                    or base in self.setup_class_symbols
                    or base in self.setup_class_qual_symbols
                    or base in self.setup_class_lines
                    or base in self.setup_class_active_gens
                    or base in self.setup_class_raw_keys
                    or base in self.setup_class_tok_words
                )
                if not has_sc:
                    continue
                sc_files = set(self.setup_class_files.get(base, set()))
                sc_syms = set(self.setup_class_symbols.get(base, set()))
                sc_qsyms = set(self.setup_class_qual_symbols.get(base, set()))
                sc_lines = self.setup_class_lines.get(base, {})
                sc_ckeys: set[str] = set()
                sc_gkeys: set[str] = set()
                for c in self.setup_class_classes.get(base, set()):
                    cf, ck = self.resolve_class_mro(c)
                    sc_files.update(cf)
                    sc_ckeys.update(ck)
                for gc in self.setup_class_active_gens.get(base, set()):
                    _, gk = self.resolve_class_mro(gc)
                    sc_gkeys.update(gk)
                sc_rkeys = self.setup_class_raw_keys.get(base, set())
                sc_twords = self.setup_class_tok_words.get(base, {})
                for tnum in tnums:
                    self.test_to_files[tnum].update(sc_files)
                    self.test_to_symbols[tnum].update(sc_syms)
                    self.test_to_qual_symbols[tnum].update(sc_qsyms)
                    for rel_f, lnums in sc_lines.items():
                        fmap = self.line_to_test_sets[rel_f]
                        for ln in lnums:
                            s = fmap[ln]
                            if len(s) <= 220:
                                s.add(tnum)
                    self.test_to_class_keys[tnum].update(sc_ckeys)
                    self.test_to_active_gen_keys[tnum].update(sc_gkeys)
                    if sc_rkeys or sc_twords:
                        self._flush_keys_for_test(tnum, sc_rkeys, sc_twords)

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
    print(f"Running test suite in {test_dirs} (in-process ProcessPool + MRO + Per-Key hooks) ...", file=sys.stderr)
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

    # Static bridges for subprocess-isolated test_lazy_load (#214) and meta-tests
    lazy_load_num = id_to_number.get("tests/dialects/test_dialect.py::TestDialect::test_lazy_load")
    if lazy_load_num is not None:
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
    qual_symbol_to_tests: dict[str, list[int]] = defaultdict(list)
    class_to_tests: dict[str, list[int]] = defaultdict(list)
    active_gen_to_tests: dict[str, list[int]] = defaultdict(list)

    for num, files in collector.test_to_files.items():
        for f in files:
            file_to_tests[f].append(num)

    for num, syms in collector.test_to_symbols.items():
        for s in syms:
            symbol_to_tests[s].append(num)

    for num, qsyms in collector.test_to_qual_symbols.items():
        for qs in qsyms:
            qual_symbol_to_tests[qs].append(num)

    for num, ckeys in collector.test_to_class_keys.items():
        for ck in ckeys:
            class_to_tests[ck].append(num)

    for num, gkeys in collector.test_to_active_gen_keys.items():
        for gk in gkeys:
            active_gen_to_tests[gk].append(num)

    line_to_tests_sorted: dict[str, dict[str, list[int]]] = {}
    max_nodes_num = id_to_number.get("tests/test_expressions.py::TestExpressions::test_max_nodes")
    max_nodes_lines: dict[str, list[int]] = {}
    total_lines_tracked = 0
    for rel_f in sorted(collector.line_to_test_sets.keys()):
        f_lines = collector.line_to_test_sets[rel_f]
        if not f_lines:
            continue
        f_out: dict[str, list[int]] = {}
        mn_lns: list[int] = []
        for ln in sorted(f_lines.keys()):
            tset = f_lines[ln]
            if not tset:
                continue
            total_lines_tracked += 1
            if max_nodes_num is not None and max_nodes_num in tset:
                mn_lns.append(ln)
            f_out[str(ln)] = [-1] if len(tset) > 220 else sorted(tset)
        if f_out:
            line_to_tests_sorted[rel_f] = f_out
        if mn_lns:
            max_nodes_lines[rel_f] = mn_lns

    key_to_tests_sorted: dict[str, list[int]] = {}
    for k in sorted(collector.key_to_test_sets.keys()):
        tset = collector.key_to_test_sets[k]
        if ".KEYWORDS[s:" in k and len(tset) > HIGH_FREQ_KEY_CAP:
            key_to_tests_sorted[k] = [-1]
        else:
            key_to_tests_sorted[k] = sorted(tset)

    file_to_tests_sorted = {k: sorted(set(v)) for k, v in sorted(file_to_tests.items())}
    symbol_to_tests_sorted = {k: sorted(set(v)) for k, v in sorted(symbol_to_tests.items())}
    qual_symbol_sorted = {
        k: ([-1] if len(v) > 220 else sorted(set(v)))
        for k, v in sorted(qual_symbol_to_tests.items())
    }
    class_to_tests_sorted = {k: sorted(set(v)) for k, v in sorted(class_to_tests.items())}
    active_gen_sorted = {k: sorted(set(v)) for k, v in sorted(active_gen_to_tests.items())}

    result = {
        "metadata": {
            "sqlglot_commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
            "coverage_duration_seconds": round(elapsed, 2),
            "total_tracked_tests": len(collector.test_to_files),
            "covered_source_files": len(file_to_tests_sorted),
            "covered_symbols": len(symbol_to_tests_sorted),
            "covered_qual_symbols": len(qual_symbol_sorted),
            "covered_lines": total_lines_tracked,
            "covered_classes": len(class_to_tests_sorted),
            "covered_registry_keys": len(key_to_tests_sorted),
            "passed_tests_count": len(collector.passed_tests),
            "failed_tests_count": len(collector.failed_tests),
        },
        "file_to_tests": file_to_tests_sorted,
        "symbol_to_tests": symbol_to_tests_sorted,
        "qual_symbol_to_tests": qual_symbol_sorted,
        "line_to_tests": line_to_tests_sorted,
        "max_nodes_lines": max_nodes_lines,
        "class_to_tests": class_to_tests_sorted,
        "active_gen_to_tests": active_gen_sorted,
        "key_to_tests": key_to_tests_sorted,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, separators=(",", ":"))

    print(f"Coverage map written to {args.out} ({args.out.stat().st_size / (1024*1024):.2f} MB)", file=sys.stderr)
    print(f"  Covered files:         {len(file_to_tests_sorted)}", file=sys.stderr)
    print(f"  Covered symbols:       {len(symbol_to_tests_sorted)}", file=sys.stderr)
    print(f"  Covered classes:       {len(class_to_tests_sorted)}", file=sys.stderr)
    print(f"  Covered registry keys: {len(key_to_tests_sorted)}", file=sys.stderr)


if __name__ == "__main__":
    main()
