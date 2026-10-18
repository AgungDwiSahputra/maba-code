"""Perintah slash: bawaan (/help, /model, /mode, ...) dan kustom dari .maba/commands/*.md."""

from __future__ import annotations

import asyncio
import re
import shutil
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from rich.table import Table
from rich.text import Text

from maba_code import __version__
from maba_code.config import AUTO_COMPACT_PCT, CORE_MODEL, MODE_LABELS, MODEL_ALIASES, MODES, maba_home
from maba_code.memory import list_auto_memory, load_memory
from maba_code.models import make_session
from maba_code.prompts import INIT_PROMPT
from maba_code.tools import tool_class

if TYPE_CHECKING:
    from maba_code.session import ChatApp

Handler = Callable[["ChatApp", str], Awaitable[None]]


class ExitRequested(Exception):
    """Dilempar oleh /exit untuk keluar dari REPL."""


@dataclass
class Command:
    name: str
    help: str
    handler: Handler
    aliases: tuple[str, ...] = ()


@dataclass
class CustomCommand:
    name: str
    description: str
    body: str
    source: Path


def _ago(timestamp: float) -> str:
    delta = max(0, time.time() - timestamp)
    for limit, unit, div in ((60, "dtk", 1), (3600, "mnt", 60), (86400, "jam", 3600)):
        if delta < limit:
            return f"{int(delta / div)} {unit} lalu"
    return f"{int(delta / 86400)} hari lalu"


def _table(*columns: str) -> Table:
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2), pad_edge=False)
    for col in columns:
        table.add_column(col)
    return table


# ------------------------------------------------------------------ handlers
# Pengelompokan /help. Perintah bawaan yang belum terdaftar di sini otomatis masuk "Lainnya".
HELP_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Percakapan", ("clear", "resume", "compact", "undo", "todo", "exit")),
    ("Model & izin", ("model", "mode", "plan", "permissions", "tools")),
    ("Proyek & ekstensi", ("init", "memory", "agents", "skills", "mcp", "tasks", "loop")),
    ("Info & diagnosis", ("status", "cost", "config", "doctor", "help")),
)


async def cmd_help(app: ChatApp, args: str) -> None:
    out = app.ui.chrome
    out.print("[bold]Mulai cepat[/bold]")
    out.print("  Ketik tugas dalam bahasa biasa, mis. [italic]jelaskan struktur proyek ini[/italic] lalu tekan Enter.")
    out.print("  Agen akan menunjukkan tiap langkahnya dan [bold]bertanya dulu[/bold] sebelum mengubah file atau menjalankan perintah.")
    out.print("  Salah langkah? [cyan]/undo[/cyan] membatalkan perubahan file, [cyan]Esc[/cyan] menghentikan agen.")
    builtin = {cmd.name: cmd for cmd in app.commands.builtin_list()}
    placed: set[str] = set()
    groups = [(title, [builtin[n] for n in names if n in builtin]) for title, names in HELP_GROUPS]
    for _, cmds in groups:
        placed.update(cmd.name for cmd in cmds)
    rest = [cmd for name, cmd in builtin.items() if name not in placed]
    if rest:
        groups.append(("Lainnya", rest))
    for title, cmds in groups:
        out.print(f"\n[bold]{title}[/bold]")
        table = _table("Perintah", "Fungsi")
        table.show_header = False
        for cmd in cmds:
            alias = f" ({', '.join('/' + a for a in cmd.aliases)})" if cmd.aliases else ""
            table.add_row(Text(f"/{cmd.name}", style="cyan"), cmd.help + alias)
        out.print(table)
    extra = _table("Perintah", "Fungsi")
    extra.show_header = False
    for skill in app.commands.user_skills().values():
        extra.add_row(Text(f"/{skill.name}", style="magenta"), f"{skill.description[:80]} [dim](skill)[/dim]")
    for custom in app.commands.custom.values():
        extra.add_row(Text(f"/{custom.name}", style="magenta"), f"{custom.description} [dim](kustom)[/dim]")
    if extra.row_count:
        out.print("\n[bold]Skill & perintah kustom[/bold]")
        out.print(extra)
    out.print()
    out.print("[dim]Pintasan: Shift+Tab ganti mode · Alt+Enter baris baru · Esc batalkan giliran · Ctrl+L bersihkan layar · Ctrl+C dua kali keluar[/dim]")
    out.print("[dim]Awalan: @file sertakan file · !perintah jalankan shell langsung · baris diakhiri \\ lanjut ke baris berikutnya[/dim]")


