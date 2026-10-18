"""Simbol, warna, dan label tampilan (Bahasa Indonesia)."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any

ACCENT = "cyan"
MODE_STYLE = {"default": "grey70", "acceptEdits": "green", "plan": "dodger_blue1", "bypass": "bold red"}
MODE_SHORT = {"default": "standar", "acceptEdits": "terima-edit", "plan": "rencana", "bypass": "BYPASS"}
# Keterangan sangat singkat di toolbar, dan satu kalimat penuh yang muncul sesaat saat mode diganti (Shift+Tab).
MODE_TAG = {"default": "tanya dulu", "acceptEdits": "edit otomatis", "plan": "hanya baca", "bypass": "TANPA TANYA"}
MODE_HINT = {
    "default": "Mode standar: agen bertanya dulu sebelum menulis file atau menjalankan perintah.",
    "acceptEdits": "Mode terima-edit: edit file di folder kerja otomatis; perintah shell tetap ditanya.",
    "plan": "Mode rencana: agen hanya membaca & menyusun rencana; tidak ada yang diubah sebelum Anda setuju.",
    "bypass": "Mode BYPASS: semua aksi berjalan tanpa bertanya. Pakai hanya di lingkungan yang Anda percaya.",
}


@dataclass(frozen=True)
class Symbols:
    bullet: str
    branch: str
    ok: str
    fail: str
    prompt: str
    todo_pending: str
    todo_active: str
    todo_done: str
    sep: str
    wordmark: tuple[str, ...]
    info: str
    warn: str
    meter_on: str
    meter_off: str
    minus: str


UNICODE = Symbols(
    bullet="●", branch="⎿", ok="✓", fail="✗", prompt="❯", todo_pending="☐", todo_active="◼", todo_done="☑", sep="·",
    wordmark=("╔╦╗╔═╗╔╗ ╔═╗", "║║║╠═╣╠╩╗╠═╣", "╩ ╩╩ ╩╚═╝╩ ╩"),
    info="ℹ", warn="⚠", meter_on="▰", meter_off="▱", minus="−",
)
ASCII = Symbols(
    bullet="*", branch="|_", ok="v", fail="x", prompt=">", todo_pending="[ ]", todo_active="[~]", todo_done="[x]", sep="-",
    wordmark=("MABA",),
    info="i", warn="!", meter_on="#", meter_off="-", minus="-",
)


def pick_symbols() -> Symbols:
    if os.environ.get("MABA_ASCII"):
        return ASCII
    encoding = (getattr(sys.stdout, "encoding", None) or "").lower().replace("-", "")
    return UNICODE if encoding in {"utf8", "utf8sig"} else ASCII


TOOL_LABELS = {
    "read_file": "Baca",
    "write_file": "Tulis",
    "edit_file": "Edit",
    "glob_files": "Cari file",
    "grep_files": "Cari teks",
    "run_command": "Jalankan",
    "todo_write": "Perbarui todo",
    "exit_plan_mode": "Ajukan rencana",
    "explore": "Jelajahi",
    "task": "Sub-agen",
    "skill": "Skill",
    "task_stop": "Hentikan task",
    "web_fetch": "Ambil web",
}

# Bentuk "sedang melakukan" untuk baris status (TOOL_LABELS dipakai untuk baris yang dicetak permanen).
TOOL_ACTIVITY = {
    "read_file": "Membaca",
    "write_file": "Menulis",
    "edit_file": "Mengedit",
    "glob_files": "Mencari file",
    "grep_files": "Mencari teks",
    "run_command": "Menjalankan",
    "todo_write": "Memperbarui todo",
    "exit_plan_mode": "Mengajukan rencana",
    "explore": "Menjelajahi proyek",
    "task": "Sub-agen bekerja",
    "skill": "Memuat skill",
    "task_stop": "Menghentikan task",
    "web_fetch": "Mengambil halaman web",
}


def activity_label(name: str) -> str:
    if name in TOOL_ACTIVITY:
        return TOOL_ACTIVITY[name]
    if name.startswith("mcp__"):
        parts = name.split("__")
        return f"Memanggil MCP {parts[1]}:{parts[2]}" if len(parts) >= 3 else "Memanggil tool MCP"
    return name


# Tahap pekerjaan agen. Label kosong = baris status memakai detailnya saja (mis. "Membaca calc.py").
PHASES = {
    "idle": "",
    "prepare": "Menyiapkan",
    "think": "Berpikir",
    "write": "Menulis jawaban",
    "tool": "",
    "compact": "Meringkas percakapan",
    "mcp": "Menghubungkan server MCP",
    "custom": "",
}

# Level pesan notice(): nama -> (atribut ikon di Symbols atau None, gaya rich).
NOTICE_LEVELS: dict[str, tuple[str | None, str]] = {
    "info": ("info", ACCENT),
    "success": ("ok", "green"),
    "warn": ("warn", "yellow"),
    "error": ("fail", "red"),
    "dim": (None, "dim"),
}
# Pemanggil lama memakai nama warna sebagai "gaya"; dipetakan ke level agar semua pesan otomatis berikon.
_STYLE_TO_LEVEL = {"cyan": "info", "green": "success", "yellow": "warn", "red": "error", "dim": "dim", "": "dim"}


def notice_level(style: str) -> str:
    """Level untuk sebuah 'gaya' notice; gaya tak dikenal -> 'custom' (dicetak apa adanya, tanpa ikon)."""
    if style in NOTICE_LEVELS:
        return style
    return _STYLE_TO_LEVEL.get(style, "custom")


# Lencana kelas izin pada dialog: kelas -> (teks, gaya, apakah Enter TIDAK boleh menyetujui).
CLASS_BADGE: dict[str, tuple[str, str, bool]] = {
    "read": ("BACA", "bold green", False),
    "write": ("TULIS", "bold yellow", False),
    "danger": ("BERBAHAYA", "bold red", True),
    "network": ("INTERNET", "bold magenta", True),
    "mcp": ("MCP", "bold magenta", True),
    "plan": ("RENCANA", f"bold {ACCENT}", False),
    "unknown": ("TOOL", "bold yellow", True),
}


# Ambang warna meter konteks (persen): kuning = mulai penuh, merah = hampir diringkas otomatis.
CTX_WARN_PCT = 60
CTX_CRIT_PCT = 85


def fmt_duration(seconds: float) -> str:
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs:02d}s"


def meter(pct: int | None, width: int, sym: Symbols) -> str:
    """Batang kecil, mis. ▰▰▱▱▱ untuk 32%."""
    filled = round(width * max(0, min(100, pct or 0)) / 100)
    return sym.meter_on * filled + sym.meter_off * (width - filled)


def _one_line(text: str, limit: int) -> str:
    line = " ".join(str(text).split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def describe_tool(name: str, inp: dict[str, Any], limit: int = 110) -> str:
    """Ringkasan satu baris argumen tool untuk ditampilkan ke pengguna."""
    if name == "read_file":
        extra = f":{inp['offset']}" if int(inp.get("offset") or 1) > 1 else ""
        return _one_line(f"{inp.get('path', '')}{extra}", limit)
    if name in ("write_file", "edit_file"):
        return _one_line(inp.get("path", ""), limit)
    if name == "glob_files":
        where = f" di {inp['path']}" if inp.get("path") not in (None, "", ".") else ""
        return _one_line(f"{inp.get('pattern', '')}{where}", limit)
    if name == "grep_files":
        scope = inp.get("glob") or (inp.get("path") if inp.get("path") not in (None, "", ".") else "")
        return _one_line(f"{inp.get('pattern', '')}" + (f"  ({scope})" if scope else ""), limit)
    if name == "run_command":
        prefix = "[latar belakang] " if inp.get("run_in_background") else ""
        return _one_line(prefix + str(inp.get("command", "")), limit)
    if name == "todo_write":
        return f"{len(inp.get('todos') or [])} item"
    if name == "explore":
        return _one_line(inp.get("task", ""), limit)
    if name == "task":
        label = inp.get("description") or inp.get("prompt", "")
        return _one_line(f"{inp.get('subagent_type', '')} › {label}", limit)
    if name == "skill":
        return _one_line(f"{inp.get('name', '')} {inp.get('arguments', '')}".strip(), limit)
    if name == "task_stop":
        return _one_line(inp.get("task_id", ""), limit)
    if name == "web_fetch":
        return _one_line(inp.get("url", ""), limit)
    if name.startswith("mcp__"):
        return _one_line(", ".join(f"{k}={v}" for k, v in inp.items()), limit)
    return ""
