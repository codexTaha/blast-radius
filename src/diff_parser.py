"""Stage 3.1 — Fast, Zero-Dependency Unified Diff Parser.

Parses Git unified diffs into structured file diffs, hunks, pinned-commit line intervals,
and strictly verified Python `#`-comment detection in sub-millisecond time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Match standard unified diff hunk header: @@ -old_start[,old_count] +new_start[,new_count] @@ [header]
HUNK_HEADER_RE = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@(.*)")
# PEP 263 Python source encoding declaration on lines 1-2
PEP263_CODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+")


def _normalize_path(p: str) -> str:
    """Strip git diff prefixes ('a/', 'b/') and leading slashes."""
    p = p.strip()
    if p.startswith("a/") or p.startswith("b/"):
        p = p[2:]
    return p.lstrip("/")


def _is_python_hash_comment_or_empty(line: str) -> bool:
    """Strictly check if a Python line is empty or starts with '#' (never '*', '//', '--')."""
    stripped = line.strip()
    if not stripped:
        return True
    return stripped.startswith("#")


def _compress_intervals(lines: list[int]) -> list[tuple[int, int]]:
    """Compress a sorted list of line numbers into contiguous [start, end] intervals."""
    if not lines:
        return []
    sorted_unique = sorted(set(lines))
    intervals: list[tuple[int, int]] = []
    start = sorted_unique[0]
    end = start

    for line in sorted_unique[1:]:
        if line == end + 1:
            end = line
        else:
            intervals.append((start, end))
            start = end = line
    intervals.append((start, end))
    return intervals


@dataclass(slots=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header_context: str
    added_lines: list[tuple[int, str]] = field(default_factory=list)
    added_old_anchors: list[int] = field(default_factory=list)
    deleted_lines: list[tuple[int, str]] = field(default_factory=list)
    context_lines: list[str] = field(default_factory=list)
    context_with_lineno: list[tuple[int, str]] = field(default_factory=list)
    modified_lines: list[int] = field(default_factory=list)
    is_comment_only: bool = False
    is_whitespace_only: bool = False

    def finalize(self, is_python_file: bool = True) -> None:
        """Compute strict is_comment_only and is_whitespace_only flags."""
        all_pairs = self.added_lines + self.deleted_lines
        if not all_pairs:
            self.is_comment_only = is_python_file
            self.is_whitespace_only = True
            return

        # Strict Python comment-only check:
        # 1. File must be a .py file (SQL fixtures parse '#' metadata in _extract_meta!)
        # 2. No modified line on lines 1-2 (PEP 263 '# coding:' declarations affect compilation)
        # 3. Every added/deleted line must be empty or start with '#'
        # 4. No triple quotes anywhere in hunk (guards against '#' inside multiline strings)
        if is_python_file:
            has_encoding_decl = any(
                ln <= 2 and bool(PEP263_CODING_RE.match(text))
                for ln, text in all_pairs
            )
            has_triple_quote = any(
                '"""' in text or "'''" in text
                for _, text in all_pairs
            )
            if not has_encoding_decl and not has_triple_quote:
                self.is_comment_only = all(_is_python_hash_comment_or_empty(t) for _, t in all_pairs)
            else:
                self.is_comment_only = False
        else:
            self.is_comment_only = False

        added_stripped = [l.strip() for _, l in self.added_lines if l.strip()]
        deleted_stripped = [l.strip() for _, l in self.deleted_lines if l.strip()]
        self.is_whitespace_only = added_stripped == deleted_stripped

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_start": self.old_start,
            "old_count": self.old_count,
            "new_start": self.new_start,
            "new_count": self.new_count,
            "header_context": self.header_context,
            "modified_lines": self.modified_lines,
            "is_comment_only": self.is_comment_only,
            "is_whitespace_only": self.is_whitespace_only,
        }


@dataclass(slots=True)
class FileDiff:
    old_path: str | None
    new_path: str | None
    path: str
    is_new: bool = False
    is_deleted: bool = False
    is_rename: bool = False
    is_binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)
    modified_lines: list[int] = field(default_factory=list)
    modified_ranges: list[tuple[int, int]] = field(default_factory=list)
    insertion_anchors: list[tuple[int, int]] = field(default_factory=list)
    is_comment_only: bool = False
    is_whitespace_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "old_path": self.old_path,
            "new_path": self.new_path,
            "is_new": self.is_new,
            "is_deleted": self.is_deleted,
            "is_rename": self.is_rename,
            "is_binary": self.is_binary,
            "modified_lines": self.modified_lines,
            "modified_ranges": self.modified_ranges,
            "is_comment_only": self.is_comment_only,
            "is_whitespace_only": self.is_whitespace_only,
            "hunk_count": len(self.hunks),
        }


@dataclass(slots=True)
class ParsedPatch:
    files: list[FileDiff] = field(default_factory=list)
    is_empty: bool = True
    is_comment_only: bool = False
    is_whitespace_only: bool = False
    total_added: int = 0
    total_deleted: int = 0

    @property
    def file_paths(self) -> list[str]:
        return [f.path for f in self.files]

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_empty": self.is_empty,
            "is_comment_only": self.is_comment_only,
            "is_whitespace_only": self.is_whitespace_only,
            "total_added": self.total_added,
            "total_deleted": self.total_deleted,
            "files": [f.to_dict() for f in self.files],
        }