async def cmd_status(app: ChatApp, args: str) -> None:
    """Ringkasan keadaan sesi sekarang: jawaban cepat untuk 'apa yang sedang terjadi?'."""
    from maba_code.ui.theme import MODE_HINT, MODE_STYLE, meter

    sym = app.ui.sym
    out = app.ui.chrome
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", no_wrap=True)
    grid.add_column()
    mode = app.policy.mode
    grid.add_row("keadaan", Text(f"{sym.ok} menunggu perintah Anda (agen tidak sedang bekerja)", style="green"))
    grid.add_row("mode", Text(MODE_HINT.get(mode, mode), style=MODE_STYLE.get(mode, "")))
    grid.add_row("model", f"{app.settings.model}  [dim](sub-agen & kompaksi: {CORE_MODEL})[/dim]")
    grid.add_row("folder", str(app.ws.cwd))
    grid.add_row("sesi", f"{app.session_id}  [dim]({len(app.agent.messages)} pesan)[/dim]")
    pct = app.last_ctx_pct
    if pct is None:
        grid.add_row("konteks", "[dim]belum terukur (muncul setelah giliran pertama)[/dim]")
    else:
        grid.add_row("konteks", f"{meter(pct, 10, sym)} {pct}%  [dim](diringkas otomatis di {AUTO_COMPACT_PCT}%; /compact untuk sekarang)[/dim]")
    grid.add_row("token sesi ini", f"{app.total_in:,} masuk · {app.total_out:,} keluar")
    todos = app.ws.todos
    if todos:
        done = sum(1 for t in todos if t.get("status") == "completed")
        active = next((t["content"] for t in todos if t.get("status") == "in_progress"), "")
        grid.add_row("todo", f"{done}/{len(todos)} selesai" + (f"  [dim]· sedang: {active}[/dim]" if active else ""))
    else:
        grid.add_row("todo", "[dim]belum ada[/dim]")
    changed = app.ws.last_changed()
    grid.add_row("file diubah", (", ".join(changed[:5]) + "  [dim]· /undo membatalkan[/dim]") if changed else "[dim]tidak ada yang bisa di-undo[/dim]")
    running = app.background.running()
    grid.add_row("task latar", f"{len(running)} berjalan  [dim](/tasks)[/dim]" if running else "[dim]tidak ada[/dim]")
    if app.mcp.configs:
        ok = sum(1 for s in app.mcp.states.values() if s.status == "terhubung")
        grid.add_row("server MCP", f"{ok}/{len(app.mcp.configs)} terhubung  [dim](/mcp)[/dim]")
    out.print(grid)


async def cmd_exit(app: ChatApp, args: str) -> None:
    raise ExitRequested


async def cmd_clear(app: ChatApp, args: str) -> None:
    sid = app.new_session()
    app.ui.notice(f"Percakapan baru dimulai (sesi {sid}). Sesi lama tetap tersimpan: /resume.", "green")
    await app.session_start("clear")


async def cmd_model(app: ChatApp, args: str) -> None:
    out = app.ui.chrome
    if args.strip():
        model = app.set_model(args.strip())
        app.ui.notice(f"Model agen utama diganti ke {model}. Jalankan /doctor untuk menguji koneksinya.", "green")
        if model != CORE_MODEL:
            app.ui.notice(f"Sub-agen, skill fork, web_fetch & kompaksi tetap memakai model inti {CORE_MODEL}.", "dim")
        return
    table = _table("Alias", "Model ID (Bedrock)")
    for alias, model_id in MODEL_ALIASES.items():
        marker = "[green]●[/green] " if model_id == app.settings.model else "  "
        table.add_row(f"{marker}{alias}", model_id)
    out.print(f"Model agen utama: [bold]{app.settings.model}[/bold]  (profile: {app.settings.profile})")
    out.print(f"Model inti (sub-agen, skill fork, web_fetch, kompaksi): [bold]{CORE_MODEL}[/bold] — tidak berubah oleh /model")
    out.print(table)
    out.print("[dim]Pakai: /model <alias|model-id>. ID polos tanpa 'global.' sering ditolak Bedrock.[/dim]")


