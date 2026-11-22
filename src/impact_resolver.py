"""Stage 3.2 — Full-Fidelity Multi-Granularity Impact Resolver (with Stage 1 Per-Key & Per-Attribute Tracking).

Resolves Git unified diff patches into minimal, 100%-recall test selections using:
1. Tokenize-verified Python '#' comment filtering (with multiline-string & PEP 263 guards)
2. Direct test-method, test-helper (Validator / helpers.py), and open-traced fixture resolution
3. Exact per-key registry dependency tracking (FUNCTIONS, TRANSFORMS, KEYWORDS, NUMERIC_ESCAPES,
   SHOW_PARSERS, SET_PARSERS, PROPERTY_PARSERS, EXPRESSION_METADATA, TYPE_MAPPING, etc.)
4. Exact per-attribute runtime reader intersection for safe non-init class settings (e.g. WEEK_OFFSET)
5. Factory-aware & metaclass-safe AST function-body dynamic coverage (in-process + multiprocess)
6. Exact runtime MRO class-dispatch coverage (Expr, Dialect, Tokenizer, Parser, Generator, TypeAnnotator)
7. Full module dynamic coverage + package-aware static import-graph fallback
"""

from __future__ import annotations

import ast
import json
import marshal
import textwrap
from pathlib import Path
from typing import Any

from src.diff_parser import FileDiff, Hunk, ParsedPatch, parse_patch

SPECIAL_NON_BODY_METHODS = frozenset({
    "__init_subclass__",
    "__reduce__",
    "__reduce_ex__",
    "__getstate__",
    "__setstate__",
})

DIRECT_KEY_REGISTRIES = frozenset({
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
    "TYPE_MAPPING",
    "NUMERIC_LITERALS",
})

WORD_PREFIX_REGISTRIES = frozenset({
    "SHOW_PARSERS",
    "SET_PARSERS",
})

EXPR_DISPATCH_REGISTRIES = frozenset({
    "TRANSFORMS",
    "PROPERTIES_LOCATION",
})

ALL_KEY_TRACKED_REGISTRIES = (
    DIRECT_KEY_REGISTRIES
    | WORD_PREFIX_REGISTRIES
    | EXPR_DISPATCH_REGISTRIES
    | {"NUMERIC_ESCAPES", "KEYWORDS", "EXPRESSION_METADATA", "arg_types"}
)

PURE_ENUM_CLASSES = frozenset({
    "TrieResult",
    "IsPredicatePrecedence",
    "ErrorLevel",
})

STR_ENUM_CLASSES = frozenset({
    "NormalizationStrategy",
    "UsingColumnOrder",
    "AliasPrecedence",
})

VERIFIED_STR_ENUM_EXPR_UNPARSED = frozenset({
    "self.normalization_strategy",
    "self.NORMALIZATION_STRATEGY",
    "strategy",
    "self.USING_COLUMN_ORDER",
    "dialect.normalization_strategy",
    "dialect.NORMALIZATION_STRATEGY",
})

COPY_WRAPPER_FUNCS = frozenset({
    "exp.alias_",
    "alias_",
    "exp.maybe_parse",
    "maybe_parse",
    "exp.condition",
    "condition",
    "exp.to_table",
    "to_table",
    "exp.to_column",
    "to_column",
})

FRESH_AST_CONSTRUCTORS = frozenset({
    "exp.table_",
    "table_",
    "exp.column",
    "column",
    "exp.var",
    "var",
    "exp.to_identifier",
    "to_identifier",
    "exp.Literal.string",
    "exp.Literal.number",
})


def _is_pure_ref_expr(node: ast.expr) -> bool:
    """Verify expression is a side-effect-free variable or attribute lookup chain."""
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, ast.Attribute):
        return _is_pure_ref_expr(node.value)
    return False


def _is_fresh_ast_constructor_call(node: ast.expr) -> bool:
    """Verify `node` is an inline call to a pure SQLGlot AST node factory returning a fresh unreferenced Expr."""
    if not isinstance(node, ast.Call):
        return False
    if ast.unparse(node.func) not in FRESH_AST_CONSTRUCTORS:
        return False
    for a in node.args:
        if isinstance(a, ast.Starred) or not (_is_pure_ref_expr(a) or isinstance(a, ast.Constant)):
            return False
    for kw in node.keywords:
        if kw.arg is None or not (_is_pure_ref_expr(kw.value) or isinstance(kw.value, ast.Constant)):
            return False
    return True


class _ProvenEquivalenceCanonicalizer(ast.NodeTransformer):
    """Canonicalizes narrowly proven behavior-preserving AST forms."""

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        if len(node.ops) != 1 or len(node.comparators) != 1:
            return node
        op = node.ops[0]
        if not isinstance(op, (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)):
            return node
        lhs = node.left
        rhs = node.comparators[0]

        # Rule 2A: `x is None` <-> `x == None` and `x is not None` <-> `x != None`
        # Proven across all SQLGlot classes except `reverse_key` in `sqlglot/executor/env.py`.
        if (
            self.path != "sqlglot/executor/env.py"
            and isinstance(rhs, ast.Constant)
            and rhs.value is None
            and _is_pure_ref_expr(lhs)
        ):
            new_op: ast.cmpop = ast.Is() if isinstance(op, (ast.Is, ast.Eq)) else ast.IsNot()
            return ast.copy_location(ast.Compare(left=lhs, ops=[new_op], comparators=[rhs]), node)

        # Rule 2B: `x is EnumCls.MEMBER` <-> `x == EnumCls.MEMBER`
        if (
            isinstance(rhs, ast.Attribute)
            and isinstance(rhs.value, ast.Name)
            and rhs.attr.isupper()
            and _is_pure_ref_expr(lhs)
        ):
            enum_cls = rhs.value.id
            is_safe_enum = enum_cls in PURE_ENUM_CLASSES or (
                enum_cls in STR_ENUM_CLASSES and ast.unparse(lhs) in VERIFIED_STR_ENUM_EXPR_UNPARSED
            )
            if is_safe_enum:
                new_op = ast.Is() if isinstance(op, (ast.Is, ast.Eq)) else ast.IsNot()
                return ast.copy_location(ast.Compare(left=lhs, ops=[new_op], comparators=[rhs]), node)

        return node

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        # Rule 2C: `copy=False` <-> `copy=True` when arg0 is an inline fresh AST constructor rvalue
        if (
            node.args
            and ast.unparse(node.func) in COPY_WRAPPER_FUNCS
            and _is_fresh_ast_constructor_call(node.args[0])
        ):
            new_kws: list[ast.keyword] = []
            modified = False
            for kw in node.keywords:
                if (
                    kw.arg == "copy"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, bool)
                ):
                    new_kws.append(
                        ast.copy_location(
                            ast.keyword(arg="copy", value=ast.Constant(value=True)),
                            kw,
                        )
                    )
                    modified = True
                else:
                    new_kws.append(kw)
            if modified:
                return ast.copy_location(
                    ast.Call(func=node.func, args=node.args, keywords=new_kws),
                    node,
                )
        return node


