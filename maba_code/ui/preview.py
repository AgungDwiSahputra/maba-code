"""Pratinjau semua tampilan UI dengan data palsu: tanpa Bedrock, tanpa biaya, tanpa mengubah file.

    python -m maba_code.ui.preview                  # semua komponen di terminal ini
    python -m maba_code.ui.preview --width 60       # uji terminal sempit
    python -m maba_code.ui.preview --ascii          # uji mode tanpa simbol Unicode (MABA_ASCII)
    python -m maba_code.ui.preview --svg out.svg    # simpan sebagai gambar untuk ditinjau/dibagikan
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console
from rich.rule import Rule

from maba_code.config import Settings
from maba_code.permissions import PermissionRequest
from maba_code.ui.activity import Activity
from maba_code.ui.terminal import TerminalUI
from maba_code.ui.theme import ASCII, UNICODE
from maba_code.workspace import ToolNote

_DIFF = "--- a/calc.py\n+++ b/calc.py\n@@ -1,3 +1,3 @@\n def mean(nums):\n-    return sum(nums) / (len(nums) + 1)\n+    return sum(nums) / len(nums)\n"


def _section(ui: TerminalUI, title: str) -> None:
    ui.chrome.print()
    ui.chrome.print(Rule(title, style="dim", align="left"))


def _status_samples(ui: TerminalUI) -> None:
    clock = [100.0]
    act = Activity(clock=lambda: clock[0])
    act.begin_turn()
    clock[0] += 2
    act.set_phase("think", "memahami permintaan Anda")
    ui.chrome.print(act.render())
    clock[0] += 3
    act.set_phase("write")
    act.add_text("Saya sudah menemukan penyebab bug di fungsi mean dan akan memperbaikinya sekarang.")
    ui.chrome.print(act.render())
    act.tool_started("run_command", {"command": "python -m unittest discover -s tests"})
    clock[0] += 12
    ui.chrome.print(act.render())
    act.sub_step("explore", "Membaca calc.py")
    act.sub_step("explore", "Mencari teks mean")
    ui.chrome.print(act.render())
    act.set_phase("compact", cancellable=False)
    ui.chrome.print(act.render())


def _request(kind: str) -> PermissionRequest:
    if kind == "edit":
        return PermissionRequest(
            "edit_file", "Edit file", "calc.py", _DIFF, "diff", "menulis file",
            impact="Mengubah isi file yang sudah ada. Bisa dibatalkan dengan /undo.", mode="default",
            intent="Saya akan memperbaiki pembagi di fungsi mean agar rata-ratanya benar.",
        )
    if kind == "command":
        return PermissionRequest(
            "run_command", "Jalankan perintah", "jalankan tes", "python -m unittest discover -s tests", "command",
            "menjalankan perintah shell", danger=True,
            impact="Perintah berjalan di komputer Anda (shell) pada folder kerja; hasilnya dikirim kembali ke agen.",
            mode="default", intent="Sekarang saya jalankan tes untuk memastikan perbaikannya berhasil.",
            session_label="Ya, jangan tanya lagi untuk `python -m unittest *` (sesi ini)",
        )
    if kind == "web":
        return PermissionRequest(
            "web_fetch", "Ambil halaman web", "https://docs.python.org/3/library/statistics.html", "ringkas fungsi mean",
            "text", "mengakses internet", impact="Mengunduh halaman dari internet; isinya diringkas model lalu dikirim ke agen.",
            mode="default",
        )
    return PermissionRequest(
        "mcp", "Jalankan server MCP dari .mcp.json proyek?", "github", "npx -y @modelcontextprotocol/server-github", "command",
        "server dari repo ini akan berjalan di komputer Anda", danger=True,
        impact="Program server itu dijalankan di komputer Anda dan tool-nya bisa dipakai agen. Setujui hanya bila Anda mempercayai repo ini.",
        mode="default",
    )


def render_all(ui: TerminalUI) -> None:
    """Cetak setiap komponen UI dengan data contoh ke ``ui.chrome``."""
    out = ui.chrome
    settings = Settings()
    settings.warnings = ["Kunci 'hooks' di settings proyek diabaikan (tidak dipercaya)."]
    _section(ui, "1. Banner awal")
    ui.banner(
        settings, Path("D:/Workspace Agung/MABA"), "default", "20261018-101530-ab12", False,
        notes=[("info", "Belum ada MABA.md di proyek ini: ketik /init agar agen memahami proyek Anda."),
               ("warn", "ripgrep (rg) tidak ditemukan: pencarian teks memakai mode Python yang lebih lambat.")],
    )

    _section(ui, "2. Baris status (spinner) di berbagai tahap")
    _status_samples(ui)

    _section(ui, "3. Pesan berlevel")
    for message, style in (
        ("Menyertakan: calc.py, tests/test_calc.py", "dim"),
        ("Model agen utama diganti ke global.anthropic.claude-sonnet-5-5.", "green"),
        ("Hook Stop meminta agen melanjutkan: jalankan tes dulu.", "cyan"),
        ("Agen sudah memakai 40 dari 50 putaran model untuk perintah ini; batasnya hampir tercapai.", "yellow"),
        ("Perintah tidak dikenal: /foo. Ketik /help.", "red"),
        ("Konteks mendekati 75%: percakapan lama diringkas otomatis…", "yellow"),
        ("Percakapan diringkas otomatis: 24 → 9 pesan.", "green"),
    ):
        ui.notice(message, style)

    _section(ui, "4. Langkah agen (satu giliran lengkap)")
    ui.begin_turn()
    for tid, name, inp, note in (
        ("1", "glob_files", {"pattern": "**/calc.py"}, ToolNote("1 file")),
        ("2", "read_file", {"path": "calc.py"}, ToolNote("8 baris (dari 8)")),
    ):
        ui.tool_started(name, inp)
        ui.tool_finished(name, inp, True, "", 0.2, note)
    ui.tool_started("edit_file", {"path": "calc.py"})
    ui.tool_finished("edit_file", {"path": "calc.py"}, True, "", 0.1, ToolNote("+1 −1", _DIFF, "diff"))
    ui.tool_started("run_command", {"command": "python -m unittest"})
    ui.tool_finished("run_command", {"command": "python -m unittest"}, True, "", 4.2, ToolNote("exit 0", "Ran 8 tests in 0.02s\nOK", "output"))
    ui.tool_started("explore", {"task": "cari semua pemakai mean"})
    ui.subagent_step("explore", "Membaca calc.py")
    ui.subagent_step("explore", "Mencari teks mean")
    ui.tool_finished("explore", {"task": "x"}, True, "", 6.5, ToolNote("laporan 812 karakter"))
    ui.tool_denied("run_command", {"command": "rm -rf build"}, "ditolak pengguna")
    ui.show_todos([
        {"content": "Temukan penyebab bug", "status": "completed"},
        {"content": "Perbaiki pembagi", "status": "completed"},
        {"content": "Jalankan tes", "status": "in_progress"},
        {"content": "Tambah uji regresi", "status": "pending"},
    ])

    _section(ui, "5. Ringkasan akhir giliran")
    ui.turn_footer(1200, 340, 32, 14.2)
    ui.begin_turn()
    ui.turn_footer(840, 96, 3, 2.3)
    ui.begin_turn()
    ui.tool_started("read_file", {"path": "a.py"})
    ui.tool_finished("read_file", {"path": "a.py"}, True, "", 0.1, ToolNote("20 baris"))
    ui.turn_footer(5200, 410, 68, 31.0, outcome="cancelled")
    ui.begin_turn()
    ui.tool_started("read_file", {"path": "a.py"})
    ui.tool_finished("read_file", {"path": "a.py"}, True, "", 0.1, ToolNote("20 baris"))
    ui.turn_footer(90000, 4100, 88, 120.0, outcome="limit", limit_turns=50)

    _section(ui, "6. Dialog izin: edit file (Enter = Ya)")
    out.print(ui._permission_panel(_request("edit")))
    _section(ui, "7. Dialog izin: perintah shell (BERBAHAYA, Enter tidak menyetujui)")
    out.print(ui._permission_panel(_request("command")))
    _section(ui, "8. Dialog izin: internet")
    out.print(ui._permission_panel(_request("web")))
    _section(ui, "9. Dialog izin: server MCP proyek")
    out.print(ui._permission_panel(_request("mcp")))

    _section(ui, "10. Panel error")
    ui.error(
        "Kredensial AWS kosong atau kedaluwarsa",
        "Jika memakai SSO: `aws sso login --profile agung`; jika tidak, perbarui kredensial profile tersebut.",
        session=True,
    )


def render_toolbar(ui: TerminalUI) -> None:
    """Cetak teks toolbar bawah (memakai ChatApp.toolbar_fragments dengan objek tiruan)."""
    from maba_code.session import ChatApp

    stub = SimpleNamespace(
        policy=SimpleNamespace(mode="acceptEdits"), ui=ui, settings=SimpleNamespace(model="global.openai.gpt-6-luna"),
        last_ctx_pct=68, total_in=12400, total_out=3100, ws=SimpleNamespace(todos=[{"status": "completed"}, {"status": "pending"}]),
        background=SimpleNamespace(running=lambda: [1]), _flash=None,
    )
    _section(ui, "11. Toolbar bawah (idle · menu / terbuka · menu @ terbuka)")
    for menu in (None, "slash", "file"):
        ui.chrome.print("".join(text for _, text in ChatApp.toolbar_fragments(stub, menu)), highlight=False, markup=False)  # type: ignore[arg-type]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m maba_code.ui.preview", description=__doc__.splitlines()[0])
    parser.add_argument("--width", type=int, help="lebar terminal (default: lebar saat ini)")
    parser.add_argument("--ascii", action="store_true", help="pakai simbol ASCII, bukan Unicode")
    parser.add_argument("--svg", metavar="FILE", help="simpan hasilnya sebagai SVG")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    console = Console(width=args.width, record=bool(args.svg), highlight=False)
    ui = TerminalUI(console, symbols=ASCII if args.ascii else UNICODE)
    render_all(ui)
    render_toolbar(ui)
    if args.svg:
        console.save_svg(args.svg, title="MABA Code — pratinjau UI")
        console.print(f"\nSVG disimpan: {args.svg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