async def cmd_mode(app: ChatApp, args: str) -> None:
    wanted = args.strip()
    if not wanted:
        for mode in MODES:
            marker = "●" if mode == app.policy.mode else " "
            app.ui.chrome.print(f" {marker} [bold]{mode:<12}[/bold] {MODE_LABELS[mode]}")
        app.ui.chrome.print("[dim]Pakai: /mode <nama> atau tekan Shift+Tab.[/dim]")
        return
    if wanted not in MODES:
        app.ui.notice(f"Mode tidak dikenal: {wanted}. Pilihan: {', '.join(MODES)}", "red")
        return
    app.set_mode(wanted)
    app.ui.notice(f"Mode: {wanted} — {MODE_LABELS[wanted]}", "green")


async def cmd_plan(app: ChatApp, args: str) -> None:
    app.set_mode("plan")
    app.ui.notice("Mode rencana aktif: agen hanya membaca lalu mengajukan rencana untuk disetujui.", "green")
    if args.strip():
        await app.run_prompt(args.strip())


async def cmd_compact(app: ChatApp, args: str) -> None:
    app.ui.set_status("Meringkas percakapan…", cancellable=False)
    try:
        before, after = await app.compact(args.strip())
    finally:
        app.ui.clear_status()
    app.ui.notice(f"Percakapan diringkas: {before} → {after} pesan.", "green")


async def cmd_resume(app: ChatApp, args: str) -> None:
    sessions = app.list_sessions()
    if not sessions:
        app.ui.notice("Belum ada sesi tersimpan untuk folder ini.", "yellow")
        return
    wanted = args.strip()
    if not wanted:
        table = _table("#", "ID", "Judul", "Pesan", "Terakhir")
        for i, meta in enumerate(sessions, 1):
            current = "●" if meta["id"] == app.session_id else " "
            table.add_row(f"{current}{i}", meta["id"], str(meta.get("title", ""))[:48], str(meta.get("messages", 0)), _ago(meta.get("updated", 0)))
        app.ui.chrome.print(table)
        app.ui.chrome.print("[dim]Pakai: /resume <nomor|id>[/dim]")
        return
    target = None
    if wanted.isdigit() and 1 <= int(wanted) <= len(sessions):
        target = sessions[int(wanted) - 1]["id"]
    else:
        target = next((m["id"] for m in sessions if m["id"].startswith(wanted)), None)
    if target is None:
        app.ui.notice(f"Sesi tidak ditemukan: {wanted}", "red")
        return
    count = app.resume_session(target)
    app.ui.notice(f"Sesi {target} dilanjutkan ({count} pesan dimuat).", "green")
    await app.session_start("resume")


async def cmd_todo(app: ChatApp, args: str) -> None:
    if not app.ws.todos:
        app.ui.notice("Belum ada daftar todo.", "dim")
        return
    app.ui.show_todos(app.ws.todos)


async def cmd_undo(app: ChatApp, args: str) -> None:
    report = app.ws.undo_last_turn()
    if not report:
        app.ui.notice("Tidak ada perubahan file dari agen yang bisa dibatalkan.", "yellow")
        return
    for line in report:
        app.ui.notice(line, "green")
    app.ui.notice("Catatan: perubahan lewat run_command tidak dilacak.", "dim")


