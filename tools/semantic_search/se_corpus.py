#!/usr/bin/env python3
"""Markdown corpus discovery + heading-aware chunking for semantic_search.

Provenance is preserved for every chunk: the source path (relative to
OMNI_DIR when inside it), the markdown heading breadcrumb, the 1-based line
range in the original file, and a content hash.
"""

import hashlib
import os
import re
from pathlib import Path

DEFAULT_GLOBS = ("**/*.md",)
SKIP_DIRS = {".git", "node_modules", "target", ".venv", "__pycache__", ".cache"}
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FRONTMATTER_FENCE = "---"


def split_frontmatter(text):
    """Return (body, frontmatter_line_count) - YAML frontmatter is not indexed."""
    lines = text.split("\n")
    if lines and lines[0].strip() == _FRONTMATTER_FENCE:
        for idx in range(1, len(lines)):
            if lines[idx].strip() == _FRONTMATTER_FENCE:
                return "\n".join(lines[idx + 1:]), idx + 1
    return text, 0


def discover_files(roots, globs=None, omni_dir=None):
    """Expand corpus roots + globs into a sorted list of absolute file paths."""
    globs = tuple(globs) if globs else DEFAULT_GLOBS
    base = Path(omni_dir) if omni_dir else None
    found = set()
    for root in roots:
        if not root:
            continue
        root_path = Path(root)
        if not root_path.is_absolute():
            root_path = (base / root_path) if base else Path.cwd() / root_path
        if not root_path.exists():
            continue
        for pattern in globs:
            for path in root_path.glob(pattern):
                if not path.is_file():
                    continue
                if any(part in SKIP_DIRS for part in path.parts):
                    continue
                found.add(path.resolve())
    return sorted(found, key=lambda p: str(p))


def display_path(path, omni_dir=None):
    """Path relative to OMNI_DIR when possible (stable provenance across hosts)."""
    path = Path(path)
    if omni_dir:
        try:
            return str(path.resolve().relative_to(Path(omni_dir).resolve()))
        except ValueError:
            pass
    return str(path)


def content_hash(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def chunk_markdown(text, max_chars=1200, overlap=200, source_path="", start_line=1):
    """Heading-aware markdown chunking with line-number provenance.

    Sections are split at markdown headings first; a section larger than
    max_chars is further split on newline boundaries with `overlap` characters
    of carry-over so a sentence spanning the split is still retrievable.
    Returns a list of dicts: {path, heading, line_start, line_end, text, hash}.

    Every emitted chunk is at most max_chars characters, so a single huge
    section can never blow up the process memory of the caller.
    """
    body, skipped = split_frontmatter(text)
    lines = body.split("\n")
    base_line = start_line + skipped
    breadcrumb = []
    chunks = []

    def flush(buf, heading, first_line, last_line):
        chunk_text = "\n".join(buf).strip()
        if not chunk_text:
            return
        chunks.append({
            "path": source_path,
            "heading": heading,
            "line_start": first_line,
            "line_end": last_line,
            "text": chunk_text,
            "hash": content_hash(source_path + "\n" + chunk_text),
        })

    buf = []
    buf_first = None
    buf_last = None
    buf_chars = 0
    heading = ""
    for idx, line in enumerate(lines):
        line_no = base_line + idx
        match = _HEADING_RE.match(line)
        if match:
            # A new heading closes the current buffer (headings are not indexed
            # as standalone chunks).
            if buf:
                _emit(buf, heading, buf_first, buf_last, max_chars, overlap, flush)
                buf, buf_first, buf_chars = [], None, 0
            level = len(match.group(1))
            title = match.group(2).strip()
            breadcrumb = breadcrumb[:level - 1]
            breadcrumb.append(title)
            heading = " > ".join([b for b in breadcrumb if b])
            continue
        if buf_first is None:
            buf_first = line_no
        buf.append(line)
        buf_last = line_no
        buf_chars += len(line) + 1
        if buf_chars > max_chars * 2:
            _emit(buf, heading, buf_first, buf_last, max_chars, overlap, flush)
            buf, buf_first, buf_chars = [], None, 0
    if buf:
        _emit(buf, heading, buf_first, buf_last, max_chars, overlap, flush)
    return chunks


def _emit(buf, heading, first_line, last_line, max_chars, overlap, flush):
    """Emit `buf` as one or more chunks of at most max_chars characters."""
    max_chars = max(200, int(max_chars or 1200))
    overlap = max(0, min(int(overlap or 0), max_chars // 2))
    text = "\n".join(buf)
    if len(text) <= max_chars:
        flush(buf, heading, first_line, last_line)
        return
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            newline = text.rfind("\n", start, end)
            if newline > start:
                end = newline
        piece = text[start:end]
        # translate character offsets back to lines of this buffer
        piece_first = first_line + text.count("\n", 0, start)
        piece_last = first_line + text.count("\n", 0, end)
        flush(piece.split("\n"), heading, piece_first, piece_last)
        if end >= len(text):
            break
        # Guarantee forward progress: when the overlap window would land at or
        # before the current start (short first line), jump past `end`
        # instead of re-emitting the same piece forever (this was an unbounded
        # loop that OOM-killed the indexer on large sections).
        next_start = end - overlap
        start = end if next_start <= start else next_start
        if start > len(text) - 1:
            break


def resolve_roots(roots, profile=None, omni_dir=None):
    """Expand '{profile}' placeholders and relative roots against OMNI_DIR."""
    resolved = []
    for root in roots:
        if not root:
            continue
        value = str(root).strip()
        if not value:
            continue
        if profile is not None:
            value = value.replace("{profile}", profile)
        resolved.append(value)
    return resolved
