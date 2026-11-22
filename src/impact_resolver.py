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

from src.diff_parser import (
    PEP263_CODING_RE,
    FileDiff,
    Hunk,
    ParsedPatch,
    _is_python_hash_comment_or_empty,
    parse_patch,
)

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

    def __init__(
        self,
        path: str,
        allowed_none_self_attrs: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        super().__init__()
        self.path = path
        self.allowed_none_self_attrs = allowed_none_self_attrs

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        if len(node.ops) != 1 or len(node.comparators) != 1:
            return node
        op = node.ops[0]
        if not isinstance(op, (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)):
            return node
        lhs = node.left
        rhs = node.comparators[0]

        # Rule 2A: `self.<attr> is None` <-> `self.<attr> == None` and `self.<attr> is not None` <-> `self.<attr> != None`
        # Restricted strictly to statically proven-safe scalar-typed `self.<attr>` attributes on the enclosing class.
        if (
            self.path != "sqlglot/executor/env.py"
            and isinstance(rhs, ast.Constant)
            and rhs.value is None
            and isinstance(lhs, ast.Attribute)
            and isinstance(lhs.value, ast.Name)
            and lhs.value.id == "self"
            and lhs.attr in self.allowed_none_self_attrs
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

    @staticmethod
    def _canonicalize_top_level_copy_call(expr: ast.expr | None) -> ast.expr | None:
        """Rule 2C: `copy=False` <-> `copy=True` ONLY when the `COPY_WRAPPER_FUNCS` call is the direct top-level RHS expression."""
        if not isinstance(expr, ast.Call):
            return expr
        if (
            expr.args
            and ast.unparse(expr.func) in COPY_WRAPPER_FUNCS
            and _is_fresh_ast_constructor_call(expr.args[0])
            and all(
                not isinstance(a, ast.Starred) and (_is_pure_ref_expr(a) or isinstance(a, ast.Constant))
                for a in expr.args[1:]
            )
            and all(
                kw.arg is not None and (_is_pure_ref_expr(kw.value) or isinstance(kw.value, ast.Constant))
                for kw in expr.keywords
            )
        ):
            new_kws: list[ast.keyword] = []
            modified = False
            for kw in expr.keywords:
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
                    ast.Call(func=expr.func, args=expr.args, keywords=new_kws),
                    expr,
                )
        return expr

    def visit_Call(self, node: ast.Call) -> ast.AST:
        # Never canonicalize comparisons or nested calls inside function/method call arguments.
        return node

    def visit_Assign(self, node: ast.Assign) -> ast.AST:
        self.generic_visit(node)
        new_val = self._canonicalize_top_level_copy_call(node.value)
        if new_val is not node.value and new_val is not None:
            return ast.copy_location(
                ast.Assign(targets=node.targets, value=new_val, type_comment=node.type_comment),
                node,
            )
        return node

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST:
        self.generic_visit(node)
        new_val = self._canonicalize_top_level_copy_call(node.value)
        if new_val is not node.value:
            return ast.copy_location(
                ast.AnnAssign(
                    target=node.target,
                    annotation=node.annotation,
                    value=new_val,
                    simple=node.simple,
                ),
                node,
            )
        return node

    def visit_Return(self, node: ast.Return) -> ast.AST:
        self.generic_visit(node)
        new_val = self._canonicalize_top_level_copy_call(node.value)
        if new_val is not node.value:
            return ast.copy_location(ast.Return(value=new_val), node)
        return node


def _line_delimiter_delta(line: str) -> list[int] | None:
    """Return [paren_delta, bracket_delta, brace_delta] for `line`, or None if unclosed quote, triple quote, or trailing backslash."""
    if '"""' in line or "'''" in line:
        return None
    p_d = b_d = c_d = 0
    in_quote: str | None = None
    escaped = False
    last_non_ws_before_comment = ""
    for ch in line:
        if escaped:
            escaped = False
            last_non_ws_before_comment = ch
            continue
        if ch == "\\":
            escaped = True
            last_non_ws_before_comment = "\\"
            continue
        if in_quote is not None:
            if ch == in_quote:
                in_quote = None
            continue
        if ch in ("'", '"'):
            in_quote = ch
            last_non_ws_before_comment = ch
            continue
        if ch == "#":
            break
        if not ch.isspace():
            last_non_ws_before_comment = ch
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
    if in_quote is not None or escaped or last_non_ws_before_comment == "\\":
        return None
    return [p_d, b_d, c_d]


def _parse_verified_stmt_block(block_text: str) -> tuple[str, ast.AST] | None:
    """Parse a complete statement (`Assign`, `AnnAssign`, `Return`) or suite header (`if`/`elif`/`while`).

    Never accepts bare `ast.Expr` expressions, single-line suite bodies, or isolated ternary fragments.
    """
    dedented = textwrap.dedent(block_text).strip()
    if not dedented or dedented.startswith("#") or ";" in dedented:
        return None
    if dedented.endswith(":"):
        words = dedented.split()
        first_word = words[0] if words else ""
        if first_word in ("if", "elif", "while"):
            hdr = "if " + dedented[5:].lstrip() if first_word == "elif" else dedented
            try:
                mod = ast.parse(hdr + "\n    pass")
            except SyntaxError:
                return None
            if (
                len(mod.body) == 1
                and isinstance(mod.body[0], (ast.If, ast.While))
                and len(mod.body[0].body) == 1
                and isinstance(mod.body[0].body[0], ast.Pass)
                and not mod.body[0].orelse
            ):
                return (first_word, mod)
        return None
    try:
        mod = ast.parse(dedented)
    except SyntaxError:
        return None
    if len(mod.body) != 1:
        return None
    stmt = mod.body[0]
    if not isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.Return)):
        return None
    if isinstance(stmt, ast.Assign) and " = " not in dedented:
        return None
    return ("stmt", mod)