async def cmd_cost(app: ChatApp, args: str) -> None:
    table = _table("Metrik", "Nilai")
    table.add_row("Token masuk (sesi proses ini)", f"{app.total_in:,}")
    table.add_row("Token keluar", f"{app.total_out:,}")
    table.add_row("Sub-agen (sudah termasuk di atas)", f"{app.ws.sub_usage['in']:,} masuk / {app.ws.sub_usage['out']:,} keluar")
    table.add_row(
        "Konteks terpakai",
        f"{app.last_ctx_pct}% dari {app.settings.context_window:,} (diringkas otomatis di {AUTO_COMPACT_PCT}%)"
        if app.last_ctx_pct is not None else "-",
    )
    app.ui.chrome.print(table)
    app.ui.chrome.print("[dim]Biaya dalam dolar tidak dihitung: tarif model di akun Anda tidak diketahui MABA Code.[/dim]")


async def cmd_init(app: ChatApp, args: str) -> None:
    await app.run_prompt(INIT_PROMPT)


_CLASS_LABELS = {
    "read": "baca (otomatis)",
    "write": "tulis (ditanya)",
    "danger": "berbahaya (ditanya)",
    "network": "internet (ditanya)",
    "mcp": "MCP (ditanya)",
    "plan": "rencana",
    "unknown": "tidak dikenal (ditanya)",
}


async def cmd_tools(app: ChatApp, args: str) -> None:
    table = _table("Tool", "Kelas izin")
    for tool in app.all_tools():
        name = getattr(tool, "tool_name", "?")
        table.add_row(name, _CLASS_LABELS[tool_class(name)])
    app.ui.chrome.print(table)


async def cmd_permissions(app: ChatApp, args: str) -> None:
    p = app.policy
    out = app.ui.chrome
    out.print(f"Mode: [bold]{p.mode}[/bold]")
    for label, rules in (("deny", p.deny), ("ask", p.ask), ("allow", p.allow), ("allow (sesi ini)", p.session_allow)):
        out.print(f"  {label:<17} " + (", ".join(str(r) for r in rules) if rules else "[dim](kosong)[/dim]"))
    out.print("[dim]Urutan evaluasi: deny → ask → allow. Aturan proyek hanya boleh menambah deny/ask; allow hanya dari ~/.maba/settings.json.[/dim]")


async def cmd_agents(app: ChatApp, args: str) -> None:
    app.reload_extensions()
    table = _table("Sub-agen", "Sumber", "Tool", "Deskripsi")
    for d in app.agent_defs.values():
        tools = "semua" if d.tools is None else ", ".join(d.tools) or "-"
        if d.disallowed:
            tools += f" (kecuali {', '.join(d.disallowed)})"
        table.add_row(Text(d.name, style="cyan"), d.source.split(":")[0], tools, d.description[:70])
    app.ui.chrome.print(table)
    app.ui.chrome.print(
        f"[dim]Tambah sendiri: ~/.maba/agents/*.md atau .maba/agents/*.md (frontmatter name, description, tools). "
        f"Semua sub-agen berjalan di {CORE_MODEL}.[/dim]"
    )


async def cmd_skills(app: ChatApp, args: str) -> None:
    app.reload_extensions()
    if not app.skills:
        app.ui.notice("Belum ada skill. Buat ~/.maba/skills/<nama>/SKILL.md atau .maba/skills/<nama>/SKILL.md.", "yellow")
        return
    table = _table("Skill", "Sumber", "Dipanggil oleh", "Deskripsi")
    for s in app.skills.values():
        who = " & ".join(w for w, ok in (("model", s.model_invocable), ("/" + s.name, s.user_invocable)) if ok) or "-"
        if s.fork:
            who += f" · fork→{s.agent}"
        table.add_row(Text(s.name, style="cyan"), s.source, who, s.description[:70])
    app.ui.chrome.print(table)


async def cmd_tasks(app: ChatApp, args: str) -> None:
    sub, _, rest = args.strip().partition(" ")
    if sub == "stop" and rest.strip():
        try:
            task = await asyncio.to_thread(app.background.stop, rest.strip())
        except KeyError:
            app.ui.notice(f"Task tidak ditemukan: {rest.strip()}", "red")
            return
        app.ui.notice(f"Task {task.id} {task.status()}.", "green")
        return
    tasks = list(app.background.tasks.values())
    if not tasks:
        app.ui.notice("Belum ada task latar belakang. Agen membuatnya lewat run_command(run_in_background=true).", "dim")
        return
    table = _table("ID", "Status", "Durasi", "Perintah", "Log")
    for t in tasks:
        table.add_row(Text(t.id, style="cyan"), t.status(), f"{t.runtime():.0f}s", t.command[:50], str(t.log_path))
    app.ui.chrome.print(table)
    app.ui.chrome.print("[dim]Hentikan: /tasks stop <id>. Semua task dimatikan saat MABA Code keluar.[/dim]")