def parse_patch(patch_text: str) -> ParsedPatch:
    """Parse a unified git diff patch into structured FileDiff and Hunk representations."""
    if not patch_text or not patch_text.strip():
        return ParsedPatch(is_empty=True, is_comment_only=True, is_whitespace_only=True)

    files: list[FileDiff] = []
    current_file: FileDiff | None = None
    current_hunk: Hunk | None = None
    cur_old = 0
    cur_new = 0
    in_deletion_group = False

    total_added = 0
    total_deleted = 0

    lines = patch_text.lstrip("\ufeff").replace("\r\n", "\n").split("\n")

    for line in lines:
        if line.startswith("diff --git"):
            if current_hunk and current_file:
                current_hunk.finalize(is_python_file=current_file.path.endswith(".py"))
                current_file.hunks.append(current_hunk)
                current_hunk = None

            parts = line.split()
            old_p = _normalize_path(parts[2]) if len(parts) > 2 else ""
            new_p = _normalize_path(parts[3]) if len(parts) > 3 else ""
            eff_path = new_p if new_p and new_p not in ("/dev/null", "dev/null") else old_p

            current_file = FileDiff(
                old_path=old_p if old_p and old_p != "dev/null" else None,
                new_path=new_p if new_p and new_p != "dev/null" else None,
                path=eff_path,
            )
            files.append(current_file)
            in_deletion_group = False
            continue

        if current_file is None:
            if line.startswith("--- ") or line.startswith("+++ "):
                current_file = FileDiff(old_path=None, new_path=None, path="")
                files.append(current_file)
            else:
                continue

        if line.startswith("--- "):
            raw = line[4:].strip()
            if raw in ("/dev/null", "dev/null"):
                current_file.is_new = True
                current_file.old_path = None
            else:
                current_file.old_path = _normalize_path(raw)
            continue

        if line.startswith("+++ "):
            raw = line[4:].strip()
            if raw in ("/dev/null", "dev/null"):
                current_file.is_deleted = True
                current_file.new_path = None
                current_file.path = current_file.old_path or ""
            else:
                current_file.new_path = _normalize_path(raw)
                current_file.path = current_file.new_path
            continue

        if line.startswith("rename from "):
            current_file.is_rename = True
            current_file.old_path = _normalize_path(line[12:])
            continue

        if line.startswith("rename to "):
            current_file.is_rename = True
            current_file.new_path = _normalize_path(line[10:])
            current_file.path = current_file.new_path
            continue

        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            current_file.is_binary = True
            current_file.is_comment_only = False
            current_file.is_whitespace_only = False
            continue

        if line.startswith("@@"):
            if current_hunk:
                current_hunk.finalize(is_python_file=current_file.path.endswith(".py"))
                current_file.hunks.append(current_hunk)

            m = HUNK_HEADER_RE.match(line)
            if m:
                old_start = int(m.group(1))
                old_count = int(m.group(2)) if m.group(2) is not None else 1
                new_start = int(m.group(3))
                new_count = int(m.group(4)) if m.group(4) is not None else 1
                header_context = m.group(5).strip()

                current_hunk = Hunk(
                    old_start=old_start,
                    old_count=old_count,
                    new_start=new_start,
                    new_count=new_count,
                    header_context=header_context,
                )
                cur_old = old_start
                cur_new = new_start
                in_deletion_group = False
            continue

        if current_hunk is not None:
            if line.startswith("+") and not line.startswith("+++"):
                content = line[1:]
                current_hunk.added_lines.append((cur_new, content))
                current_hunk.added_old_anchors.append(cur_old)
                if current_file.is_new or current_hunk.old_start == 0:
                    current_hunk.modified_lines.append(cur_new)
                elif not in_deletion_group:
                    # Pure insertion in existing file: anchor to pinned-commit cur_old
                    anchor = max(1, cur_old)
                    current_hunk.modified_lines.append(anchor)
                    current_file.insertion_anchors.append((max(1, cur_old - 1), anchor))
                total_added += 1
                cur_new += 1

            elif line.startswith("-") and not line.startswith("---"):
                content = line[1:]
                current_hunk.deleted_lines.append((cur_old, content))
                current_hunk.modified_lines.append(cur_old)
                in_deletion_group = True
                total_deleted += 1
                cur_old += 1

            elif line.startswith(" "):
                current_hunk.context_lines.append(line[1:])
                current_hunk.context_with_lineno.append((cur_old, line[1:]))
                in_deletion_group = False
                cur_old += 1
                cur_new += 1

    if current_hunk and current_file:
        current_hunk.finalize(is_python_file=current_file.path.endswith(".py"))
        current_file.hunks.append(current_hunk)

    for f in files:
        all_mod_lines: list[int] = []
        file_comment_only = bool(f.hunks) and f.path.endswith(".py")
        file_ws_only = bool(f.hunks)

        for h in f.hunks:
            all_mod_lines.extend(h.modified_lines)
            if not h.is_comment_only:
                file_comment_only = False
            if not h.is_whitespace_only:
                file_ws_only = False

        if f.is_binary or f.is_new or f.is_deleted or f.is_rename:
            file_comment_only = False
            file_ws_only = False

        f.modified_lines = sorted(set(all_mod_lines))
        f.modified_ranges = _compress_intervals(f.modified_lines)
        f.is_comment_only = file_comment_only
        f.is_whitespace_only = file_ws_only

    is_all_comment = bool(files) and all(f.is_comment_only for f in files)
    is_all_ws = bool(files) and all(f.is_whitespace_only for f in files)

    return ParsedPatch(
        files=files,
        is_empty=len(files) == 0,
        is_comment_only=is_all_comment,
        is_whitespace_only=is_all_ws,
        total_added=total_added,
        total_deleted=total_deleted,
    )