def _extract_hunk_ast_pair(h: Hunk) -> tuple[tuple[str, ast.AST], tuple[str, ast.AST]] | None:
    """Extract a context-verified (deleted_ast, added_ast) statement pair from a single-line hunk."""
    if len(h.deleted_lines) != 1 or len(h.added_lines) != 1:
        return None
    del_ln, del_txt = h.deleted_lines[0]
    _, add_txt = h.added_lines[0]
    del_indent = len(del_txt) - len(del_txt.lstrip(" "))
    add_indent = len(add_txt) - len(add_txt.lstrip(" "))
    if del_indent != add_indent:
        return None
    d_del = _line_delimiter_delta(del_txt)
    d_add = _line_delimiter_delta(add_txt)
    if d_del is None or d_add is None or d_del != [0, 0, 0] or d_add != [0, 0, 0]:
        return None

    ctx_map: dict[int, str] = dict(h.context_with_lineno)
    before_lns = sorted(ln for ln in ctx_map if ln < del_ln)
    after_lns = sorted(ln for ln in ctx_map if ln > del_ln)

    ctx_deltas: dict[int, list[int]] = {}
    for ln, txt in ctx_map.items():
        d = _line_delimiter_delta(txt)
        if d is None:
            return None
        ctx_deltas[ln] = d

    pre_bal = [0, 0, 0]
    for ln in before_lns:
        d = ctx_deltas[ln]
        pre_bal[0] += d[0]
        pre_bal[1] += d[1]
        pre_bal[2] += d[2]

    post_bal = [0, 0, 0]
    post_has_negative = False
    for ln in after_lns:
        d = ctx_deltas[ln]
        post_bal[0] += d[0]
        post_bal[1] += d[1]
        post_bal[2] += d[2]
        if post_bal[0] < 0 or post_bal[1] < 0 or post_bal[2] < 0:
            post_has_negative = True

    # Path 1: Single-line complete statement or suite header at outer delimiter depth [0, 0, 0]
    if pre_bal == [0, 0, 0] and not post_has_negative:
        p_del = _parse_verified_stmt_block(del_txt)
        p_add = _parse_verified_stmt_block(add_txt)
        if p_del is not None and p_add is not None and p_del[0] == p_add[0]:
            return (p_del, p_add)

    # Path 2: Enclosing multiline parenthesized statement fully contained within the hunk's context lines
    if not before_lns or not after_lns:
        return None
    all_lns = [*before_lns, del_ln, *after_lns]
    if all_lns != list(range(all_lns[0], all_lns[-1] + 1)):
        return None
    del_idx = all_lns.index(del_ln)

    cum_from_zero = [0, 0, 0]
    for start_idx in range(0, del_idx + 1):
        if start_idx > 0:
            prev_d = ctx_deltas[all_lns[start_idx - 1]]
            cum_from_zero[0] += prev_d[0]
            cum_from_zero[1] += prev_d[1]
            cum_from_zero[2] += prev_d[2]
        if cum_from_zero != [0, 0, 0]:
            continue
        start_txt = del_txt if start_idx == del_idx else ctx_map[all_lns[start_idx]]
        if not start_txt.strip() or start_txt.lstrip().startswith("#"):
            continue
        win_bal = [0, 0, 0]
        valid_open = True
        closed_end_idx: int | None = None
        for k in range(start_idx, len(all_lns)):
            d = d_del if k == del_idx else ctx_deltas[all_lns[k]]
            win_bal[0] += d[0]
            win_bal[1] += d[1]
            win_bal[2] += d[2]
            if win_bal[0] < 0 or win_bal[1] < 0 or win_bal[2] < 0:
                valid_open = False
                break
            if k < del_idx and win_bal == [0, 0, 0]:
                valid_open = False
                break
            if k >= del_idx and win_bal == [0, 0, 0]:
                closed_end_idx = k
                break
        if not valid_open or closed_end_idx is None or closed_end_idx == start_idx:
            continue
        rem_bal = [0, 0, 0]
        rem_neg = False
        for k in range(closed_end_idx + 1, len(all_lns)):
            d = ctx_deltas[all_lns[k]]
            rem_bal[0] += d[0]
            rem_bal[1] += d[1]
            rem_bal[2] += d[2]
            if rem_bal[0] < 0 or rem_bal[1] < 0 or rem_bal[2] < 0:
                rem_neg = True
                break
        if rem_neg:
            continue

        del_block = "\n".join(
            del_txt if k == del_idx else ctx_map[all_lns[k]]
            for k in range(start_idx, closed_end_idx + 1)
        )
        add_block = "\n".join(
            add_txt if k == del_idx else ctx_map[all_lns[k]]
            for k in range(start_idx, closed_end_idx + 1)
        )
        p_del = _parse_verified_stmt_block(del_block)
        p_add = _parse_verified_stmt_block(add_block)
        if p_del is not None and p_add is not None and p_del[0] == p_add[0]:
            return (p_del, p_add)
        return None

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
        self.unsafe_attrs: set[str] = set(bundle.get("unsafe_attrs", []))
        self.proven_safe_none_attrs: dict[str, list[str]] = bundle.get("proven_safe_none_attrs", {})
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

    def _is_safe_blank_or_comment_line(self, p: str, ln: int, txt: str) -> bool:
        """Verify line `ln` is a Python blank or `#` comment line outside multiline strings and PEP 263 headers."""
        if not _is_python_hash_comment_or_empty(txt):
            return False
        if '"""' in txt or "'''" in txt:
            return False
        if ln <= 2 and bool(PEP263_CODING_RE.match(txt)):
            return False
        mls = self.multiline_strings.get(p)
        if mls and any(s_r <= ln <= e_r for s_r, e_r in mls):
            return False
        return True

    def _hunk_span(self, h: Hunk, p: str = "") -> tuple[int, int]:
        del_ln_set = {ln for ln, _ in h.deleted_lines}
        pure_ins_lines = [ln for ln in h.modified_lines if ln not in del_ln_set]
        span_lns: list[int] = []
        for ins_ln in pure_ins_lines:
            if not h.deleted_lines:
                span_lns.append(max(1, ins_ln - 1))
            span_lns.append(ins_ln)

        if h.deleted_lines:
            del_list = list(h.deleted_lines)
            if p.endswith(".py"):
                # Guard 2A-1: Trim contiguous trailing blank/comment deleted lines outside multiline strings
                while (
                    len(del_list) > 1
                    and del_list[-1][0] == del_list[-2][0] + 1
                    and self._is_safe_blank_or_comment_line(p, del_list[-1][0], del_list[-1][1])
                ):
                    del_list.pop()

                # Guard 2A-1: Trim contiguous leading blank/comment lines on pure deletions,
                # or contiguous leading `#` comment lines at matching indentation when no higher-indented added line precedes them.
                first_code_indent: int | None = None
                for ln_c, txt_c in del_list:
                    if not self._is_safe_blank_or_comment_line(p, ln_c, txt_c):
                        first_code_indent = len(txt_c) - len(txt_c.lstrip(" "))
                        break
                first_add_indent: int | None = None
                for _, txt_a in h.added_lines:
                    s_a = txt_a.lstrip(" ")
                    if s_a and not s_a.startswith("#"):
                        first_add_indent = len(txt_a) - len(s_a)
                        break

                while (
                    len(del_list) > 1
                    and del_list[0][0] + 1 == del_list[1][0]
                    and self._is_safe_blank_or_comment_line(p, del_list[0][0], del_list[0][1])
                ):
                    if not h.added_lines:
                        del_list.pop(0)
                    elif (
                        del_list[0][1].lstrip(" ").startswith("#")
                        and first_code_indent is not None
                        and (len(del_list[0][1]) - len(del_list[0][1].lstrip(" "))) == first_code_indent
                        and (first_add_indent is None or first_add_indent <= first_code_indent)
                    ):
                        del_list.pop(0)
                    else:
                        break
            span_lns.extend(ln for ln, _ in del_list)
            return min(span_lns), max(span_lns)

        if span_lns:
            return min(span_lns), max(span_lns)
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

        # Case 1b: Pure deletion of a subclass dict override `ATTR = {**Base.ATTR, k1: v1, ...}`
        if a_start == h_s and h_e == a_end and h.deleted_lines and not h.added_lines:
            old_parsed = _parse_full_dict_assign(h.deleted_lines, attr_name, check_safe_values=False)
            if old_parsed is not None:
                old_kv, old_sp = old_parsed
                if old_sp and old_kv:
                    return set(old_kv.keys()), True

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
        is_single_hunk_patch: bool = False,
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
            if not is_single_hunk_patch or keyset_changed or any(tc != ckey for tc in target_classes):
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

    def _is_hunk_semantically_equivalent(self, h: Hunk, path: str, qual: str = "") -> bool:
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
        allowed_none_attrs: set[str] = set()
        if "." in qual and ".<locals>." not in qual:
            cls_name = qual.split(".")[0]
            allowed_none_attrs = set(self.proven_safe_none_attrs.get(f"{path}::{cls_name}", ()))
        parsed_pair = _extract_hunk_ast_pair(h)
        if parsed_pair is None:
            return False
        p_del, p_add = parsed_pair
        canon = _ProvenEquivalenceCanonicalizer(path, allowed_none_attrs)
        c_del = canon.visit(p_del[1])
        c_add = canon.visit(p_add[1])
        return ast.dump(c_del) == ast.dump(c_add)

    def _get_func_entry_tests(self, p: str, qual: str) -> set[int] | None:
        """Return function-entry baseline via co_qualname, intersecting [-1] overflows with enclosing class_to_tests."""
        leaf = qual.split(".")[-1]
        qsym_key = f"{p}::{qual}"
        sym_key = f"{p}::{leaf}"
        if self.qual_symbol_to_tests:
            q_list = self.qual_symbol_to_tests.get(qsym_key)
            if q_list is None:
                func_tests: set[int] = set()
            elif q_list == [-1]:
                func_tests = set(self.symbol_to_tests.get(sym_key, []))
                # Guard A5: Eliminate cross-class leaf-name collision when q_list == [-1]
                if "." in qual and ".<locals>." not in qual:
                    cls_qual = qual.rsplit(".", 1)[0]
                    ckey = f"{p}::{cls_qual}"
                    if ckey in self.class_to_tests:
                        func_tests &= set(self.class_to_tests[ckey])
            else:
                func_tests = set(q_list)
        else:
            s_list = self.symbol_to_tests.get(sym_key)
            if s_list is None:
                return None
            func_tests = set(s_list)
        return func_tests

    def _get_func_entry_tests_with_readers(self, p: str, qual: str) -> set[int] | None:
        func_tests = self._get_func_entry_tests(p, qual)
        if func_tests is None:
            return None
        leaf = qual.split(".")[-1]
        if leaf in self.attr_reader_tests:
            readers = set(self.attr_reader_tests[leaf])
            if "." in qual and ".<locals>." not in qual:
                cls_qual = qual.rsplit(".", 1)[0]
                ckey = f"{p}::{cls_qual}"
                if ckey in self.class_to_tests:
                    readers &= set(self.class_to_tests[ckey])
            func_tests = func_tests | readers
        return func_tests

    def _try_resolve_func_header_or_deletion(
        self,
        p: str,
        qual: str,
        h: Hunk,
        h_s: int,
        h_e: int,
        s_start: int,
        s_body: int,
        s_end: int,
    ) -> set[int] | None:
        """Resolve a guarded function signature edit or single whole-function/method pure deletion (`s_start <= h_s < s_body`)."""
        if p == "sqlglot/executor/env.py":
            return None
        leaf = qual.split(".")[-1]
        if (
            qual.startswith("_Dialect")
            or leaf in SPECIAL_NON_BODY_METHODS
            or (leaf.startswith("__") and leaf.endswith("__"))
            or leaf in self.unsafe_attrs
        ):
            return None

        # Reject if any decorator line `@...` exists in deleted, added, or pre-body context lines
        for _, txt in h.deleted_lines + h.added_lines:
            if txt.lstrip(" ").startswith("@"):
                return None
        for c_ln, c_txt in h.context_with_lineno:
            if s_start <= c_ln < s_body and c_txt.lstrip(" ").startswith("@"):
                return None

        # Case 1: Pure deletion of the entire function/method (`not h.added_lines`)
        if not h.added_lines:
            if h_s == s_start and h_e == s_end:
                return self._get_func_entry_tests_with_readers(p, qual)
            return None

        # Case 2: Guarded signature / header edit (`h.added_lines` non-empty)
        min_def_indent = 4 * qual.replace(".<locals>", "").count(".")
        # Verify the function has no decorator above `def` (`s_start` must be the `def` line)
        ctx_map = dict(h.context_with_lineno)
        del_map = dict(h.deleted_lines)
        start_line_txt = del_map.get(s_start) or ctx_map.get(s_start) or ""
        if not start_line_txt.lstrip(" ").startswith((f"def {leaf}(", f"async def {leaf}(")):
            return None

        # Reconstruct new lines in the header region and check added body lines after the header
        header_added_by_anchor: dict[int, list[str]] = {}
        anchors = h.added_old_anchors if len(h.added_old_anchors) == len(h.added_lines) else [s_body] * len(h.added_lines)
        for idx_a, ((_, txt), old_anc) in enumerate(zip(h.added_lines, anchors)):
            stripped = txt.lstrip(" ")
            if not stripped or stripped.startswith("#"):
                if old_anc <= s_body:
                    header_added_by_anchor.setdefault(old_anc, []).append(txt)
                continue
            indent = len(txt) - len(stripped)
            if old_anc <= s_body:
                if indent == min_def_indent:
                    if idx_a != 0 or s_start not in del_map or not stripped.startswith((f"def {leaf}(", f"async def {leaf}(")):
                        return None
                elif indent < min_def_indent:
                    return None
                header_added_by_anchor.setdefault(old_anc, []).append(txt)
            else:
                if indent <= min_def_indent:
                    return None

        # Reconstruct complete new `def` header `[s_start .. s_body - 1]` from context + header added lines
        full_hdr_lines: list[str] = []
        emitted_anchors: set[int] = set()
        for ln in range(s_start, s_body + 1):
            if ln in header_added_by_anchor and ln not in emitted_anchors:
                full_hdr_lines.extend(header_added_by_anchor[ln])
                emitted_anchors.add(ln)
            if ln < s_body and ln not in del_map and ln in ctx_map:
                full_hdr_lines.append(ctx_map[ln])

        hdr_src = textwrap.dedent("\n".join(full_hdr_lines)).strip()
        if not hdr_src.endswith(":"):
            return None
        try:
            mod = ast.parse(hdr_src + "\n    pass")
        except SyntaxError:
            return None
        if len(mod.body) != 1 or not isinstance(mod.body[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
            return None
        fn_ast = mod.body[0]
        if fn_ast.name != leaf or fn_ast.decorator_list:
            return None
        for d_expr in (*fn_ast.args.defaults, *fn_ast.args.kw_defaults):
            if d_expr is not None and not _is_import_safe_expr(d_expr):
                return None

        return self._get_func_entry_tests(p, qual)

    def _try_resolve_multi_method_deletion(
        self,
        p: str,
        h: Hunk,
        h_s: int,
        h_e: int,
        syms: list[list[Any]],
    ) -> tuple[set[int], list[str], bool] | None:
        """Resolve a pure deletion hunk (`not h.added_lines`) that cleanly deletes 1 or more complete non-factory methods."""
        if h.added_lines or not h.deleted_lines or p == "sqlglot/executor/env.py":
            return None
        for _, txt in h.deleted_lines:
            if txt.lstrip(" ").startswith("@"):
                return None

        cand_methods = [
            s for s in syms if s[4] == 0 and h_s <= s[0] and s[2] <= h_e
        ]
        if not cand_methods:
            return None

        # Every non-blank/non-comment deleted line must fall inside one of the completely deleted methods
        used_methods: list[list[Any]] = []
        for del_ln, del_txt in h.deleted_lines:
            if self._is_safe_blank_or_comment_line(p, del_ln, del_txt):
                continue
            matched = [m for m in cand_methods if m[0] <= del_ln <= m[2]]
            if not matched:
                return None
            for m in matched:
                if m not in used_methods:
                    used_methods.append(m)

        if not used_methods:
            return None

        out_tests: set[int] = set()
        tags: list[str] = []
        all_tok = True
        for m in used_methods:
            m_qual = m[3]
            m_leaf = m_qual.split(".")[-1]
            if (
                m_qual.startswith("_Dialect")
                or m_leaf in SPECIAL_NON_BODY_METHODS
                or (m_leaf.startswith("__") and m_leaf.endswith("__"))
                or m_leaf in self.unsafe_attrs
            ):
                return None
            m_tests = self._get_func_entry_tests_with_readers(p, m_qual)
            if m_tests is None:
                return None
            out_tests.update(m_tests)
            tags.append(f"{p}::{m_qual}")
            if not (".Tokenizer." in f".{m_qual}." or ".JSONPathTokenizer." in f".{m_qual}."):
                all_tok = False

        return out_tests, tags, all_tok

    def _resolve_func_body_hunk(
        self,
        p: str,
        qual: str,
        h: Hunk,
        h_s: int,
        h_e: int,
        s_body: int = 0,
        is_single_hunk_patch: bool = False,
    ) -> set[int] | None:
        """Resolve a hunk strictly inside a non-factory function/method/closure using statement-unit LINE + co_qualname coverage."""
        if is_single_hunk_patch and self._is_hunk_semantically_equivalent(h, p, qual):
            return set()
        # Guard against dedenting out of the function body (e.g. adding a sibling method at outer indent)
        min_def_indent = 4 * qual.replace(".<locals>", "").count(".")
        for _, txt in h.added_lines:
            stripped = txt.lstrip(" ")
            if stripped and not stripped.startswith("#"):
                indent = len(txt) - len(stripped)
                if indent <= min_def_indent:
                    return None

        qsym_key = f"{p}::{qual}"
        func_tests = self._get_func_entry_tests(p, qual)
        if func_tests is None:
            return None

        units = self.func_stmt_cov.get(qsym_key)
        if not units:
            return func_tests

        del_ln_set = {ln for ln, _ in h.deleted_lines}
        pure_ins_lines = sorted(set(h.modified_lines) - del_ln_set)
        code_del_pairs = [
            (ln, txt)
            for ln, txt in h.deleted_lines
            if not self._is_safe_blank_or_comment_line(p, ln, txt)
        ]

        matched_units: list[list[Any]] = []
        if h.deleted_lines and not code_del_pairs:
            # Hunk only deleted blank/comment lines inside function body (and added code): anchor around deleted lines
            for del_ln, _ in h.deleted_lines:
                pure_ins_lines.append(del_ln)

        ctx_map = dict(h.context_with_lineno)
        del_map = dict(h.deleted_lines)

        for idx, (del_ln, del_txt) in enumerate(code_del_pairs):
            covering = [u for u in units if u[0] <= del_ln <= u[1]]
            if not covering:
                # Guard 2B: Allow deleted `else:` only when its matching `if ` header at the exact same indentation
                # is also in `code_del_pairs`, no intervening line has smaller indentation, and both branches have deleted code lines.
                if del_txt.strip() == "else:" and 0 < idx < len(code_del_pairs) - 1:
                    else_indent = len(del_txt) - len(del_txt.lstrip(" "))
                    matching_if_ln: int | None = None
                    for prev_ln, prev_txt in code_del_pairs[:idx]:
                        prev_stripped = prev_txt.lstrip(" ")
                        prev_indent = len(prev_txt) - len(prev_stripped)
                        if prev_indent == else_indent and prev_stripped.startswith("if "):
                            matching_if_ln = prev_ln
                    if matching_if_ln is not None:
                        intervening_dedent = False
                        for check_ln in range(matching_if_ln + 1, del_ln):
                            raw_t = del_map.get(check_ln) or ctx_map.get(check_ln) or ""
                            if not self._is_safe_blank_or_comment_line(p, check_ln, raw_t):
                                c_ind = len(raw_t) - len(raw_t.lstrip(" "))
                                if c_ind < else_indent:
                                    intervening_dedent = True
                                    break
                        if not intervening_dedent:
                            continue
                return func_tests
            innermost = min(covering, key=lambda u: (u[1] - u[0], u[0]))
            if innermost not in matched_units:
                matched_units.append(innermost)

        for ins_ln in pure_ins_lines:
            u_prev = [u for u in units if u[0] <= max(1, ins_ln - 1) <= u[1]]
            u_next = [u for u in units if u[0] <= ins_ln <= u[1]]
            if not u_prev or not u_next:
                return func_tests
            in_prev = min(u_prev, key=lambda u: (u[1] - u[0], u[0]))
            in_next = min(u_next, key=lambda u: (u[1] - u[0], u[0]))
            for u in (in_prev, in_next):
                if u not in matched_units:
                    matched_units.append(u)

        if not matched_units:
            return func_tests

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
                # Guard 2C: Suppress fallback for verified interior edits to a 0-bytecode function docstring unit
                doc_u = units[0]
                if (
                    s_body > 0
                    and doc_u[0] == s_body
                    and doc_u[2] == []
                    and doc_u[0] < h_s
                    and h_e < doc_u[1]
                    and any(s_r <= doc_u[0] and doc_u[1] <= e_r for s_r, e_r in mls)
                    and not any('"""' in t or "'''" in t for _, t in (h.deleted_lines + h.added_lines))
                ):
                    return set()
                return func_tests
        return res_tests

    def _add_file_closure(self, p: str, selected: set[int], triggers: dict[str, list[str]]) -> None:
        file_selected: set[int] = set()

        if p in self.file_to_tests and self.file_to_tests[p]:
            file_selected.update(self.file_to_tests[p])
            triggers["tier4_dynamic_files"].append(p)

        if p in self.file_to_dialect:
            d = self.file_to_dialect[p]
            if d in self.dialect_to_tests:
                file_selected.update(self.dialect_to_tests[d])
                if d not in triggers["tier2_dialects"]:
                    triggers["tier2_dialects"].append(d)

        if p.startswith("sqlglot/typing/"):
            file_selected.update(self.by_file.get("tests/test_typing.py", []))

        if p.startswith("sqlglot/expressions/"):
            file_selected.update(self.by_file.get("tests/test_expressions.py", []))
            file_selected.update(self.by_file.get("tests/test_typing.py", []))
            file_selected.update(self.by_file.get("tests/test_serde.py", []))

        if p.startswith("sqlglot/optimizer/") and (
            p not in self.file_to_tests or not self.file_to_tests[p] or p.endswith("__init__.py")
        ):
            file_selected.update(self.by_file.get("tests/test_optimizer.py", []))
            file_selected.update(self.by_file.get("tests/test_executor.py", []))
            triggers["tier4_dynamic_files"].append("tests/test_optimizer.py")
            triggers["tier4_dynamic_files"].append("tests/test_executor.py")

        # Static import graph fallback when dynamic coverage is absent or file is __init__.py
        if p not in self.file_to_tests or not self.file_to_tests[p] or p.endswith("__init__.py"):
            mod_name = self._file_to_module(p)
            if mod_name and mod_name in self.test_dependents:
                for dep_test_file in self.test_dependents[mod_name]:
                    if dep_test_file in self.by_file:
                        file_selected.update(self.by_file[dep_test_file])
                        triggers["tier5_fallback"].append(dep_test_file)

        # Ultimate safety guard for unprofiled sqlglot/ or sqlglotc/ code files
        if not file_selected and p.startswith(("sqlglot/", "sqlglotc/")):
            if p.startswith("sqlglotc/"):
                file_selected.update(self.by_file.get("tests/test_sqlglotc.py", []))
            elif p != "sqlglot/__main__.py" and not p.endswith(".typed"):
                for fallback_tf in (
                    "tests/dialects/test_dialect.py",
                    "tests/test_transpile.py",
                    "tests/test_parser.py",
                    "tests/test_generator.py",
                    "tests/test_expressions.py",
                ):
                    file_selected.update(self.by_file.get(fallback_tf, []))
                triggers["tier5_fallback"].append("core_safety_net")

        selected.update(file_selected)

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
                tier0_selected: set[int] = set()
                if p in self.fixture_to_tests:
                    tier0_selected.update(self.fixture_to_tests[p])
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
                            tier0_selected.update(method_nums)
                            triggers["tier0_test_files"].append(p)
                            scoped_methods = True

                    if not scoped_methods:
                        if p in self.by_file:
                            tier0_selected.update(self.by_file[p])
                            triggers["tier0_test_files"].append(p)
                        if p in self.file_to_tests:
                            tier0_selected.update(self.file_to_tests[p])
                        mod_name = self._file_to_module(p)
                        if mod_name in self.test_dependents:
                            for dep_tf in self.test_dependents[mod_name]:
                                tier0_selected.update(self.by_file.get(dep_tf, []))
                else:
                    if p in self.file_to_tests:
                        tier0_selected.update(self.file_to_tests[p])
                        if p not in triggers["tier1_fixtures"]:
                            triggers["tier1_fixtures"].append(p)
                    if p.startswith("tests/fixtures/optimizer/tpc-"):
                        tier0_selected.update(self.by_file.get("tests/test_executor.py", []))
                        tier0_selected.update(self.by_file.get("tests/test_optimizer.py", []))

                if not tier0_selected:
                    if "optimizer" in p:
                        tier0_selected.update(self.by_file.get("tests/test_optimizer.py", []))
                        tier0_selected.update(self.by_file.get("tests/test_executor.py", []))
                    elif "jsonpath" in p:
                        tier0_selected.update(self.by_file.get("tests/test_jsonpath.py", []))
                    else:
                        for tf_tests in self.by_file.values():
                            tier0_selected.update(tf_tests)
                selected.update(tier0_selected)
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
                h_s, h_e = self._hunk_span(h, p)
                enclosing = [s for s in syms if s[0] <= h_s and h_e <= s[2]]
                if not enclosing:
                    multi_del = self._try_resolve_multi_method_deletion(p, h, h_s, h_e, syms)
                    if multi_del is not None:
                        m_tests, m_tags, m_all_tok = multi_del
                        selected.update(m_tests)
                        for qtag in m_tags:
                            if qtag not in triggers["tier3_symbols"]:
                                triggers["tier3_symbols"].append(qtag)
                        if m_tests and p.startswith("sqlglot/typing/"):
                            selected.update(self.by_file.get("tests/test_typing.py", []))
                        if p in self.file_to_dialect:
                            d = self.file_to_dialect[p]
                            if not m_all_tok and d not in triggers["tier2_dialects"]:
                                triggers["tier2_dialects"].append(d)
                        if not m_all_tok:
                            all_tokenizer_only = False
                        continue
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
                        key_tests = self._resolve_registry_keys(
                            p,
                            attr_qual,
                            changed_keys,
                            keyset_changed,
                            syms,
                            is_single_hunk_patch=is_single_hunk_patch,
                        )
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

                    # Level A: Strictly inside non-factory function/method/closure body or guarded signature/deletion
                    if kflag == 0 and s_start <= h_s and h_e <= s_end:
                        leaf = qual.split(".")[-1]
                        if not qual.startswith("_Dialect") and leaf not in SPECIAL_NON_BODY_METHODS:
                            if s_body <= h_s:
                                line_scoped = self._resolve_func_body_hunk(
                                    p,
                                    qual,
                                    h,
                                    h_s,
                                    h_e,
                                    s_body=s_body,
                                    is_single_hunk_patch=is_single_hunk_patch,
                                )
                            else:
                                line_scoped = self._try_resolve_func_header_or_deletion(
                                    p, qual, h, h_s, h_e, s_start, s_body, s_end
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

                    # Level B: Strictly inside class body (excluding class header/decorators), or Guard 3B-1 pure leaf-class deletion
                    elif (
                        kflag == 2
                        and qual != "_Dialect"
                        and h_e <= s_end
                        and (
                            s_start < h_s
                            or (
                                s_start == h_s
                                and h_e == s_end
                                and not h.added_lines
                                and qual.split(".")[-1] not in self.unsafe_attrs
                                and qual.split(".")[-1] in self.attr_reader_tests
                                and not any(
                                    sub_s[4] in (0, 1, 2) and s_start <= sub_s[0] and sub_s[2] <= s_end and sub_s[3] != qual
                                    for sub_s in syms
                                )
                            )
                        )
                    ):
                        is_pure_class_del = s_start == h_s and h_e == s_end and not h.added_lines
                        if not is_pure_class_del:
                            # First check if this class-body hunk is a pure deletion of 2+ sibling methods inside the class
                            multi_del = self._try_resolve_multi_method_deletion(p, h, h_s, h_e, syms)
                            if multi_del is not None:
                                m_tests, m_tags, m_all_tok = multi_del
                                selected.update(m_tests)
                                for qtag in m_tags:
                                    if qtag not in triggers["tier3_symbols"]:
                                        triggers["tier3_symbols"].append(qtag)
                                if m_tests and p.startswith("sqlglot/typing/"):
                                    selected.update(self.by_file.get("tests/test_typing.py", []))
                                if p in self.file_to_dialect:
                                    d = self.file_to_dialect[p]
                                    if not m_all_tok and d not in triggers["tier2_dialects"]:
                                        triggers["tier2_dialects"].append(d)
                                if not m_all_tok:
                                    all_tokenizer_only = False
                                span_resolved = True
                                break
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
                            if is_pure_class_del:
                                cls_set.update(self.attr_reader_tests.get(qual.split(".")[-1], []))
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