async def cmd_mcp(app: ChatApp, args: str) -> None:
    if not app.mcp.configs:
        app.ui.notice("Belum ada server MCP. Tambahkan di ~/.maba/mcp.json atau .mcp.json proyek (format mcpServers).", "dim")
        return
    table = _table("Server", "Sumber", "Transport", "Status", "Tool")
    for cfg in app.mcp.configs:
        state = app.mcp.states.get(cfg.name)
        status = state.status if state else "belum dijalankan"
        tools = ", ".join(state.tools[:4]) + (" …" if state and len(state.tools) > 4 else "") if state and state.tools else "-"
        table.add_row(Text(cfg.name, style="cyan"), cfg.source, cfg.transport, status, tools)
    app.ui.chrome.print(table)
    app.ui.chrome.print(
        f"[dim]Tool MCP selalu ditanya; izinkan permanen di ~/.maba/settings.json, mis. \"mcp__github__*\". Log: {app.mcp.log_path}[/dim]"
    )


LOOP_DEFAULT_SECONDS = 600
LOOP_MIN_SECONDS = 10
LOOP_MAX_ITERATIONS = 20
_LOOP_INTERVAL = re.compile(r"^(\d+)([smh])$")


async def cmd_loop(app: ChatApp, args: str) -> None:
    first, _, rest = args.strip().partition(" ")
    match = _LOOP_INTERVAL.match(first.lower())
    interval, body = (LOOP_DEFAULT_SECONDS, args.strip())
    if match:
        interval = int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)]
        body = rest.strip()
    if not body:
        app.ui.notice("Pakai: /loop [10s|5m|1h] <prompt atau /perintah>   (default 10m, maks. 20 iterasi)", "yellow")
        return
    if body.lower().startswith("/loop"):
        app.ui.notice("/loop tidak bisa bersarang.", "red")
        return
    interval = max(interval, LOOP_MIN_SECONDS)

    async def once():
        if body.startswith("/"):
            await app.commands.dispatch(body)
            return None
        return await app.run_prompt(body)

    await app.run_loop(once, interval, LOOP_MAX_ITERATIONS, body)


async def cmd_memory(app: ChatApp, args: str) -> None:
    files = load_memory(app.ws.cwd)
    if not files:
        app.ui.notice("Tidak ada MABA.md yang dimuat. Buat dengan /init.", "yellow")
    for f in files:
        app.ui.chrome.print(f"  [cyan]{f.path}[/cyan] [dim]({len(f.text)} karakter)[/dim]")
    folder = app.ws.memory_dir
    if folder is None:
        app.ui.notice("Memori otomatis nonaktif (autoMemory: false di ~/.maba/settings.json).", "dim")
        return
    notes = list_auto_memory(folder)
    app.ui.chrome.print(f"\n  Memori otomatis: [cyan]{folder}[/cyan] [dim]({len(notes)} file; ditulis agen sendiri)[/dim]")
    for note in notes:
        app.ui.chrome.print(f"    {note.name}")


async def cmd_config(app: ChatApp, args: str) -> None:
    s = app.settings
    table = _table("Pengaturan", "Nilai")
    for key, value in (
        ("profile AWS", s.profile),
        ("region", s.region or "(dari profile)"),
        ("model agen utama", s.model),
        ("model inti (dikunci)", CORE_MODEL),
        ("mode", app.policy.mode),
        ("hooks", ", ".join(sorted(app.hooks.groups)) or "(tidak ada)"),
        ("server MCP", ", ".join(c.name for c in app.mcp.configs) or "(tidak ada)"),
        ("memori otomatis", str(app.ws.memory_dir) if app.ws.memory_dir else "nonaktif"),
        ("context window", f"{s.context_window:,} token (asumsi; ubah via settings 'contextWindow')"),
        ("max output token", f"{s.max_tokens:,}"),
        ("batas giliran/perintah", s.max_turns),
        ("folder kerja", app.ws.cwd),
        ("folder sesi", app.session_dir),
        ("sesi", app.session_id),
    ):
        table.add_row(key, str(value))
    app.ui.chrome.print(table)
    app.ui.chrome.print("[dim]Kredensial AWS tidak pernah ditampilkan atau disimpan oleh MABA Code.[/dim]")


