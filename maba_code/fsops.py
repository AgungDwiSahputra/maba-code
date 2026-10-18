"""Operasi file murni (tanpa I/O jaringan): teks, diff, glob, deteksi file sensitif."""

from __future__ import annotations

import difflib
import fnmatch
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

IGNORED_DIRS = frozenset(
    {
        ".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", ".idea", ".vs", "dist", "build", "bin", "obj", ".next", ".turbo", "target",
    }
)

# File yang tidak boleh dibaca/ditulis agen (rahasia). Contoh .env.example tetap boleh.
SENSITIVE_NAME_GLOBS = (
    ".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "id_rsa*", "id_ed25519*", "id_ecdsa*",
    "credentials", "credentials.json", ".netrc", ".npmrc", ".pypirc", "*.tfstate", "*.kdbx",
)
SENSITIVE_DIR_NAMES = frozenset({".aws", ".ssh", ".gnupg"})
_SAFE_SUFFIXES = (".example", ".sample", ".template", ".dist")

MAX_LINE_CHARS = 2000
MAX_READ_BYTES = 20 * 1024 * 1024


class NotUtf8Error(ValueError):
    """File bukan teks UTF-8 yang valid."""


def is_sensitive_path(path: Path | str) -> bool:
    p = Path(path)
    parts = {part.lower() for part in p.parts}
    if parts & SENSITIVE_DIR_NAMES:
        return True
    name = p.name.lower()
    if name.endswith(_SAFE_SUFFIXES):
        return False
    return any(fnmatch.fnmatch(name, pattern) for pattern in SENSITIVE_NAME_GLOBS)


def looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


@dataclass
class TextFile:
    text: str  # selalu memakai "\n"
    newline: str  # "\n" atau "\r\n" (gaya asli file)
    bom: bool


def decode_text(raw: bytes) -> TextFile:
    bom = raw.startswith(b"\xef\xbb\xbf")
    if bom:
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NotUtf8Error("file bukan UTF-8 yang valid") from exc
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n")
    newline = "\r\n" if crlf and crlf >= lf - crlf else "\n"
    return TextFile(text.replace("\r\n", "\n"), newline, bom)


def encode_text(text: str, newline: str = "\n", bom: bool = False) -> bytes:
    body = text.replace("\n", newline) if newline != "\n" else text
    data = body.encode("utf-8")
    return (b"\xef\xbb\xbf" + data) if bom else data


def default_newline_for(path: Path) -> str:
    return "\r\n" if path.suffix.lower() in {".bat", ".cmd"} else "\n"


def number_lines(lines: list[str], start: int) -> str:
    out = []
    for i, line in enumerate(lines, start):
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + f"... [{len(line) - MAX_LINE_CHARS} chars truncated]"
        out.append(f"{i:>6}\t{line}")
    return "\n".join(out)


def truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit // 3
    tail = limit - head
    cut = len(text) - head - tail
    return f"{text[:head]}\n... [{cut} characters truncated] ...\n{text[-tail:]}"


# --------------------------------------------------------------------------- edit


@dataclass
class EditPlan:
    new_text: str | None
    error: str | None = None
    replacements: int = 0
    first_line: int = 1


_LINE_PREFIX = re.compile(r"^\s*\d+\t", re.M)


def plan_edit(text: str, old: str, new: str, replace_all: bool = False) -> EditPlan:
    old = old.replace("\r\n", "\n")
    new = new.replace("\r\n", "\n")
    if old == new:
        return EditPlan(None, "old_string and new_string are identical; nothing to change.")
    if old == "":
        return EditPlan(None, "old_string is empty. To create or fully replace a file use write_file.")
    count = text.count(old)
    if count == 0:
        hint = ""
        if _LINE_PREFIX.search(old):
            hint = " old_string appears to contain line-number prefixes copied from read_file output; remove them."
        elif old.strip() and old.strip() in text:
            hint = " A similar string exists but leading/trailing whitespace or indentation differs; re-read the file."
        return EditPlan(None, "old_string was not found in the file." + hint)
    if count > 1 and not replace_all:
        return EditPlan(
            None,
            f"old_string matches {count} places. Add more surrounding context to make it unique, "
            "or set replace_all=true to change every occurrence.",
        )
    first_line = text[: text.find(old)].count("\n") + 1
    new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    return EditPlan(new_text, None, count if replace_all else 1, first_line)


def unified_diff(old: str, new: str, label: str, context: int = 3) -> str:
    diff = difflib.unified_diff(
        old.splitlines(), new.splitlines(), fromfile=f"a/{label}", tofile=f"b/{label}", lineterm="", n=context
    )
    return "\n".join(diff)


def diff_stats(diff: str) -> tuple[int, int]:
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


# --------------------------------------------------------------------------- glob


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Glob gaya ripgrep/gitignore: ``*``, ``?``, ``[..]``, ``{a,b}``, ``**``.

    Pola tanpa ``/`` dicocokkan pada kedalaman berapa pun (``*.py`` == ``**/*.py``).
    """
    pat = pattern.replace("\\", "/")
    if pat.startswith("./"):
        pat = pat[2:]
    prefix = "" if "/" in pat else "(?:.*/)?"
    out: list[str] = []
    i = 0
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if pat.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            if pat.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pat.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = pat[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        elif c == "{":
            j = pat.find("}", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                alts = pat[i + 1 : j].split(",")
                out.append("(?:" + "|".join(re.escape(a) for a in alts) + ")")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + prefix + "".join(out) + "$", re.IGNORECASE if os.name == "nt" else 0)


def iter_files(root: Path, ignore_dirs: frozenset[str] = IGNORED_DIRS) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in ignore_dirs and d.lower() not in SENSITIVE_DIR_NAMES)
        base = Path(dirpath)
        for name in sorted(filenames):
            yield base / name


def match_glob(root: Path, pattern: str, limit: int = 100) -> tuple[list[Path], bool]:
    """Kembalikan (file cocok terbaru dulu, apakah terpotong)."""
    regex = glob_to_regex(pattern)
    hits: list[tuple[float, Path]] = []
    for path in iter_files(root):
        rel = path.relative_to(root).as_posix()
        if regex.match(rel):
            try:
                hits.append((path.stat().st_mtime, path))
            except OSError:
                continue
    hits.sort(key=lambda item: item[0], reverse=True)
    truncated = len(hits) > limit
    return [p for _, p in hits[:limit]], truncated
