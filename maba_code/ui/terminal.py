"""Antarmuka terminal MABA Code (implementasi Frontend untuk PermissionHook)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

from rich.columns import Columns
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule as RichRule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from maba_code import __version__
from maba_code.config import AUTO_COMPACT_PCT, MODE_LABELS, Settings
from maba_code.permissions import PermissionAnswer, PermissionRequest
from maba_code.shell import IS_WINDOWS
from maba_code.tools import tool_class
from maba_code.ui.activity import Activity, extract_intent
from maba_code.ui.markdown_stream import BlockMarkdownStream
from maba_code.ui.theme import (
    ACCENT, CLASS_BADGE, CTX_WARN_PCT, MODE_SHORT, MODE_STYLE, MODE_TAG, NOTICE_LEVELS, TOOL_LABELS, Symbols,
    describe_tool, fmt_duration, meter, notice_level, pick_symbols,
)
from maba_code.workspace import ToolNote

ChooseFn = Callable[[dict[str, str], str], Awaitable[str]]
AskTextFn = Callable[[str], Awaitable[str]]
MAX_SUMMARY_FILES = 3  # file yang disebut satu per satu di ringkasan giliran
WAITED_SHOWN_SECONDS = 5  # waktu menunggu pengguna baru disebut di ringkasan bila selama ini


def _first_line(text: str, limit: int) -> str:
    for line in text.splitlines():
        if line.strip():
            line = line.strip()
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


def diff_renderable(diff: str, max_lines: int = 40) -> Text:
    """Diff unified berwarna (tanpa header ---/+++), dipotong agar tidak membanjiri layar."""
    lines = [ln for ln in diff.splitlines() if not ln.startswith(("--- ", "+++ "))]
    out = Text()
    for ln in lines[:max_lines]:
        style = "green" if ln.startswith("+") else "red" if ln.startswith("-") else "cyan" if ln.startswith("@@") else "dim"
        out.append(ln[:220] + "\n", style=style)
    if len(lines) > max_lines:
        out.append(f"… {len(lines) - max_lines} baris lagi\n", style="dim italic")
    if not lines:
        out.append("(tidak ada perubahan)\n", style="dim")
    out.rstrip()
    return out


def _subagent_name(tool: str, inp: dict[str, Any]) -> str | None:
    """Nama sub-agen yang dijalankan sebuah tool (None bila tool itu bukan pembuat sub-agen)."""
    if tool == "explore":
        return "explore"
    if tool == "task":
        return str(inp.get("subagent_type") or "") or None
    return None


def _flow_lines(prefix: str, items: list[str], width: int, sep: str) -> list[Text]:
    """Susun ``items`` berurutan dalam baris selebar ``width``; baris baru selalu dimulai di batas item."""
    indent = " " * len(prefix)
    lines: list[Text] = []
    line, used, empty = Text(prefix, style="dim"), len(prefix), True
    for item in items:
        gap = "" if empty else sep
        if not empty and used + len(gap) + len(item) > width:
            lines.append(line)
            line, used, empty, gap = Text(indent), len(indent), True, ""
        if gap:
            line.append(gap, style="dim")
            used += len(gap)
        line.append(item, style="italic")
        used += len(item)
        empty = False
    lines.append(line)
    return lines


class _StatusLine:
    """Renderable yang dihitung ulang tiap refresh spinner, sehingga waktu berjalan ikut bergerak."""

    def __init__(self, ui: TerminalUI):
        self.ui = ui

    def __rich_console__(self, console: Console, options: Any):
        yield self.ui.activity.render()


class TerminalUI:
    def __init__(
        self,
        console: Console | None = None,
        chrome: Console | None = None,
        *,
        interactive: bool = True,
        render_markdown: bool = True,
        symbols: Symbols | None = None,
        choose: ChooseFn | None = None,
        ask_text: AskTextFn | None = None,
    ):
        self.console = console or Console()
        self.chrome = chrome or self.console  # print mode: teks ke stdout, 'chrome' ke stderr
        self.sym = symbols or pick_symbols()
        self.interactive = interactive
        self._md = BlockMarkdownStream(self.console, self.sym, render=render_markdown)
        self._status: Any = None
        self.activity = Activity()
        self._recent_text = ""  # teks agen sejak giliran/hasil tool terakhir (untuk kutipan di dialog izin)
        self._text_stale = False
        self.pt_input: Any = None  # hanya untuk pengujian (pipe input prompt_toolkit)
        self.pt_output: Any = None
        self._choose: ChooseFn = choose or self._pt_choose
        self._ask_text: AskTextFn = ask_text or self._pt_ask_text
        self.watcher: Any = None  # EscWatcher, diisi oleh sesi
        self.on_state_change: Callable[[], None] | None = None
        self._inflight = 0
        self._overlap = False

    # ------------------------------------------------------------- streaming
    def text_delta(self, text: str) -> None:
        if self._text_stale:  # teks baru setelah hasil tool = pesan agen yang baru
            self._recent_text, self._text_stale = "", False
        self._recent_text = (self._recent_text + text)[-2000:]
        self.activity.add_text(text)
        self._md.feed(text)

    def text_end(self) -> None:
        self._md.flush()

    # ---------------------------------------------------------------- status
    def _status_visible(self) -> bool:
        return self.interactive and self.chrome.is_terminal

    def _ensure_status(self) -> None:
        if not self._status_visible() or self._status is not None:
            return
        self._status = self.chrome.status(_StatusLine(self), spinner="dots")
        self._status.start()

    def set_phase(self, phase: str, detail: str = "", cancellable: bool = True) -> None:
        """Tahap kerja agen (lihat theme.PHASES); baris status diperbarui otomatis."""
        self.activity.set_phase(phase, detail, cancellable)
        self._ensure_status()

    def set_status(self, message: str | None, cancellable: bool = True) -> None:
        """Teks status bebas (dipakai /compact, MCP, jeda /loop). None = hapus."""
        if message is None:
            self.clear_status()
            return
        self.set_phase("custom", message, cancellable)

    def clear_status(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    def begin_turn(self) -> None:
        self.activity.begin_turn()
        self._recent_text, self._text_stale = "", False

    def end_turn(self) -> None:
        self.activity.end_turn()

    @contextmanager
    def _modal(self):
        """Hentikan spinner & pengamat tombol selama dialog interaktif (waktu tunggu dicatat terpisah)."""
        had_status = self._status is not None
        self.clear_status()
        self.text_end()
        self.activity.begin_wait()
        try:
            with (self.watcher.paused() if self.watcher is not None else nullcontext()):
                yield
        finally:
            self.activity.end_wait()
        if had_status:
            self._ensure_status()

    # ----------------------------------------------------------------- tools
    def tool_started(self, name: str, inp: dict[str, Any]) -> None:
        self.text_end()
        self._inflight += 1
        if self._inflight > 1:
            self._overlap = True
        label = TOOL_LABELS.get(name, name)
        line = Text.assemble((f"{self.sym.bullet} ", ACCENT), (label, "bold"), ("  ", ""), (describe_tool(name, inp), ""))
        self.chrome.print(line)
        self.activity.tool_started(name, inp, running=self._inflight)
        self._ensure_status()

    def tool_denied(self, name: str, inp: dict[str, Any], reason: str) -> None:
        self.text_end()
        self._text_stale = True
        self.activity.tool_denied()
        label = TOOL_LABELS.get(name, name)
        target = describe_tool(name, inp, 70)
        self.chrome.print(
            Text.assemble(
                (f"{self.sym.fail} ", "red"), (f"{label} ditolak", "red"),
                (f"  {target}" if target else "", "dim"), (f" — {_first_line(reason, 110)}" if reason else "", "dim"),
            )
        )
        self._ensure_status()

    def tool_finished(
        self, name: str, inp: dict[str, Any], ok: bool, text: str, duration: float, note: ToolNote | None
    ) -> None:
        sym = self.sym
        self._inflight = max(0, self._inflight - 1)
        self._text_stale = True
        self.activity.tool_finished(name, inp, ok, note, running=self._inflight)
        prefix = ""
        if self._overlap:
            prefix = f"{TOOL_LABELS.get(name, name)} {describe_tool(name, inp, 40)} {sym.sep} "
        if self._inflight == 0:
            self._overlap = False
        timing = f" {sym.sep} {duration:.1f}s" if duration >= 0.1 else ""
        agent = _subagent_name(name, inp)
        sub_steps = self.activity.take_sub_steps(agent) if agent else 0
        sub = f" {sym.sep} {sub_steps} langkah" if sub_steps else ""
        if ok:
            summary = (note.summary if note and note.summary else _first_line(text, 90)) or "selesai"
            self.chrome.print(
                Text.assemble((f"  {sym.branch} ", "dim"), (f"{sym.ok} ", "green"), (prefix, "dim"), (summary, "dim"), (sub, "dim"), (timing, "dim"))
            )
            self._print_detail(note)
        else:
            # Untuk perintah shell, ringkasan "exit 1" lebih informatif daripada baris pertama outputnya.
            reason = (note.summary if note and note.summary else _first_line(text, 150)) or "gagal"
            self.chrome.print(
                Text.assemble((f"  {sym.branch} ", "dim"), (f"{sym.fail} ", "red"), (prefix, "dim"), (reason, "red"), (sub, "dim"), (timing, "dim"))
            )
            self._print_detail(note, only_output=True)
        self._ensure_status()

    # ------------------------------------------------------------ sub-agen
    def subagent_step(self, agent: str, text: str) -> None:
        """Satu langkah sub-agen: tampil di baris status (tidak dicetak permanen; ringkasannya ikut baris hasil)."""
        self.activity.sub_step(agent, text)
        self._ensure_status()

    def subagent_tool_finished(self, name: str, inp: dict[str, Any], ok: bool, note: ToolNote | None) -> None:
        self.activity.sub_tool_finished(name, inp, ok, note)

    def _print_detail(self, note: ToolNote | None, only_output: bool = False) -> None:
        if not note or not note.detail:
            return
        if note.kind == "diff" and not only_output:
            body = diff_renderable(note.detail, max_lines=14)
            self.chrome.print(Group(*[Text("    ") + line for line in body.split("\n")]))
        elif note.kind == "output":
            lines = note.detail.splitlines()
            shown = [Text("    " + ln[:150], style="dim") for ln in lines[:8]]
            if len(lines) > 8:
                shown.append(Text(f"    … {len(lines) - 8} baris lagi", style="dim italic"))
            if shown:
                self.chrome.print(Group(*shown))

    def show_todos(self, todos: list[dict[str, str]]) -> None:
        self.text_end()
        s = self.sym
        done = sum(1 for item in todos if item.get("status") == "completed")
        pct = round(100 * done / len(todos)) if todos else 0
        self.chrome.print(
            Text.assemble(
                ("  Rencana kerja ", "bold"), (f"{done}/{len(todos)} selesai  ", "dim"), (meter(pct, 8, s), ACCENT)
            )
        )
        for item in todos:
            status = item.get("status")
            if status == "completed":
                self.chrome.print(Text(f"    {s.todo_done} {item['content']}", style="dim strike"))
            elif status == "in_progress":
                self.chrome.print(Text(f"    {s.todo_active} {item['content']}", style="bold yellow"))
            else:
                self.chrome.print(Text(f"    {s.todo_pending} {item['content']}"))

    # --------------------------------------------------------------- dialogs
    def _class_of(self, req: PermissionRequest) -> str:
        return "danger" if req.tool == "mcp" else tool_class(req.tool)  # "mcp" = persetujuan server MCP proyek

    def _badge(self, req: PermissionRequest) -> tuple[str, str, bool]:
        """(teks lencana, gaya, ketat). Ketat = Enter TIDAK boleh menyetujui (berbahaya/internet/MCP)."""
        text, style, strict = CLASS_BADGE.get(self._class_of(req), CLASS_BADGE["unknown"])
        if req.danger and not strict:
            text, style, strict = CLASS_BADGE["danger"]
        return text, style, strict

    @staticmethod
    def _yes_label(req: PermissionRequest) -> str:
        return {"diff": "Ya, terapkan perubahan", "command": "Ya, jalankan", "plan": "Ya, setujui"}.get(req.kind, "Ya, izinkan")

    @staticmethod
    def _key_line(key: str, style: str, label: str, explain: str = "") -> Text:
        line = Text.assemble((f"[{key}]", style), (f" {label}", ""))
        if explain:
            line.append(f" — {explain}", style="dim")
        return line

    def _permission_panel(self, req: PermissionRequest) -> Panel:
        sym = self.sym
        badge, badge_style, strict = self._badge(req)
        head = Text.assemble((req.title, "bold"), ("  ", ""), (req.target, ACCENT))
        if req.reason:
            head.append(f"  ({req.reason})", style="yellow")
        rows: list[Any] = [head]
        if req.intent:
            rows.append(Text.assemble(("Agen menjelaskan: ", "dim"), (f'"{req.intent}"', "italic")))
        rows.append(Text(""))
        if req.detail:
            if req.kind == "diff":
                body: Any = diff_renderable(req.detail, 40)
            elif req.kind == "command":
                body = Syntax(req.detail, "powershell" if IS_WINDOWS else "bash", theme="ansi_dark", word_wrap=True, background_color="default")
            elif req.kind == "plan":
                body = Markdown(req.detail)
            else:
                body = Text(req.detail)
            rows += [body, Text("")]
        if req.impact:
            rows.append(Text.assemble(("Dampak: ", "bold"), (req.impact, "")))
        if req.mode:
            tag = MODE_TAG.get(req.mode, "")
            rows.append(
                Text.assemble(
                    ("Mode aktif: ", "dim"), (MODE_SHORT.get(req.mode, req.mode), MODE_STYLE.get(req.mode, "")),
                    (f" {sym.sep} {tag}" if tag else "", "dim"),
                )
            )
        rows.append(Text(""))
        rows.append(self._key_line("y", "bold green", self._yes_label(req)))
        if req.session_label:
            rows.append(self._key_line("a", "bold cyan", req.session_label))
        rows.append(self._key_line("n", "bold red", "Tidak", "agen diberi tahu lalu mencari cara lain"))
        rows.append(self._key_line("e", "bold yellow", "Tidak, beri arahan", "Anda mengetik alasan/arahan untuk agen"))
        rows.append(
            Text(
                "Enter tidak menyetujui aksi ini: tekan y bila Anda yakin." if strict else "Enter = Ya.",
                style="red" if strict else "dim",
            )
        )
        title = Text.assemble((f"{sym.warn} Izin diperlukan ", "bold"), (f"{sym.sep} {badge}", badge_style))
        return Panel(Group(*rows), title=title, title_align="left", border_style="red" if strict else "yellow", padding=(0, 1))

    async def ask_permission(self, request: PermissionRequest) -> PermissionAnswer:
        if not self.interactive:
            return PermissionAnswer(
                "deny",
                "Non-interactive run: re-run with --mode acceptEdits or --mode bypass to allow this action.",
            )
        if not request.intent:
            request.intent = extract_intent(self._recent_text)
        strict = self._badge(request)[2]
        with self._modal():
            self.chrome.print(self._permission_panel(request))
            mapping = {"y": "allow", "n": "deny", "escape": "deny", "e": "comment"}
            if not strict:
                mapping["enter"] = "allow"
            if request.session_label:
                mapping["a"] = "allow_session"
            choice = await self._choose(mapping, "Pilih tombol › ")
            comment = ""
            if choice == "comment":
                comment = (await self._ask_text("Alasan / arahan untuk model: ")).strip()
                choice = "deny"
            if choice == "deny":
                self.chrome.print(Text(f"  {self.sym.fail} Ditolak" + (f": {comment}" if comment else "") + " — agen diberi tahu", style="red"))
            elif choice == "allow_session":
                self.chrome.print(Text(f"  {self.sym.ok} Diizinkan untuk sesi ini — agen melanjutkan", style="green"))
            else:
                self.chrome.print(Text(f"  {self.sym.ok} Diizinkan — agen melanjutkan", style="green"))
        return PermissionAnswer(choice, comment)

    async def ask_plan(self, plan: str) -> str:
        if not self.interactive:
            return "keep"
        with self._modal():
            opts = [
                self._key_line("1", "bold green", "Ya, jalankan & terima edit otomatis", "agen mulai bekerja; edit file tanpa bertanya"),
                self._key_line("2", "bold cyan", "Ya, jalankan & tanya tiap edit", "agen mulai bekerja; Anda menyetujui tiap edit"),
                self._key_line("3", "bold red", "Tidak, lanjut merencanakan", "tetap mode rencana; beri masukan agar agen merevisi"),
            ]
            self.chrome.print(
                Panel(
                    Group(Markdown(plan), Text(""), Text("Apakah rencana ini boleh dijalankan?", style="bold"), *opts),
                    title="Rencana dari agen", title_align="left", border_style=ACCENT, padding=(0, 1),
                )
            )
            choice = await self._choose({"1": "acceptEdits", "2": "default", "3": "keep", "n": "keep", "escape": "keep"}, "Pilih 1/2/3 › ")
            self.chrome.print(
                Text(f"  {self.sym.ok} Rencana disetujui ({choice})" if choice != "keep" else f"  {self.sym.fail} Lanjut merencanakan",
                     style="green" if choice != "keep" else "yellow")
            )
        return choice

    # ------------------------------------------------- default input widgets
    async def _pt_choose(self, mapping: dict[str, str], message: str) -> str:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.key_binding import KeyBindings

        kb = KeyBindings()
        for key, value in mapping.items():

            @kb.add(key, eager=True)
            def _pick(event, value=value):
                event.app.exit(result=value)

        @kb.add("<any>")
        def _ignore(event):
            pass

        @kb.add("c-c", eager=True)
        def _cancel(event):
            event.app.exit(result=mapping.get("escape", "deny"))

        session = PromptSession(message, key_bindings=kb, erase_when_done=True, input=self.pt_input, output=self.pt_output)
        try:
            return await session.prompt_async()
        except (KeyboardInterrupt, EOFError):
            return mapping.get("escape", "deny")

    async def _pt_ask_text(self, message: str) -> str:
        from prompt_toolkit import PromptSession

        try:
            return await PromptSession(message, input=self.pt_input, output=self.pt_output).prompt_async()
        except (KeyboardInterrupt, EOFError):
            return ""

    # ----------------------------------------------------------------- misc
    def _notice_text(self, message: str, style: str = "dim") -> Text:
        """Satu baris pesan dengan ikon sesuai levelnya (info/sukses/peringatan/error); 'dim' tanpa ikon."""
        level = notice_level(style or "dim")
        if message.startswith("! "):  # awalan lama untuk peringatan; ikon level yang menggantikannya
            message, level = message[2:], "warn" if level in ("dim", "custom") else level
        if level == "custom":
            return Text(f"  {message}", style=style)
        icon, rich_style = NOTICE_LEVELS[level]
        if icon is None:
            return Text(f"  {message}", style=rich_style)
        body_style = rich_style if level in ("warn", "error") else ""
        return Text.assemble((f"  {getattr(self.sym, icon)} ", f"bold {rich_style}"), (message, body_style))

    def notice(self, message: str, style: str = "dim") -> None:
        self.text_end()
        self.chrome.print(self._notice_text(message, style))

    def mode_changed(self, mode: str) -> None:
        if self.on_state_change:
            self.on_state_change()

    def error(self, title: str, hint: str = "", session: bool = False) -> None:
        """Panel error. ``session=True``: error terjadi di tengah percakapan (riwayat tetap aman)."""
        self.clear_status()
        self.text_end()
        parts: list[Any] = [Text(hint)] if hint else []
        if session:
            if parts:
                parts.append(Text(""))
            parts.append(Text("Percakapan tetap tersimpan: ulangi perintah Anda, atau ketik /help.", style="dim"))
        body = Group(*parts) if parts else Text("")
        self.chrome.print(Panel(body, title=f"{self.sym.fail} {title}", title_align="left", border_style="red", padding=(0, 1)))

    def turn_footer(
        self, tokens_in: int, tokens_out: int, ctx_pct: int | None, seconds: float,
        outcome: str = "done", limit_turns: int | None = None,
    ) -> None:
        """Ringkasan akhir giliran: apa yang dikerjakan, apa yang berubah, dan apa yang bisa dilakukan berikutnya."""
        s = self.sym
        sep = f" {s.sep} "
        log = self.activity.log
        waited = self.activity.waited
        took = fmt_duration(max(0.0, seconds - waited))
        busy = bool(log.steps or log.changes or log.sub_total or log.denied)

        usage = [f"{_fmt_tokens(tokens_in)} masuk", f"{_fmt_tokens(tokens_out)} keluar"]
        ctx_style = "dim"
        ctx_note = ""
        if ctx_pct is not None:
            if ctx_pct >= CTX_WARN_PCT:
                ctx_note = f"{s.sep} /compact untuk meringkas sekarang"
                ctx_style = "yellow"
            else:
                ctx_note = f"(diringkas otomatis di {AUTO_COMPACT_PCT}%)"
            usage.append(f"konteks {ctx_pct}%")

        if outcome == "cancelled":
            icon, style, title = s.warn, "yellow", f"Dihentikan setelah {took}"
        elif outcome == "limit":
            icon, style, title = s.warn, "yellow", f"Batas {limit_turns} giliran per perintah tercapai setelah {took}"
        else:
            icon, style, title = s.ok, "green", f"Selesai dalam {took}"
        head = [title]
        if log.steps:
            head.append(f"{log.steps} langkah")
        if waited >= WAITED_SHOWN_SECONDS:
            head.append(f"menunggu Anda {fmt_duration(waited)}")
        if outcome == "done" and not busy:
            line = sep.join(head + usage) + (f" {s.sep} /compact untuk meringkas sekarang" if ctx_style == "yellow" else "")
            self.chrome.print(Text.assemble((f"  {icon} ", style), (line, "yellow" if ctx_style == "yellow" else "dim")))
            return

        self.chrome.print(Text.assemble((f"  {icon} ", style), (sep.join(head), f"bold {style}")))
        if log.changes:
            files = list(log.changes.items())
            shown = ", ".join(f"{path} (+{a} {s.minus}{r})" for path, (a, r) in files[:MAX_SUMMARY_FILES])
            more = f" dan {len(files) - MAX_SUMMARY_FILES} file lain" if len(files) > MAX_SUMMARY_FILES else ""
            self.chrome.print(Text.assemble(("    Diubah: ", "bold"), (shown + more, "")))
        extras = []
        if log.commands:
            extras.append(f"Dijalankan: {log.commands} perintah")
        if log.denied:
            extras.append(f"Ditolak: {log.denied} aksi")
        if log.sub_total:
            extras.append("Sub-agen: " + ", ".join(f"{name} {n} langkah" for name, n in log.sub_total.items()))
        if extras:
            self.chrome.print(Text("    " + sep.join(extras), style="dim"))
        self.chrome.print(Text("    " + sep.join(usage) + (f" {ctx_note}" if ctx_note else ""), style=ctx_style))
        nxt = []
        if log.changes:
            nxt.append("/undo membatalkan perubahan file")
        if outcome == "cancelled":
            nxt.append("ketik instruksi baru untuk melanjutkan")
        elif outcome == "limit":
            nxt.append("ketik 'lanjut' untuk meneruskan")
        if nxt:
            self.chrome.print(Text.assemble(("    Berikutnya: ", "bold"), ("; ".join(nxt), "")))

    def banner(
        self, settings: Settings, cwd: Path, mode: str, session_id: str, resumed: bool,
        notes: list[tuple[str, str]] | None = None,
    ) -> None:
        """``notes``: (level, teks) kesiapan lokal, mis. 'belum ada MABA.md'."""
        s = self.sym
        compact = self.chrome.width < 80
        info = Table.grid(padding=(0, 2))
        info.add_column(style="dim", no_wrap=True)
        info.add_column()
        info.add_row("model", Text(settings.model, style="bold"))
        info.add_row("profile", Text(settings.profile, style="bold"))
        info.add_row("folder", str(cwd))
        info.add_row("mode", Text(f"{mode} — {MODE_LABELS.get(mode, '')}", style=MODE_STYLE.get(mode, "")))
        info.add_row("sesi", f"{session_id}" + ("  (dilanjutkan)" if resumed else ""))
        if compact:
            title = Text.assemble((" MABA CODE ", f"bold reverse {ACCENT}"), (f"  v{__version__}", "dim"))
            top: Any = Group(title, Text(""), info)
        else:
            mark = Text("\n".join(s.wordmark), style=f"bold {ACCENT}")
            title = Text.assemble((" CODE ", f"bold reverse {ACCENT}"), (f"  v{__version__}", "dim"))
            header = Table.grid(padding=(0, 3))
            header.add_row(mark, Group(Text(""), title))
            top = Group(header, Text(""), info)
        self.chrome.print()
        self.chrome.print(Panel(top, border_style=ACCENT, padding=(0, 2), expand=False))
        for warning in settings.warnings:
            self.chrome.print(self._notice_text(warning, "warn"))
        for level, text in notes or []:
            self.chrome.print(self._notice_text(text, level))
        keys = [
            ("Enter", "kirim"), ("Alt+Enter", "baris baru"), ("/", "perintah"), ("@", "sertakan file"),
            ("!", "shell langsung"), ("Shift+Tab", "ganti mode"), ("Esc", "batalkan"), ("/help", "bantuan"),
        ]
        self.chrome.print(Columns([Text.assemble((k, "bold"), (f" {d}", "dim")) for k, d in keys], padding=(0, 3), expand=False))
        if not resumed:
            examples = ["jelaskan struktur proyek ini", "perbaiki bug di @app.py", "/plan tambahkan fitur login"]
            for line in _flow_lines("  Contoh: ", examples, self.chrome.width, f"  {s.sep}  "):
                self.chrome.print(line)
        self.chrome.print(RichRule(style="dim"))


def _fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def mode_label(mode: str) -> str:
    return MODE_SHORT.get(mode, mode)