async def run_doctor(app: ChatApp) -> bool:
    """Uji profile → kredensial → STS → model Bedrock. Tidak pernah mencetak kunci."""
    from botocore.exceptions import ProfileNotFound

    out = app.ui.chrome
    s = app.settings
    ok_all = True

    def line(ok: bool, label: str, detail: str = "") -> None:
        nonlocal ok_all
        ok_all = ok_all and ok
        mark = "[green]✓[/green]" if ok else "[red]✗[/red]"
        out.print(f"  {mark} {label}" + (f"  [dim]{detail}[/dim]" if detail else ""))

    out.print(f"[bold]MABA Code {__version__} — diagnosis[/bold]")
    line(True, f"Python {sys.version.split()[0]}")
    try:
        session = make_session(s)
        line(True, f"Profile AWS '{s.profile}' ditemukan", f"region {session.region_name or '?'}")
    except ProfileNotFound:
        line(False, f"Profile AWS '{s.profile}' tidak ditemukan", "cek: aws configure list-profiles")
        return False
    try:
        creds = await asyncio.to_thread(session.get_credentials)
        line(creds is not None, "Kredensial profile terbaca" if creds else "Kredensial kosong (tidak ditampilkan)")
        if creds is None:
            return False
        ident = await asyncio.to_thread(lambda: session.client("sts").get_caller_identity())
        kind = ident["Arn"].split(":")[-1].split("/")[0]
        line(True, "STS get-caller-identity berhasil", f"akun …{ident['Account'][-4:]} · tipe {kind}")
    except Exception as exc:  # noqa: BLE001
        line(False, "Gagal memverifikasi identitas AWS", f"{type(exc).__name__}: {str(exc)[:120]}")
        return False
    client = session.client("bedrock-runtime")
    # Model inti GPT-6 Luna selalu diuji: sub-agen/kompaksi/web_fetch memakainya walau agen utama diganti.
    for model_id in dict.fromkeys([s.model, CORE_MODEL]):
        label = f"Model {model_id}" + (" (inti)" if model_id == CORE_MODEL else "")
        try:
            started = time.monotonic()
            await asyncio.to_thread(
                lambda m=model_id: client.converse(
                    modelId=m,
                    messages=[{"role": "user", "content": [{"text": "ping"}]}],
                    inferenceConfig={"maxTokens": 32},
                )
            )
            line(True, f"{label} merespons", f"{time.monotonic() - started:.1f}s")
        except Exception as exc:  # noqa: BLE001
            line(False, f"{label} gagal dipanggil", f"{type(exc).__name__}: {str(exc)[:160]}")
    line(bool(shutil.which("rg")), "ripgrep (rg)" if shutil.which("rg") else "ripgrep tidak ada — grep memakai mode Python (lebih lambat)")
    line(True, "Folder sesi", str(app.session_dir))
    return ok_all


async def cmd_doctor(app: ChatApp, args: str) -> None:
    await run_doctor(app)


