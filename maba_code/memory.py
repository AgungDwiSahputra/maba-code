"""Memori proyek berlapis (setara CLAUDE.md): MABA.md.

Urutan muat (yang lebih spesifik muncul belakangan sehingga lebih diprioritaskan):
  1. ~/.maba/MABA.md                      (preferensi pribadi, semua proyek)
  2. MABA.md dan .maba/MABA.md            dari akar drive turun ke folder kerja
  3. MABA.local.md di folder kerja        (catatan pribadi, jangan di-commit)
Baris ``@jalur/ke/file.md`` di dalam file disisipkan (maks. 3 tingkat).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from maba_code.config import maba_home, project_key

MAX_FILE_BYTES = 40_000
AUTO_MEMORY_MAX_LINES = 200  # sama dengan Claude Code: hanya 200 baris / 25 KB pertama MEMORY.md yang dimuat
AUTO_MEMORY_MAX_BYTES = 25_000
MAX_TOTAL_CHARS = 60_000
MAX_IMPORT_DEPTH = 3
_IMPORT = re.compile(r"^@(\S+\.md)\s*$")


@dataclass
class MemoryFile:
    path: Path
    text: str


def _read(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES * 4:
            return None
        return path.read_text(encoding="utf-8-sig", errors="replace")[:MAX_FILE_BYTES]
    except OSError:
        return None


def _expand(text: str, base: Path, depth: int, seen: set[Path]) -> str:
    if depth >= MAX_IMPORT_DEPTH:
        return text
    out: list[str] = []
    for line in text.splitlines():
        m = _IMPORT.match(line.strip())
        if not m:
            out.append(line)
            continue
        target = (base / m.group(1)).resolve()
        if target in seen:
            out.append(f"<!-- import berulang dilewati: {m.group(1)} -->")
            continue
        body = _read(target)
        if body is None:
            out.append(f"<!-- import tidak ditemukan: {m.group(1)} -->")
            continue
        seen.add(target)
        out.append(_expand(body, target.parent, depth + 1, seen))
    return "\n".join(out)


def candidate_paths(cwd: Path) -> list[Path]:
    paths = [maba_home() / "MABA.md"]
    chain = [cwd, *cwd.parents][:8]
    for folder in reversed(chain):
        paths += [folder / "MABA.md", folder / ".maba" / "MABA.md"]
    paths.append(cwd / "MABA.local.md")
    seen: set[Path] = set()
    unique = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def load_memory(cwd: Path) -> list[MemoryFile]:
    files: list[MemoryFile] = []
    total = 0
    for path in candidate_paths(cwd):
        raw = _read(path)
        if not raw or not raw.strip():
            continue
        text = _expand(raw, path.parent, 0, {path.resolve()}).strip()
        if total + len(text) > MAX_TOTAL_CHARS:
            break
        total += len(text)
        files.append(MemoryFile(path, text))
    return files


def render_memory(files: list[MemoryFile]) -> str:
    return "\n\n".join(f"<!-- {f.path} -->\n{f.text}" for f in files)


# ------------------------------------------------------------------ memori otomatis
def auto_memory_dir(cwd: Path) -> Path:
    """Folder catatan yang ditulis agen sendiri, per proyek (di luar repo, tidak ikut ter-commit)."""
    return maba_home() / "projects" / project_key(cwd) / "memory"


def load_auto_memory(folder: Path) -> str:
    """Isi indeks MEMORY.md (dipotong 200 baris / 25 KB); file topik dibaca agen bila perlu."""
    index = folder / "MEMORY.md"
    try:
        raw = index.read_bytes()[:AUTO_MEMORY_MAX_BYTES]
    except OSError:
        return ""
    lines = raw.decode("utf-8-sig", errors="replace").splitlines()
    text = "\n".join(lines[:AUTO_MEMORY_MAX_LINES])
    if len(lines) > AUTO_MEMORY_MAX_LINES or index.stat().st_size > AUTO_MEMORY_MAX_BYTES:
        text += "\n[MEMORY.md truncated: keep the index short, one line per memory]"
    return text


def list_auto_memory(folder: Path) -> list[Path]:
    return sorted(folder.glob("*.md")) if folder.is_dir() else []
