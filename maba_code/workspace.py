"""Keadaan bersama untuk satu sesi kerja: cwd, pelacakan 'sudah dibaca', checkpoint, todo."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROTECTED_DIRS = frozenset({".git", ".maba", ".vscode", ".idea", ".husky"})


@dataclass
class Checkpoint:
    turn: int
    path: Path
    before: bytes | None  # None = file belum ada sebelum agen menulisnya


@dataclass
class ToolNote:
    """Catatan ringkas dari tool untuk ditampilkan UI (bukan untuk model)."""

    summary: str = ""
    detail: str = ""  # mis. diff atau cuplikan output
    kind: str = "text"  # "diff" | "output" | "text"


class Workspace:
    def __init__(self, cwd: Path | str):
        self.cwd = Path(cwd).resolve()
        self.read_state: dict[str, int] = {}
        self.checkpoints: list[Checkpoint] = []
        self.turn = 0
        self.todos: list[dict[str, str]] = []
        self.notes: dict[str, ToolNote] = {}
        self.write_lock = asyncio.Lock()  # tool tulis/berbahaya berjalan satu per satu
        self.dialog_lock = asyncio.Lock()  # satu dialog izin pada satu waktu (agen utama + semua sub-agen)
        self.sub_usage = {"in": 0, "out": 0}
        self.plan_approved = False  # diset PermissionHook saat pengguna menyetujui rencana
        # Folder di luar cwd yang boleh DIBACA tanpa dialog (skill, log task latar belakang, memori otomatis).
        self.trusted_read_roots: list[Path] = []
        self.memory_dir: Path | None = None  # folder memori otomatis (tulis .md di sini diizinkan)
        # Diisi oleh sesi:
        self.on_todos: Callable[[list[dict[str, str]]], None] | None = None
        self.get_mode: Callable[[], str] = lambda: "default"
        self.run_subagent: Callable[[str, str, Any], Awaitable[str]] | None = None
        self.run_skill: Callable[[str, str, Any], Awaitable[str]] | None = None
        self.web_fetch: Callable[[str, str, Any], Awaitable[str]] | None = None
        self.background: Any = None  # tasks.BackgroundTasks

    # ------------------------------------------------------------------ path
    def resolve(self, raw: str | os.PathLike[str] | None) -> Path:
        text = os.path.expandvars(os.path.expanduser(str(raw or ".")))
        path = Path(text)
        if not path.is_absolute():
            path = self.cwd / path
        return Path(os.path.normpath(path))

    def inside(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.cwd)
            return True
        except ValueError:
            return False

    def display(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.cwd).as_posix() or "."
        except ValueError:
            return str(path)

    def in_trusted_root(self, path: Path) -> bool:
        resolved = path.resolve()
        for root in self.trusted_read_roots:
            try:
                resolved.relative_to(root.resolve())
                return True
            except ValueError:
                continue
        return False

    def in_memory_dir(self, path: Path) -> bool:
        if self.memory_dir is None:
            return False
        try:
            path.resolve().relative_to(self.memory_dir.resolve())
            return True
        except ValueError:
            return False

    def is_protected(self, path: Path) -> bool:
        try:
            parts = path.resolve().relative_to(self.cwd).parts
        except ValueError:
            return False
        return any(part.lower() in PROTECTED_DIRS for part in parts)

    # ------------------------------------------------------- read-before-edit
    @staticmethod
    def _key(path: Path) -> str:
        return os.path.normcase(str(path.resolve()))

    def mark_read(self, path: Path) -> None:
        try:
            self.read_state[self._key(path)] = path.stat().st_mtime_ns
        except OSError:
            pass

    def was_read_fresh(self, path: Path) -> bool:
        """True bila file sudah dibaca dan belum berubah sejak itu."""
        recorded = self.read_state.get(self._key(path))
        if recorded is None:
            return False
        try:
            return path.stat().st_mtime_ns == recorded
        except OSError:
            return False

    # ------------------------------------------------------------ checkpoints
    def begin_turn(self) -> int:
        self.turn += 1
        return self.turn

    def checkpoint(self, path: Path, before: bytes | None) -> None:
        self.checkpoints.append(Checkpoint(self.turn, path, before))

    def last_changed(self) -> list[str]:
        """File (nama tampilan) yang diubah agen pada giliran terakhir yang punya perubahan; yang akan dipulihkan /undo."""
        if not self.checkpoints:
            return []
        turn = max(c.turn for c in self.checkpoints)
        return sorted({self.display(c.path) for c in self.checkpoints if c.turn == turn})

    def undo_last_turn(self) -> list[str]:
        """Kembalikan semua file yang diubah agen pada giliran terakhir yang punya perubahan.

        Perubahan lewat ``run_command`` tidak dilacak (sama seperti Claude Code).
        """
        if not self.checkpoints:
            return []
        turn = max(c.turn for c in self.checkpoints)
        batch = [c for c in self.checkpoints if c.turn == turn]
        self.checkpoints = [c for c in self.checkpoints if c.turn != turn]
        report: list[str] = []
        for cp in reversed(batch):
            try:
                if cp.before is None:
                    if cp.path.exists():
                        cp.path.unlink()
                    report.append(f"dihapus   {self.display(cp.path)}")
                else:
                    cp.path.write_bytes(cp.before)
                    report.append(f"dipulihkan {self.display(cp.path)}")
            except OSError as exc:
                report.append(f"GAGAL     {self.display(cp.path)} ({exc})")
            self.read_state.pop(self._key(cp.path), None)
        return report

    # -------------------------------------------------------------- catatan UI
    def note(self, tool_use_id: str, summary: str = "", detail: str = "", kind: str = "text") -> None:
        self.notes[tool_use_id] = ToolNote(summary, detail, kind)

    def pop_note(self, tool_use_id: str) -> ToolNote | None:
        return self.notes.pop(tool_use_id, None)