BUILTINS: tuple[Command, ...] = (
    Command("help", "Tampilkan bantuan", cmd_help, ("?",)),
    Command("clear", "Mulai percakapan baru", cmd_clear, ("new",)),
    Command("model", "Lihat/ganti model Bedrock", cmd_model),
    Command("mode", "Lihat/ganti mode izin", cmd_mode),
    Command("plan", "Masuk mode rencana (opsional: langsung beri tugas)", cmd_plan),
    Command("compact", "Ringkas percakapan (opsional: fokus ringkasan)", cmd_compact),
    Command("resume", "Daftar / lanjutkan sesi tersimpan", cmd_resume, ("sessions",)),
    Command("todo", "Tampilkan daftar todo agen", cmd_todo),
    Command("undo", "Batalkan perubahan file agen pada giliran terakhir", cmd_undo),
    Command("status", "Ringkasan keadaan sesi sekarang (mode, konteks, todo, file diubah)", cmd_status, ("st",)),
    Command("cost", "Pemakaian token & konteks", cmd_cost, ("usage",)),
    Command("init", "Buat MABA.md untuk proyek ini", cmd_init),
    Command("memory", "Tampilkan file MABA.md yang dimuat", cmd_memory),
    Command("agents", "Daftar sub-agen (bawaan & dari file)", cmd_agents),
    Command("skills", "Daftar skill (SKILL.md)", cmd_skills),
    Command("tasks", "Task latar belakang (/tasks stop <id>)", cmd_tasks, ("bashes",)),
    Command("loop", "Ulangi prompt/perintah tiap interval: /loop 5m <prompt>", cmd_loop),
    Command("mcp", "Status server MCP & tool-nya", cmd_mcp),
    Command("tools", "Daftar tool & kelas izinnya", cmd_tools),
    Command("permissions", "Tampilkan aturan izin aktif", cmd_permissions),
    Command("config", "Tampilkan konfigurasi aktif", cmd_config),
    Command("doctor", "Uji profile AWS, kredensial, dan model", cmd_doctor),
    Command("exit", "Keluar", cmd_exit, ("quit", "q")),
)

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


def load_custom_commands(cwd: Path) -> dict[str, CustomCommand]:
    found: dict[str, CustomCommand] = {}
    for folder in (maba_home() / "commands", cwd / ".maba" / "commands"):  # proyek menimpa pengguna
        if not folder.is_dir():
            continue
        for file in sorted(folder.glob("*.md")):
            try:
                raw = file.read_text(encoding="utf-8-sig")
            except OSError:
                continue
            description, body = "", raw
            match = _FRONTMATTER.match(raw)
            if match:
                body = match.group(2)
                for ln in match.group(1).splitlines():
                    if ln.lower().startswith("description:"):
                        description = ln.split(":", 1)[1].strip().strip("\"'")
            name = file.stem.lower()
            found[name] = CustomCommand(name, description or (body.strip().splitlines() or [""])[0][:60], body.strip(), file)
    return found


@dataclass
class CommandRegistry:
    app: ChatApp
    custom: dict[str, CustomCommand] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.custom = load_custom_commands(self.app.ws.cwd)
        self._lookup: dict[str, Command] = {}
        for cmd in BUILTINS:
            self._lookup[cmd.name] = cmd
            for alias in cmd.aliases:
                self._lookup[alias] = cmd
        self.app.commands = self
        self.app.command_names = self.names

    def builtin_list(self) -> tuple[Command, ...]:
        return BUILTINS

    def user_skills(self) -> dict:
        return {n: s for n, s in getattr(self.app, "skills", {}).items() if s.user_invocable and n not in self._lookup}

    def names(self) -> list[tuple[str, str]]:
        items = [(c.name, c.help) for c in BUILTINS]
        skills = self.user_skills()
        items += [(s.name, f"{s.description[:60]} (skill)") for s in skills.values()]
        items += [(c.name, c.description) for c in self.custom.values() if c.name not in self._lookup and c.name not in skills]
        return items

    async def dispatch(self, line: str) -> None:
        name, _, args = line[1:].partition(" ")
        name = name.lower().strip()
        args = args.strip()
        if name in self._lookup:
            await self._lookup[name].handler(self.app, args)
            return
        if name in self.user_skills():  # seperti Claude Code: skill mengungguli perintah kustom bernama sama
            await self.app.invoke_skill(name, args)
            return
        if name in self.custom:
            body = self.custom[name].body
            prompt = body.replace("$ARGUMENTS", args) if "$ARGUMENTS" in body else (f"{body}\n\n{args}" if args else body)
            await self.app.run_prompt(prompt)
            return
        self.app.ui.notice(f"Perintah tidak dikenal: /{name}. Ketik /help.", "red")