def _parse_single_line_ast_unit(line: str) -> tuple[str, ast.AST] | None:
    """Parse a single stripped line as a single statement, suite header, or ternary if-clause."""
    s = line.strip()
    if not s or s.startswith("#") or ";" in s:
        return None
    try:
        mod = ast.parse(s)
        if len(mod.body) == 1:
            return ("stmt", mod)
    except SyntaxError:
        pass
    if s.endswith(":"):
        hdr = s[4:].strip() if s.startswith("elif ") else s
        if hdr.startswith(("if ", "while ")):
            try:
                mod = ast.parse(hdr + "\n    pass")
                if len(mod.body) == 1:
                    return (s.split()[0], mod)
            except SyntaxError:
                pass
    if s.startswith("if ") and not s.endswith(":"):
        try:
            mod = ast.parse(f"_x {s} else _y")
            if len(mod.body) == 1:
                return ("ternary_if", mod)
        except SyntaxError:
            pass
    return None


def _canon_ast_key(k: ast.expr) -> str | None:
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


def _is_import_safe_expr(node: ast.expr | None) -> bool:
    """Conservatively check that evaluating `node` at module/class import time cannot raise."""
    if node is None:
        return True
    if isinstance(node, (ast.Constant, ast.Lambda)):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd, ast.Not, ast.Invert)):
        return _is_import_safe_expr(node.operand)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_import_safe_expr(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            k is not None and _is_import_safe_expr(k) and _is_import_safe_expr(v)
            for k, v in zip(node.keys, node.values)
        )
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, ast.Attribute):
        return _is_import_safe_expr(node.value)
    if isinstance(node, ast.Call):
        # Allow standard SQLGlot builder factories with safe arguments (e.g. _show_parser(...), build_formatted_time(...))
        if _is_import_safe_expr(node.func):
            return all(_is_import_safe_expr(a) for a in node.args) and all(
                _is_import_safe_expr(kw.value) for kw in node.keywords
            )
    return False


def _strip_strings_and_comments_for_balance(line: str) -> list[int]:
    """Return [paren_delta, bracket_delta, brace_delta] outside simple quotes and '#' comments."""
    p_d = b_d = c_d = 0
    in_quote: str | None = None
    escaped = False
    for ch in line:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if in_quote is not None:
            if ch == in_quote:
                in_quote = None
            continue
        if ch in ("'", '"'):
            in_quote = ch
            continue
        if ch == "#":
            break
        if ch == "(":
            p_d += 1
        elif ch == ")":
            p_d -= 1
        elif ch == "[":
            b_d += 1
        elif ch == "]":
            b_d -= 1
        elif ch == "{":
            c_d += 1
        elif ch == "}":
            c_d -= 1
    return [p_d, b_d, c_d]


def _hunk_has_balanced_delimiters(h: Hunk) -> bool:
    add_tot = [0, 0, 0]
    del_tot = [0, 0, 0]
    for _, txt in h.added_lines:
        if "===" in txt:
            return False
        d = _strip_strings_and_comments_for_balance(txt)
        add_tot[0] += d[0]
        add_tot[1] += d[1]
        add_tot[2] += d[2]
    for _, txt in h.deleted_lines:
        d = _strip_strings_and_comments_for_balance(txt)
        del_tot[0] += d[0]
        del_tot[1] += d[1]
        del_tot[2] += d[2]
    return add_tot == del_tot


def _parse_dict_fragment(lines: list[tuple[int, str]], check_safe_values: bool) -> dict[str, str] | None:
    """Try parsing a block of dict entry lines as `{ ... }`. Returns {canon_key: unparsed_val} or None."""
    if not lines:
        return {}
    raw = textwrap.dedent("\n".join(txt for _, txt in lines))
    if not raw.strip():
        return {}
    try:
        mod = ast.parse("{\n" + raw + "\n}")
    except SyntaxError:
        return None
    if len(mod.body) != 1 or not isinstance(mod.body[0], ast.Expr) or not isinstance(mod.body[0].value, ast.Dict):
        return None
    dnode = mod.body[0].value
    out: dict[str, str] = {}
    for dk, dv in zip(dnode.keys, dnode.values):
        if dk is None or dv is None:
            return None  # **spread cannot be scoped to a single key
        ck = _canon_ast_key(dk)
        if ck is None:
            return None
        if check_safe_values and not _is_import_safe_expr(dv):
            return None
        out[ck] = ast.unparse(dv)
    return out


def _parse_full_dict_assign(lines: list[tuple[int, str]], expected_attr: str, check_safe_values: bool) -> tuple[dict[str, str], list[str]] | None:
    """Parse a full `ATTR = {...}` statement; returns (key_map, spreads_list) or None."""
    raw = textwrap.dedent("\n".join(txt for _, txt in lines))
    try:
        mod = ast.parse(raw)
    except SyntaxError:
        return None
    if len(mod.body) != 1:
        return None
    stmt = mod.body[0]
    val_node: ast.expr | None = None
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        if stmt.targets[0].id != expected_attr:
            return None
        val_node = stmt.value
    elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        if stmt.target.id != expected_attr:
            return None
        val_node = stmt.value
    else:
        return None

    if not isinstance(val_node, ast.Dict):
        return None
    kv: dict[str, str] = {}
    spreads: list[str] = []
    for dk, dv in zip(val_node.keys, val_node.values):
        if dv is None:
            return None
        if dk is None:
            spreads.append(ast.unparse(dv))
            continue
        ck = _canon_ast_key(dk)
        if ck is None:
            return None
        if check_safe_values and not _is_import_safe_expr(dv):
            return None
        kv[ck] = ast.unparse(dv)
    return kv, spreads


