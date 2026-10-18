"""Keadaan "apa yang sedang dikerjakan agen".

Satu sumber untuk tiga hal yang dilihat pengguna: baris status (spinner), ringkasan akhir giliran,
dan kutipan "agen menjelaskan" di dialog izin. Murni data + teks: tidak menyentuh terminal, sehingga
mudah diuji dengan jam palsu.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rich.text import Text

from maba_code import fsops
from maba_code.ui.theme import PHASES, activity_label, describe_tool, fmt_duration
from maba_code.workspace import ToolNote

SLOW_TOOL_SECONDS = 3.0  # lama tool baru ikut ditampilkan di status setelah selama ini
_SENTENCE_END = re.compile(r"(?<=[.!?:])\s+")


def _live_duration(seconds: float) -> str:
    """Durasi untuk baris status: detik utuh (desimal yang berdenyut terasa gelisah di spinner)."""
    return f"{int(seconds)}s" if seconds < 60 else fmt_duration(seconds)


@dataclass
class TurnLog:
    """Catatan satu giliran (direset di begin_turn)."""

    steps: int = 0  # jumlah tool yang dijalankan agen utama
    commands: int = 0
    denied: int = 0
    changes: dict[str, list[int]] = field(default_factory=dict)  # path -> [baris ditambah, baris dihapus]
    sub_total: dict[str, int] = field(default_factory=dict)  # langkah sub-agen per nama (untuk ringkasan akhir)
    sub_unreported: dict[str, int] = field(default_factory=dict)  # langkah yang belum dilaporkan di baris hasil


def extract_intent(text: str, limit: int = 180) -> str:
    """Kalimat terakhir dari teks yang ditulis model sebelum memanggil tool ('' bila tidak ada)."""
    cleaned = " ".join(text.replace("`", "").replace("*", "").split())
    if not cleaned:
        return ""
    sentences = [s for s in _SENTENCE_END.split(cleaned) if s.strip()]
    intent = sentences[-1]
    if len(intent) < 20 and len(sentences) > 1:
        intent = f"{sentences[-2]} {intent}"
    return intent if len(intent) <= limit else intent[: limit - 1] + "…"


class Activity:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self.phase = "idle"
        self.detail = ""
        self.cancellable = False
        self.phase_started = clock()
        self.turn_started: float | None = None
        self.words = 0
        self._in_word = False
        self.waited = 0.0  # detik giliran ini yang dihabiskan menunggu pengguna (dialog)
        self._wait_started: float | None = None
        self.log = TurnLog()
        self.sub_current: dict[str, str] = {}

    # ----------------------------------------------------------------- giliran
    def begin_turn(self) -> None:
        self.turn_started = self._clock()
        self.words = 0
        self._in_word = False
        self.waited = 0.0
        self._wait_started = None
        self.log = TurnLog()
        self.sub_current = {}

    def end_turn(self) -> None:
        self.turn_started = None
        self.set_phase("idle")

    def elapsed(self) -> float:
        return 0.0 if self.turn_started is None else self._clock() - self.turn_started

    def work_elapsed(self) -> float:
        """Lama giliran tanpa waktu menunggu pengguna."""
        return max(0.0, self.elapsed() - self.waited)

    # ------------------------------------------------------------------ tahap
    def set_phase(self, phase: str, detail: str = "", cancellable: bool = True) -> None:
        if phase != self.phase or detail != self.detail:
            self.phase_started = self._clock()
        if phase == "think" and self.phase != "think":
            self.words = 0  # pesan model berikutnya (setelah hasil tool) mulai dari nol
        self.phase, self.detail, self.cancellable = phase, detail, cancellable and phase != "idle"

    def add_text(self, text: str) -> None:
        """Hitung kata yang sudah ditulis model (aman terhadap kata yang terpotong di batas potongan)."""
        for ch in text:
            if ch.isspace():
                self._in_word = False
            elif not self._in_word:
                self._in_word = True
                self.words += 1

    # --------------------------------------------------------------- menunggu
    def begin_wait(self) -> None:
        if self._wait_started is None:
            self._wait_started = self._clock()

    def end_wait(self) -> None:
        if self._wait_started is not None:
            self.waited += self._clock() - self._wait_started
            self._wait_started = None

    # ------------------------------------------------------------------- tool
    def tool_started(self, name: str, inp: dict[str, Any], running: int = 1) -> None:
        self.log.steps += 1
        if name == "run_command":
            self.log.commands += 1
        if running > 1:
            self.set_phase("tool", f"{running} langkah berjalan bersamaan")
        else:
            target = describe_tool(name, inp, 60)
            self.set_phase("tool", f"{activity_label(name)} {target}".strip())

    def tool_finished(self, name: str, inp: dict[str, Any], ok: bool, note: ToolNote | None, running: int = 0) -> None:
        self.record_change(name, inp, ok, note)
        if running > 0:
            self.set_phase("tool", f"menunggu {running} langkah lain")
        else:
            self.set_phase("think", f"membaca hasil langkah {self.log.steps}")

    def tool_denied(self) -> None:
        self.log.denied += 1
        self.set_phase("think", "menyesuaikan setelah aksi ditolak")

    def record_change(self, name: str, inp: dict[str, Any], ok: bool, note: ToolNote | None) -> None:
        if not ok or name not in ("write_file", "edit_file") or note is None or not note.detail:
            return
        if name == "write_file" and note.summary.startswith("dibuat"):
            added, removed = len(str(inp.get("content", "")).splitlines()), 0  # pratinjau note dipotong; hitung penuh
        else:
            added, removed = fsops.diff_stats(note.detail)
        entry = self.log.changes.setdefault(str(inp.get("path", "?")), [0, 0])
        entry[0] += added
        entry[1] += removed

    # --------------------------------------------------------------- sub-agen
    def sub_tool_finished(self, name: str, inp: dict[str, Any], ok: bool, note: ToolNote | None) -> None:
        """Hasil tool milik sub-agen: perubahan file & perintahnya ikut masuk ringkasan giliran."""
        self.record_change(name, inp, ok, note)
        if name == "run_command":
            self.log.commands += 1

    def sub_step(self, agent: str, text: str) -> int:
        """Satu langkah sub-agen; mengembalikan jumlah langkahnya sejauh ini."""
        total = self.log.sub_total.get(agent, 0) + 1
        self.log.sub_total[agent] = total
        self.log.sub_unreported[agent] = self.log.sub_unreported.get(agent, 0) + 1
        self.sub_current[agent] = text
        self.set_phase("tool", f"{agent} › {text} ({self.log.sub_unreported[agent]} langkah)")
        return total

    def take_sub_steps(self, agent: str) -> int:
        """Ambil (dan nolkan) langkah sub-agen yang belum dilaporkan di baris hasil tool-nya.

        Bila dua sub-agen bernama sama berjalan paralel, yang selesai lebih dulu mengambil semuanya.
        """
        self.sub_current.pop(agent, None)
        return self.log.sub_unreported.pop(agent, 0)

    # ----------------------------------------------------------------- render
    def headline(self) -> str:
        label = PHASES.get(self.phase, self.phase)
        if not label:
            return self.detail
        if self.phase == "write":
            return label
        return f"{label}: {self.detail}" if self.detail else label

    def render(self, esc_hint: str = "Esc untuk batal") -> Text:
        """Baris status: 'Membaca calc.py  langkah 3 · 12s  (Esc untuk batal)'."""
        meta: list[str] = []
        if self.phase == "write" and self.words:
            meta.append(f"{self.words} kata")
        if self.log.steps:
            meta.append(f"langkah {self.log.steps}")
        if self.turn_started is not None:
            meta.append(_live_duration(self.work_elapsed()))
        phase_age = self._clock() - self.phase_started
        if self.phase == "tool" and phase_age >= SLOW_TOOL_SECONDS:
            meta.append(f"langkah ini {_live_duration(phase_age)}")
        text = Text(self.headline() or "Bekerja")
        if meta:
            text.append("  " + " · ".join(meta), style="dim")
        if self.cancellable and esc_hint:
            text.append(f"  ({esc_hint})", style="dim")
        return text