class ImpactResolver:
    """Multi-granularity test impact prediction engine."""

    def __init__(self, bundle_or_path: Path | str | dict[str, Any]) -> None:
        if isinstance(bundle_or_path, dict):
            bundle = bundle_or_path
        else:
            bundle_path = Path(bundle_or_path)
            marshal_path = bundle_path.with_suffix(".marshal")
            if marshal_path.exists():
                bundle = marshal.loads(marshal_path.read_bytes())
            else:
                bundle = json.loads(bundle_path.read_text(encoding="utf-8"))

        self.test_durations: list[int] = bundle["test_durations"]
        self.by_file: dict[str, list[int]] = bundle["by_file"]
        self.by_id: dict[str, int] = bundle.get("by_id", {})
        self.fixture_to_tests: dict[str, list[int]] = bundle["fixture_to_tests"]
        self.dialect_to_tests: dict[str, list[int]] = bundle["dialect_to_tests"]
        self.file_to_dialect: dict[str, str] = bundle["file_to_dialect"]
        self.core_engine_files: set[str] = set(bundle["core_engine_files"])
        self.file_to_tests: dict[str, list[int]] = bundle["file_to_tests"]
        self.symbol_to_tests: dict[str, list[int]] = bundle["symbol_to_tests"]
        self.qual_symbol_to_tests: dict[str, list[int]] = bundle.get("qual_symbol_to_tests", {})
        self.func_stmt_cov: dict[str, list[list[Any]]] = bundle.get("func_stmt_cov", {})
        self.class_to_tests: dict[str, list[int]] = bundle.get("class_to_tests", {})
        self.active_gen_to_tests: dict[str, list[int]] = bundle.get("active_gen_to_tests", {})
        self.expr_to_tests: dict[str, list[int]] = bundle.get("expr_to_tests", {})
        self.key_to_tests: dict[str, list[int]] = bundle.get("key_to_tests", {})
        self.word_to_tests: dict[str, list[int]] = bundle.get("word_to_tests", {})
        self.attr_reader_tests: dict[str, list[int]] = bundle.get("attr_reader_tests", {})
        self.cross_attr_deps: dict[str, list[str]] = bundle.get("cross_attr_deps", {})
        self.test_attr_refs: dict[str, list[str]] = bundle.get("test_attr_refs", {})
        self.file_symbols: dict[str, list[list[Any]]] = bundle["file_symbols"]
        self.multiline_strings: dict[str, list[list[int]]] = bundle.get("multiline_strings", {})
        self.test_dependents: dict[str, list[str]] = bundle["test_dependents"]
        self.gen_memory_test_num: int | None = self.by_id.get(
            "tests/test_generator.py::TestGenerator::test_generator_memory"
        )

    @staticmethod
    def _file_to_module(path: str) -> str:
        """Convert repository file path (including package __init__.py) to Python module name."""
        p = path[:-3] if path.endswith(".py") else path
        if p.endswith("/__init__"):
            p = p[:-9]
        elif p == "__init__":
            return ""
        return p.replace("/", ".")

    @staticmethod
    def _symbol_to_key(path: str, qname: str) -> str:
        """Convert AST symbol qualified name to runtime bytecode symbol key."""
        func_name = qname.split(".")[-1]
        return f"{path}::{func_name}"

    def _is_hunk_safe_comment_only(self, h: Hunk, path: str) -> bool:
        if not h.is_comment_only or not path.endswith(".py") or path.startswith("tests/fixtures/"):
            return False
        mls = self.multiline_strings.get(path)
        if mls:
            for del_ln, _ in h.deleted_lines:
                if any(s_row <= del_ln <= e_row for s_row, e_row in mls):
                    return False
            if not h.deleted_lines and h.modified_lines:
                prev_ln = max(1, min(h.modified_lines) - 1)
                next_ln = max(h.modified_lines)
                if any(s_row <= prev_ln and next_ln <= e_row for s_row, e_row in mls):
                    return False
            for ln in h.modified_lines:
                if any(s_row < ln < e_row for s_row, e_row in mls):
                    return False
        return True

    def _is_safe_comment_only(self, f: FileDiff, path: str) -> bool:
        """Verify file diff is strictly a Python '#' comment outside all multiline string literals."""
        if not f.is_comment_only or not path.endswith(".py"):
            return False
        if path.startswith("tests/fixtures/"):
            return False
        mls = self.multiline_strings.get(path)
        if mls:
            for h in f.hunks:
                for del_ln, _ in h.deleted_lines:
                    if any(s_row <= del_ln <= e_row for s_row, e_row in mls):
                        return False
            for prev_ln, next_ln in f.insertion_anchors:
                if any(s_row <= prev_ln and next_ln <= e_row for s_row, e_row in mls):
                    return False
            for ln in f.modified_lines:
                if any(s_row < ln < e_row for s_row, e_row in mls):
                    return False
        return True

    def _is_syntactically_clean_py(self, f: FileDiff) -> bool:
        if f.is_new or f.is_deleted or f.is_rename or f.is_binary or not f.modified_ranges:
            return False
        return all(_hunk_has_balanced_delimiters(h) for h in f.hunks)

    def _effective_spans(self, f: FileDiff) -> list[tuple[int, int]]:
        spans = list(f.modified_ranges)
        if f.insertion_anchors:
            spans.extend(f.insertion_anchors)
        return spans

    @staticmethod
    def _hunk_span(h: Hunk) -> tuple[int, int]:
        if h.deleted_lines:
            lns = [ln for ln, _ in h.deleted_lines]
            return min(lns), max(lns)
        if h.modified_lines:
            m_min = min(h.modified_lines)
            return max(1, m_min - 1), max(h.modified_lines)
        return max(1, h.old_start), max(1, h.old_start)

    def _extract_changed_dict_keys(
        self,
        h: Hunk,
        h_s: int,
        h_e: int,
        attr_entry: list[Any],
        syms: list[list[Any]],
    ) -> tuple[set[str], bool] | None:
        """Extract exact changed canonical keys and whether key-set membership changed, or None if uncertain."""
        a_start, _, a_end, attr_qual, _ = attr_entry
        attr_name = attr_qual.rsplit(".", 1)[-1]
        prefix = attr_qual + "["
        outer_key_entries = [s for s in syms if s[4] == 4 and s[3].startswith(prefix)]
        baseline_keys = {s[3][len(prefix) : -1] for s in outer_key_entries}

        # Case 1: Full single-statement dict replacement (e.g., single-line NUMERIC_ESCAPES = {...})
        if a_start == h_s and h_e == a_end and h.deleted_lines and h.added_lines:
            old_parsed = _parse_full_dict_assign(h.deleted_lines, attr_name, check_safe_values=False)
            new_parsed = _parse_full_dict_assign(h.added_lines, attr_name, check_safe_values=True)
            if old_parsed is not None and new_parsed is not None:
                old_kv, old_sp = old_parsed
                new_kv, new_sp = new_parsed
                if old_sp == new_sp:
                    changed = {k for k in (old_kv.keys() | new_kv.keys()) if old_kv.get(k) != new_kv.get(k)}
                    keyset_changed = set(old_kv.keys()) != set(new_kv.keys())
                    return changed, keyset_changed

        # Case 2: Strictly inside a multi-line dict body
        if not (a_start < h_s and h_e <= a_end):
            return None

        # Subcase 2a: Outer dict-entry fragment(s) (handles single-line edits, additions, deletions, renames)
        strictly_inside_multiline_val = any(
            ks[0] < h_s and h_e < ks[2] for ks in outer_key_entries
        )
        if not strictly_inside_multiline_val:
            del_kv = _parse_dict_fragment(h.deleted_lines, check_safe_values=False)
            add_kv = _parse_dict_fragment(h.added_lines, check_safe_values=True)
            if del_kv is not None and add_kv is not None and (del_kv or add_kv):
                # Verify deleted keys truly belong to outer dict (not an inner nested dict!)
                if all(k in baseline_keys for k in del_kv):
                    if attr_name == "arg_types":
                        if not (
                            all(v in ("True", "False") for v in del_kv.values())
                            and all(v in ("True", "False") for v in add_kv.values())
                        ):
                            return None
                    changed = {k for k in (del_kv.keys() | add_kv.keys()) if del_kv.get(k) != add_kv.get(k)}
                    # If a key was deleted or added without counterpart, include it
                    changed.update(set(del_kv.keys()) ^ set(add_kv.keys()))
                    keyset_changed = set(del_kv.keys()) != set(add_kv.keys())
                    return changed, keyset_changed

        # Subcase 2b: Strictly inside the multi-line value of a single outer dict key (e.g. 6ze8 lambda body)
        if h.deleted_lines:
            matching_outer = [ks for ks in outer_key_entries if ks[0] < h_s and h_e < ks[2]]
            if len(matching_outer) == 1:
                ck = matching_outer[0][3][len(prefix) : -1]
                return {ck}, False

        return None

    def _resolve_registry_keys(
        self,
        p: str,
        attr_qual: str,
        affected_keys: set[str],
        keyset_changed: bool,
        syms: list[list[Any]],
    ) -> set[int] | None:
        """Map affected registry keys to exact dependent tests, or None to trigger conservative fallback."""
        attr_name = attr_qual.rsplit(".", 1)[-1]
        if attr_name not in ALL_KEY_TRACKED_REGISTRIES:
            return None

        full_attr_id = f"{p}::{attr_qual}"
        extra_test_files = self.test_attr_refs.get(full_attr_id, [])
        out: set[int] = set()
        for tf in extra_test_files:
            out.update(self.by_file.get(tf, []))

        if attr_name == "EXPRESSION_METADATA":
            ftests = set(self.file_to_tests.get(p, []))
            if not ftests:
                return None
            for ck in affected_keys:
                if not ck.startswith("e:"):
                    return None
                etests = set(self.expr_to_tests.get(ck[2:], []))
                out.update(ftests & etests)
            out.update(self.by_file.get("tests/test_typing.py", []))
            return out

        if "." not in attr_qual:
            return None
        cls_qual = attr_qual.rsplit(".", 1)[0]
        ckey = f"{p}::{cls_qual}"
        if ckey not in self.class_to_tests:
            return None

        target_classes = [ckey, *self.cross_attr_deps.get(full_attr_id, [])]

        if not affected_keys:
            return out

        if attr_name == "arg_types":
            if keyset_changed or any(tc != ckey for tc in target_classes):
                return None
            for ck in affected_keys:
                res = self.key_to_tests.get(f"{ckey}.arg_types[{ck}]")
                if res is None or res == [-1]:
                    return None
                out.update(res)
            return out

        if attr_name == "NUMERIC_ESCAPES":
            for ck in affected_keys:
                for tc in target_classes:
                    res = self.key_to_tests.get(f"{tc}.NUMERIC_ESCAPES[{ck}]", [])
                    if res == [-1]:
                        return None
                    out.update(res)
            if keyset_changed:
                # Key addition/deletion can affect ESCAPED_SEQUENCES / bytestring_sql for this dialect
                d_name = self.file_to_dialect.get(p)
                d_ckeys = [tc for tc in self.class_to_tests if d_name and f"/{d_name}.py::" in tc]
                cls_pool = set(self.class_to_tests.get(ckey, []))
                for dc in d_ckeys:
                    cls_pool.update(self.class_to_tests.get(dc, []))
                esc_callers = set(self.symbol_to_tests.get("sqlglot/generator.py::escape_str", [])) | set(
                    self.symbol_to_tests.get("sqlglot/generator.py::bytestring_sql", [])
                )
                out.update(cls_pool & esc_callers)
            return out

        if attr_name in WORD_PREFIX_REGISTRIES:
            prefix = attr_qual + "["
            baseline_keys = {s[3][len(prefix) : -1] for s in syms if s[4] == 4 and s[3].startswith(prefix)}
            upper_baseline = {k[2:].strip().upper() for k in baseline_keys if k.startswith("s:")}
            for ck in affected_keys:
                if not ck.startswith("s:"):
                    return None
                words = ck[2:].strip().upper().split()
                if not words:
                    return None
                if (
                    not keyset_changed
                    and ck in baseline_keys
                    and len(words) > 1
                    and any(" ".join(words[:i]) in upper_baseline for i in range(1, len(words)))
                ):
                    continue
                first_word = words[0]
                for tc in target_classes:
                    res = self.key_to_tests.get(f"{tc}.{attr_name}[w:{first_word}]")
                    if res is None and ck not in baseline_keys:
                        w_res = self.word_to_tests.get(first_word, [])
                        if w_res == [-1]:
                            return None
                        out.update(set(self.class_to_tests.get(tc, [])) & set(w_res))
                    elif res == [-1]:
                        return None
                    elif res:
                        out.update(res)
            return out

        if attr_name == "KEYWORDS":
            prefix = attr_qual + "["
            baseline_keys = {s[3][len(prefix) : -1] for s in syms if s[4] == 4 and s[3].startswith(prefix)}
            for ck in affected_keys:
                if not ck.startswith("s:"):
                    return None
                kw = ck[2:]
                kw_up = kw.upper()
                if kw in ("(+)", "/*+", "--+") or not kw_up:
                    return None
                if ck in baseline_keys:
                    for tc in target_classes:
                        res = self.key_to_tests.get(f"{tc}.KEYWORDS[s:{kw_up}]", [])
                        if res == [-1]:
                            return None
                        out.update(res)
                else:
                    cand_words = [kw_up]
                    if " " in kw_up:
                        cand_words.append(kw_up.split(" ")[0])
                    if any(not (ch.isalnum() or ch == "_") for ch in kw_up):
                        cand_words.append(kw_up[0])
                    for tc in target_classes:
                        tc_set = set(self.class_to_tests.get(tc, []))
                        for cw in cand_words:
                            k_res = self.key_to_tests.get(f"{tc}.KEYWORDS[s:{cw}]")
                            if k_res == [-1]:
                                return None
                            if k_res is not None:
                                out.update(k_res)
                            else:
                                w_res = self.word_to_tests.get(cw, [])
                                if w_res == [-1]:
                                    return None
                                out.update(tc_set & set(w_res))
            return out

        if attr_name in EXPR_DISPATCH_REGISTRIES:
            for ck in affected_keys:
                if not ck.startswith("e:"):
                    return None
                etests = set(self.expr_to_tests.get(ck[2:], []))
                for tc in target_classes:
                    gtests = set(self.active_gen_to_tests.get(tc, []))
                    out.update(gtests & etests)
            return out

        if attr_name in DIRECT_KEY_REGISTRIES:
            prefix = attr_qual + "["
            baseline_keys = {s[3][len(prefix) : -1] for s in syms if s[4] == 4 and s[3].startswith(prefix)}
            for ck in affected_keys:
                lk = f"s:{ck[2:].upper()}" if ck.startswith("s:") else ck
                for tc in target_classes:
                    res = self.key_to_tests.get(f"{tc}.{attr_name}[{lk}]")
                    if res == [-1]:
                        return None
                    if res is not None:
                        out.update(res)
                    elif ck not in baseline_keys and ck.startswith("s:"):
                        w_up = ck[2:].upper()
                        w_res = self.word_to_tests.get(w_up, [])
                        if w_res == [-1]:
                            return None
                        out.update(set(self.class_to_tests.get(tc, [])) & set(w_res))
            return out

        return None

    def _try_resolve_safe_attribute(
        self,
        p: str,
        h: Hunk,
        h_s: int,
        h_e: int,
        attr_entry: list[Any],
    ) -> set[int] | None:
        """Resolve safe non-init runtime class attribute modification/addition/deletion."""
        a_start, _, a_end, attr_qual, _ = attr_entry
        if "." not in attr_qual or not (a_start <= h_s and h_e <= a_end):
            return None
        cls_qual, attr_name = attr_qual.rsplit(".", 1)
        if attr_name not in self.attr_reader_tests:
            return None
        ckey = f"{p}::{cls_qual}"
        if ckey not in self.class_to_tests:
            return None

        # Verify added lines (if any) are a clean import-safe assignment to `attr_name`
        if h.added_lines:
            raw_add = textwrap.dedent("\n".join(txt for _, txt in h.added_lines))
            try:
                mod = ast.parse(raw_add)
            except SyntaxError:
                return None
            if len(mod.body) != 1:
                return None
            stmt = mod.body[0]
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                if stmt.targets[0].id != attr_name or not _is_import_safe_expr(stmt.value):
                    return None
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                if stmt.target.id != attr_name or not _is_import_safe_expr(stmt.value):
                    return None
            else:
                return None

        full_attr_id = f"{p}::{attr_qual}"
        cls_pool = set(self.class_to_tests[ckey])
        for dep_ck in self.cross_attr_deps.get(full_attr_id, []):
            cls_pool.update(self.class_to_tests.get(dep_ck, []))

        out = cls_pool & set(self.attr_reader_tests[attr_name])
        for tf in self.test_attr_refs.get(full_attr_id, []):
            out.update(self.by_file.get(tf, []))
        return out

    def _is_hunk_semantically_equivalent(self, h: Hunk, path: str) -> bool:
        """Verify whether a single-line hunk inside a normal function body is a provably behavior-preserving AST rewrite."""
        if (
            not path.endswith(".py")
            or path.startswith("tests/")
            or len(h.deleted_lines) != 1
            or len(h.added_lines) != 1
        ):
            return False
        mls = self.multiline_strings.get(path)
        if mls:
            for del_ln, _ in h.deleted_lines:
                if any(s_row <= del_ln <= e_row for s_row, e_row in mls):
                    return False
        canon = _ProvenEquivalenceCanonicalizer(path)
        for (_, del_txt), (_, add_txt) in zip(h.deleted_lines, h.added_lines):
            del_indent = len(del_txt) - len(del_txt.lstrip(" "))
            add_indent = len(add_txt) - len(add_txt.lstrip(" "))
            if del_indent != add_indent:
                return False
            p_del = _parse_single_line_ast_unit(del_txt)
            p_add = _parse_single_line_ast_unit(add_txt)
            if p_del is None or p_add is None or p_del[0] != p_add[0]:
                return False
            c_del = canon.visit(p_del[1])
            c_add = canon.visit(p_add[1])
            if ast.dump(c_del) != ast.dump(c_add):
                return False
        return True

    def _resolve_func_body_hunk(
        self,
        p: str,
        qual: str,
        h: Hunk,
        h_s: int,
        h_e: int,
        is_single_hunk_patch: bool = False,
    ) -> set[int] | None:
        """Resolve a hunk strictly inside a non-factory function/method/closure using statement-unit LINE + co_qualname coverage."""
        if is_single_hunk_patch and self._is_hunk_semantically_equivalent(h, p):
            return set()
        # Guard against dedenting out of the function body (e.g. adding a sibling method at outer indent)
        min_def_indent = 4 * qual.replace(".<locals>", "").count(".")
        for _, txt in h.added_lines:
            stripped = txt.lstrip(" ")
            if stripped and not stripped.startswith("#"):
                indent = len(txt) - len(stripped)
                if indent <= min_def_indent:
                    return None

        leaf = qual.split(".")[-1]
        qsym_key = f"{p}::{qual}"
        sym_key = f"{p}::{leaf}"

        # Function-entry baseline via co_qualname (separates closures & same-named methods across classes)
        if self.qual_symbol_to_tests:
            q_list = self.qual_symbol_to_tests.get(qsym_key)
            if q_list is None:
                func_tests: set[int] = set()
            elif q_list == [-1]:
                func_tests = set(self.symbol_to_tests.get(sym_key, []))
            else:
                func_tests = set(q_list)
        else:
            s_list = self.symbol_to_tests.get(sym_key)
            if s_list is None:
                return None
            func_tests = set(s_list)

        units = self.func_stmt_cov.get(qsym_key)
        if not units:
            return func_tests

        if h.deleted_lines:
            # Every modified line must belong to a tracked statement unit (not else:/finally:/try: structural headers)
            matched_units: list[list[Any]] = []
            for ln in range(h_s, h_e + 1):
                covering = [u for u in units if u[0] <= ln <= u[1]]
                if not covering:
                    return func_tests
                for u in covering:
                    if u not in matched_units:
                        matched_units.append(u)
            stmt_tests: set[int] = set()
            for u in matched_units:
                ut = u[2]
                if ut == [-1]:
                    return func_tests
                stmt_tests.update(ut)
            res_tests = stmt_tests & func_tests if func_tests else stmt_tests
            if not res_tests and func_tests:
                mls = self.multiline_strings.get(p, [])
                if any(s_r <= h_s and h_e <= e_r for s_r, e_r in mls):
                    return func_tests
            return res_tests
        else:
            # Pure insertion at (h_s, h_e): require both anchor lines to fall in statement units
            u_prev = [u for u in units if u[0] <= h_s <= u[1]]
            u_next = [u for u in units if u[0] <= h_e <= u[1]]
            if not u_prev or not u_next:
                return func_tests
            stmt_tests = set()
            for u in (*u_prev, *u_next):
                ut = u[2]
                if ut == [-1]:
                    return func_tests
                stmt_tests.update(ut)
            res_tests = stmt_tests & func_tests if func_tests else stmt_tests
            if not res_tests and func_tests:
                mls = self.multiline_strings.get(p, [])
                if any(s_r <= h_s and h_e <= e_r for s_r, e_r in mls):
                    return func_tests
            return res_tests

    def _add_file_closure(self, p: str, selected: set[int], triggers: dict[str, list[str]]) -> None:
        before_cnt = len(selected)

        if p in self.file_to_tests and self.file_to_tests[p]:
            selected.update(self.file_to_tests[p])
            triggers["tier4_dynamic_files"].append(p)

        if p in self.file_to_dialect:
            d = self.file_to_dialect[p]
            if d in self.dialect_to_tests:
                selected.update(self.dialect_to_tests[d])
                if d not in triggers["tier2_dialects"]:
                    triggers["tier2_dialects"].append(d)

        if p.startswith("sqlglot/typing/"):
            selected.update(self.by_file.get("tests/test_typing.py", []))

        if p.startswith("sqlglot/expressions/"):
            selected.update(self.by_file.get("tests/test_expressions.py", []))
            selected.update(self.by_file.get("tests/test_typing.py", []))
            selected.update(self.by_file.get("tests/test_serde.py", []))

        if p.startswith("sqlglot/optimizer/") and (
            p not in self.file_to_tests or not self.file_to_tests[p] or p.endswith("__init__.py")
        ):
            selected.update(self.by_file.get("tests/test_optimizer.py", []))
            selected.update(self.by_file.get("tests/test_executor.py", []))
            triggers["tier4_dynamic_files"].append("tests/test_optimizer.py")
            triggers["tier4_dynamic_files"].append("tests/test_executor.py")

        # Static import graph fallback when dynamic coverage is absent or file is __init__.py
        if p not in self.file_to_tests or not self.file_to_tests[p] or p.endswith("__init__.py"):
            mod_name = self._file_to_module(p)
            if mod_name and mod_name in self.test_dependents:
                for dep_test_file in self.test_dependents[mod_name]:
                    if dep_test_file in self.by_file:
                        selected.update(self.by_file[dep_test_file])
                        triggers["tier5_fallback"].append(dep_test_file)

        # Ultimate safety guard for unprofiled sqlglot/ or sqlglotc/ code files
        if len(selected) == before_cnt and p.startswith(("sqlglot/", "sqlglotc/")):
            if p.startswith("sqlglotc/"):
                selected.update(self.by_file.get("tests/test_sqlglotc.py", []))
            elif p != "sqlglot/__main__.py" and not p.endswith(".typed"):
                for fallback_tf in (
                    "tests/dialects/test_dialect.py",
                    "tests/test_transpile.py",
                    "tests/test_parser.py",
                    "tests/test_generator.py",
                    "tests/test_expressions.py",
                ):
                    selected.update(self.by_file.get(fallback_tf, []))
                triggers["tier5_fallback"].append("core_safety_net")

    def resolve(self, patch: str | ParsedPatch) -> list[int]:
        """Resolve a patch into a sorted list of selected test numbers."""
        return self.resolve_with_metadata(patch)["selected_tests"]

    def resolve_with_metadata(self, patch: str | ParsedPatch) -> dict[str, Any]:
        """Resolve patch and return selected test numbers with attribution metadata."""
        parsed = parse_patch(patch) if isinstance(patch, str) else patch

        if parsed.is_empty:
            return {
                "selected_tests": [],
                "reason": "empty_patch",
                "estimated_runtime_ms": 0,
            }

        if all(self._is_safe_comment_only(f, f.old_path if (f.is_rename and f.old_path) else f.path) for f in parsed.files):
            return {
                "selected_tests": [],
                "reason": "comment_only_non_sensitive",
                "estimated_runtime_ms": 0,
            }

        selected: set[int] = set()
        triggers: dict[str, list[str]] = {
            "tier0_test_files": [],
            "tier1_fixtures": [],
            "tier2_dialects": [],
            "tier3_symbols": [],
            "tier4_dynamic_files": [],
            "tier5_fallback": [],
        }

        for f in parsed.files:
            p = f.old_path if (f.is_rename and f.old_path) else f.path

            if self._is_safe_comment_only(f, p):
                continue

            # Tier 0 & Tier 1: Test files, test helpers, and fixtures
            if p.startswith("tests/") or p in self.fixture_to_tests:
                before_t = len(selected)
                if p in self.fixture_to_tests:
                    selected.update(self.fixture_to_tests[p])
                    triggers["tier1_fixtures"].append(p)

                if p.endswith(".py"):
                    scoped_methods = False
                    file_suite = self.by_file.get(p, [])
                    dyn_cov = self.file_to_tests.get(p, [])
                    if (
                        file_suite
                        and self._is_syntactically_clean_py(f)
                        and set(dyn_cov).issubset(set(file_suite))
                    ):
                        syms = self.file_symbols.get(p, [])
                        method_nums: set[int] = set()
                        all_in_test_methods = True
                        for r_s, r_e in self._effective_spans(f):
                            matched_m = False
                            for s_entry in syms:
                                s_start, s_body, s_end, qual, kflag = s_entry
                                if kflag == 0 and ".test_" in f".{qual}" and s_body <= r_s and r_e <= s_end:
                                    node_id = f"{p}::{qual.replace('.', '::')}"
                                    tnum = self.by_id.get(node_id)
                                    if tnum is not None:
                                        method_nums.add(tnum)
                                        sym_k = self._symbol_to_key(p, qual)
                                        method_nums.update(self.symbol_to_tests.get(sym_k, []))
                                        matched_m = True
                                        break
                            if not matched_m:
                                all_in_test_methods = False
                                break
                        if all_in_test_methods and method_nums:
                            selected.update(method_nums)
                            triggers["tier0_test_files"].append(p)
                            scoped_methods = True

                    if not scoped_methods:
                        if p in self.by_file:
                            selected.update(self.by_file[p])
                            triggers["tier0_test_files"].append(p)
                        if p in self.file_to_tests:
                            selected.update(self.file_to_tests[p])
                        mod_name = self._file_to_module(p)
                        if mod_name in self.test_dependents:
                            for dep_tf in self.test_dependents[mod_name]:
                                selected.update(self.by_file.get(dep_tf, []))
                else:
                    if p in self.file_to_tests:
                        selected.update(self.file_to_tests[p])
                        if p not in triggers["tier1_fixtures"]:
                            triggers["tier1_fixtures"].append(p)
                    if p.startswith("tests/fixtures/optimizer/tpc-"):
                        selected.update(self.by_file.get("tests/test_executor.py", []))
                        selected.update(self.by_file.get("tests/test_optimizer.py", []))

                if len(selected) == before_t:
                    if "optimizer" in p:
                        selected.update(self.by_file.get("tests/test_optimizer.py", []))
                        selected.update(self.by_file.get("tests/test_executor.py", []))
                    elif "jsonpath" in p:
                        selected.update(self.by_file.get("tests/test_jsonpath.py", []))
                    else:
                        for tf_tests in self.by_file.values():
                            selected.update(tf_tests)
                continue

            # Source file resolution
            if p == "sqlglot/serde.py" or not p.endswith(".py") or not self._is_syntactically_clean_py(f):
                self._add_file_closure(p, selected, triggers)
                continue

            syms = self.file_symbols.get(p, [])
            if not syms:
                self._add_file_closure(p, selected, triggers)
                continue

            # Multi-granularity per-hunk AST & registry resolution
            needs_file_closure = False
            all_tokenizer_only = True
            is_single_hunk_patch = len(parsed.files) == 1 and len(f.hunks) == 1

            for h in f.hunks:
                if self._is_hunk_safe_comment_only(h, p):
                    continue
                h_s, h_e = self._hunk_span(h)
                enclosing = [s for s in syms if s[0] <= h_s and h_e <= s[2]]
                if not enclosing:
                    needs_file_closure = True
                    all_tokenizer_only = False
                    break

                # Sort innermost first; break ties so dict_key (4) > attribute (3) > func (0) > class (2)
                priority_order = {4: 0, 3: 1, 0: 2, 1: 3, 2: 4}
                enclosing.sort(key=lambda s: (s[2] - s[0], priority_order.get(s[4], 9)))
                span_resolved = False

                # Stage 1A: Per-key registry tracking & safe per-attribute tracking
                attr_candidates = [s for s in enclosing if s[4] == 3]
                for attr_entry in attr_candidates:
                    attr_qual = attr_entry[3]
                    is_tok_attr = ".Tokenizer." in f".{attr_qual}" or ".JSONPathTokenizer." in f".{attr_qual}"

                    extracted = self._extract_changed_dict_keys(h, h_s, h_e, attr_entry, syms)
                    if extracted is not None:
                        changed_keys, keyset_changed = extracted
                        key_tests = self._resolve_registry_keys(p, attr_qual, changed_keys, keyset_changed, syms)
                        if key_tests is not None:
                            selected.update(key_tests)
                            for ck in sorted(changed_keys):
                                tag = f"{p}::{attr_qual}[{ck}]"
                                if tag not in triggers["tier3_symbols"]:
                                    triggers["tier3_symbols"].append(tag)
                            if not is_tok_attr:
                                all_tokenizer_only = False
                            span_resolved = True
                            break

                    safe_attr_tests = self._try_resolve_safe_attribute(p, h, h_s, h_e, attr_entry)
                    if safe_attr_tests is not None:
                        selected.update(safe_attr_tests)
                        tag = f"{p}::{attr_qual}"
                        if tag not in triggers["tier3_symbols"]:
                            triggers["tier3_symbols"].append(tag)
                        if not is_tok_attr:
                            all_tokenizer_only = False
                        span_resolved = True
                        break

                if span_resolved:
                    continue

                # Stage 1B: Existing function/method & class-level fallback hierarchy
                for entry in enclosing:
                    s_start, s_body, s_end, qual, kflag = entry[0], entry[1], entry[2], entry[3], entry[4]

                    # Level A: Strictly inside non-factory function/method/closure body (Stage 2 LINE + co_qualname)
                    if kflag == 0 and s_body <= h_s and h_e <= s_end:
                        leaf = qual.split(".")[-1]
                        if not qual.startswith("_Dialect") and leaf not in SPECIAL_NON_BODY_METHODS:
                            line_scoped = self._resolve_func_body_hunk(
                                p, qual, h, h_s, h_e, is_single_hunk_patch=is_single_hunk_patch
                            )
                            if line_scoped is not None:
                                selected.update(line_scoped)
                                qtag = f"{p}::{qual}"
                                if qtag not in triggers["tier3_symbols"]:
                                    triggers["tier3_symbols"].append(qtag)
                                if line_scoped and p.startswith("sqlglot/typing/"):
                                    selected.update(self.by_file.get("tests/test_typing.py", []))
                                if p in self.file_to_dialect:
                                    d = self.file_to_dialect[p]
                                    if not (".Tokenizer." in f".{qual}." or ".JSONPathTokenizer." in f".{qual}."):
                                        if d not in triggers["tier2_dialects"]:
                                            triggers["tier2_dialects"].append(d)
                                if not (".Tokenizer." in f".{qual}." or ".JSONPathTokenizer." in f".{qual}."):
                                    all_tokenizer_only = False
                                span_resolved = True
                                break

                    # Level B: Strictly inside class body (excluding class header/decorators)
                    elif kflag == 2 and s_start < h_s and h_e <= s_end and qual != "_Dialect":
                        ckey = f"{p}::{qual}"
                        is_tok_cls = qual.endswith(("Tokenizer", "JSONPathTokenizer")) or ".Tokenizer" in qual or ".JSONPathTokenizer" in qual
                        is_gen_cls = (
                            qual.endswith("Generator")
                            or ".Generator" in f".{qual}"
                            or p.startswith("sqlglot/generators/")
                        )
                        if not is_tok_cls:
                            all_tokenizer_only = False

                        hunk_raw_text = "\n".join(t for _, t in (h.deleted_lines + h.added_lines))
                        touches_nested_class_hdr = "class " in hunk_raw_text
                        touches_gen_init = (
                            touches_nested_class_hdr
                            or p in ("sqlglot/generator.py", "sqlglot/dialects/dialect.py")
                            or any(
                                kw in hunk_raw_text
                                for kw in ("SELECT_KINDS", "STRING_ESCAPES", "QUOTES", "IDENTIFIERS", "BYTE_STRINGS")
                            )
                        )

                        if is_gen_cls and not touches_gen_init and ckey in self.active_gen_to_tests:
                            cls_set = set(self.active_gen_to_tests[ckey])
                        else:
                            cls_raw = self.class_to_tests.get(ckey)
                            cls_set = set(cls_raw) if cls_raw else set()
                            if not touches_gen_init and self.gen_memory_test_num is not None:
                                cls_set.discard(self.gen_memory_test_num)

                        # Propagate cross-class attribute spreads (e.g. **Cast.arg_types, Presto.TIME_MAPPING)
                        for a_cand in attr_candidates:
                            if a_cand[3].startswith(qual + "."):
                                full_aid = f"{p}::{a_cand[3]}"
                                for dep_ck in self.cross_attr_deps.get(full_aid, []):
                                    cls_set.update(self.class_to_tests.get(dep_ck, []))
                                    if not touches_gen_init and self.gen_memory_test_num is not None:
                                        cls_set.discard(self.gen_memory_test_num)
                                for tf in self.test_attr_refs.get(full_aid, []):
                                    cls_set.update(self.by_file.get(tf, []))

                        if cls_set:
                            selected.update(cls_set)
                            if ckey not in triggers["tier3_symbols"]:
                                triggers["tier3_symbols"].append(ckey)
                            if p.startswith("sqlglot/expressions/") and qual in ("Expr", "Expression", "Func"):
                                selected.update(self.by_file.get("tests/test_expressions.py", []))
                                selected.update(self.by_file.get("tests/test_typing.py", []))
                                selected.update(self.by_file.get("tests/test_serde.py", []))
                            if p in self.file_to_dialect:
                                d = self.file_to_dialect[p]
                                if touches_nested_class_hdr:
                                    selected.update(self.dialect_to_tests.get(d, []))
                                if not is_tok_cls and d not in triggers["tier2_dialects"]:
                                    triggers["tier2_dialects"].append(d)
                            span_resolved = True
                            break

                if not span_resolved:
                    needs_file_closure = True
                    all_tokenizer_only = False
                    break

            if all_tokenizer_only and not needs_file_closure and p in self.file_to_dialect:
                d = self.file_to_dialect[p]
                tok_tag = f"{d}::tokenizer_only"
                if tok_tag not in triggers["tier2_dialects"]:
                    triggers["tier2_dialects"].append(tok_tag)

            if needs_file_closure:
                self._add_file_closure(p, selected, triggers)

        sorted_tests = sorted(selected)
        total_duration = sum(
            self.test_durations[t] for t in sorted_tests if t < len(self.test_durations)
        )

        return {
            "selected_tests": sorted_tests,
            "count": len(sorted_tests),
            "estimated_runtime_ms": total_duration,
            "triggers": triggers,
        }
