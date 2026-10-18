"""Input interaktif: prompt_toolkit dengan autocomplete / dan @file, toolbar status, dan pintasan."""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import has_completions
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.completion import generate_completions
from prompt_toolkit.styles import Style

from maba_code import fsops
from maba_code.config import maba_home

if TYPE_CHECKING:
    from maba_code.session import ChatApp

# '@' hanya dihitung sebagai mention di awal teks atau setelah spasi (jadi alamat email a@b.com tidak memicu menu).
_AT_FRAGMENT = re.compile(r"(?:^|(?<=\s))@([^\s@]*)$")
_SLASH_WORD = re.compile(r"^/([^\s/]*)$")
_FILE_LIMIT = 8000
_DIR_LIMIT = 4000


class FileIndex:
    """Daftar file & folder proyek untuk autocomplete @file (git ls-files, cadangan: telusur folder)."""

    def __init__(self, root: Path, ttl: float = 20.0):
        self.root = root
        self.ttl = ttl
        self._files: list[str] = []
        self._entries: list[str] = []  # file + folder (folder diakhiri '/')
        self._loaded_at = 0.0

    def files(self) -> list[str]:
        if self._files and time.monotonic() - self._loaded_at < self.ttl:
            return self._files
        found: list[str] = []
        try:
            out = subprocess.run(
                ["git", "-C", str(self.root), "ls-files", "-co", "--exclude-standard"],
                capture_output=True, text=True, timeout=5, check=False, encoding="utf-8", errors="replace",
            )
            if out.returncode == 0:
                found = [ln.strip().replace("\\", "/") for ln in out.stdout.splitlines() if ln.strip()]
        except (OSError, subprocess.SubprocessError):
            found = []
        if not found:
            for path in fsops.iter_files(self.root):
                found.append(path.relative_to(self.root).as_posix())
                if len(found) >= _FILE_LIMIT:
                    break
        self._files = [f for f in found[:_FILE_LIMIT] if not fsops.is_sensitive_path(f)]
        folders: set[str] = set()
        for rel in self._files:
            parts = rel.split("/")[:-1]
            for depth in range(1, len(parts) + 1):
                folders.add("/".join(parts[:depth]) + "/")
            if len(folders) >= _DIR_LIMIT:
                break
        self._entries = sorted(folders) + self._files
        self._loaded_at = time.monotonic()
        return self._files

    def entries(self) -> list[str]:
        self.files()
        return self._entries

    def search(self, fragment: str, limit: int = 40) -> list[str]:
        """File & folder yang cocok: nama diawali fragmen > path diawali > path memuat fragmen."""
        frag = fragment.replace("\\", "/").lower()
        scored: list[tuple[int, int, str]] = []
        for rel in self.entries():
            low = rel.lower()
            name = low.rstrip("/").rsplit("/", 1)[-1]
            if not frag:
                score = 3
            elif name.startswith(frag):
                score = 0
            elif low.startswith(frag):
                score = 1
            elif frag in low:
                score = 2
            else:
                continue
            scored.append((score, len(rel), rel))
        scored.sort()
        return [rel for _, _, rel in scored[:limit]]


def _highlighted(text: str, start: int, length: int) -> FormattedText:
    """Teks menu dengan bagian yang cocok diberi gaya ``completion-match``."""
    if length <= 0 or start < 0:
        return FormattedText([("", text)])
    return FormattedText(
        [("", text[:start]), ("class:completion-match", text[start:start + length]), ("", text[start + length:])]
    )


def _match_span(rel: str, fragment: str) -> tuple[int, int]:
    frag = fragment.replace("\\", "/").lower()
    if not frag:
        return 0, 0
    low = rel.lower()
    trimmed = low.rstrip("/")
    name_start = len(trimmed) - len(trimmed.rsplit("/", 1)[-1])
    if low.startswith(frag, name_start):
        return name_start, len(frag)
    index = low.find(frag)
    return (index, len(frag)) if index >= 0 else (0, 0)


class MabaCompleter(Completer):
    """Menu otomatis untuk ``/perintah`` dan ``@file``/``@folder``, dengan bagian yang cocok di-highlight."""

    def __init__(self, commands: Callable[[], list[tuple[str, str]]], files: FileIndex):
        self._commands = commands
        self._files = files

    def get_completions(self, document: Document, complete_event) -> Iterable[Completion]:
        text = document.text_before_cursor
        slash = _SLASH_WORD.match(text)
        if slash:
            word = slash.group(1).lower()
            commands = self._commands()
            # Awalan cocok lebih dulu, lalu (mulai 2 huruf) yang memuat teks yang diketik, mis. "/ode" -> mode.
            ranked = [c for c in commands if c[0].startswith(word)]
            ranked += [c for c in commands if len(word) >= 2 and word in c[0] and not c[0].startswith(word)]
            for name, description in ranked:
                # Awalan: highlight "/" + teks yang diketik; selain itu: highlight potongan di tengah nama.
                start, length = (0, len(word) + 1) if name.startswith(word) else (1 + name.find(word), len(word))
                yield Completion(
                    name, start_position=-len(slash.group(1)),
                    display=_highlighted(f"/{name}", start, length), display_meta=description,
                )
            return
        match = _AT_FRAGMENT.search(text)
        if match:
            fragment = match.group(1)
            for rel in self._files.search(fragment):
                start, length = _match_span(rel, fragment)
                inserted = f'"{rel}"' if " " in rel else rel  # path berspasi dikutip, sesuai format @"..."
                yield Completion(
                    inserted, start_position=-len(fragment), display=_highlighted(rel, start, length),
                    display_meta="folder" if rel.endswith("/") else "",
                )


def completion_context(buf: Buffer) -> str | None:
    """'slash' saat mengetik nama perintah, 'file' saat mengetik @mention, selain itu None."""
    text = buf.document.text_before_cursor
    if _SLASH_WORD.match(text):
        return "slash"
    if _AT_FRAGMENT.search(text):
        return "file"
    return None


def _candidates(buf: Buffer, completer: Completer) -> tuple[list[Completion], Completion | None]:
    """Completion yang sedang tampil (atau dihitung langsung bila menu belum sempat muncul) + item terpilih."""
    state = buf.complete_state
    if state is not None and state.completions:
        return list(state.completions), state.current_completion
    return list(completer.get_completions(buf.document, CompleteEvent(completion_requested=True))), None


def accept_completion(buf: Buffer, completion: Completion, kind: str) -> None:
    buf.apply_completion(completion)
    if kind == "file" and not completion.text.rstrip('"').endswith("/"):
        buf.insert_text(" ")  # file selesai dipilih; folder dibiarkan agar menu isinya langsung muncul


PROMPT_STYLE = Style.from_dict(
    {
        "prompt": "bold ansicyan",
        "bottom-toolbar": "noreverse bg:#262626 #a8a8a8",
        "bottom-toolbar.text": "noreverse bg:#262626 #a8a8a8",
        "tb.mode.default": "bg:#444444 #e4e4e4",
        "tb.mode.acceptedits": "bg:#1f5f2f #e4e4e4",  # nama class prompt_toolkit wajib huruf kecil
        "tb.mode.plan": "bg:#1f4f8f #e4e4e4",
        "tb.mode.bypass": "bg:#8f1f1f #ffffff bold",
        "tb.tag": "bg:#262626 #8a8a8a italic",
        "tb.hint": "#7a7a7a",
        "tb.flash": "bg:#262626 #ffd75f bold",  # keterangan sesaat, mis. arti mode setelah Shift+Tab
        "tb.ctx.warn": "bg:#262626 #ffd75f",
        "tb.ctx.crit": "bg:#262626 #ff5f5f bold",
        "placeholder": "#6c6c6c italic",
        # Menu autocomplete / dan @: baris terpilih biru, bagian yang cocok cyan tebal.
        "completion-menu": "bg:#1c1c1c #d0d0d0",
        "completion-menu.completion": "bg:#1c1c1c #d0d0d0",
        "completion-menu.completion.current": "bg:#005f87 #ffffff",
        "completion-menu.meta.completion": "bg:#1c1c1c #8a8a8a",
        "completion-menu.meta.completion.current": "bg:#005f87 #e4e4e4",
        "completion-match": "bold #5fd7ff",
        "scrollbar.background": "bg:#303030",
        "scrollbar.button": "bg:#808080",
    }
)


def create_prompt_session(app: ChatApp, symbol: str = "❯", *, input=None, output=None) -> PromptSession:
    """``input``/``output`` hanya untuk pengujian (pipe input prompt_toolkit)."""
    kb = KeyBindings()
    completer = MabaCompleter(app.command_names, FileIndex(app.ws.cwd))

    @kb.add("enter")
    def _submit(event):
        buf = event.current_buffer
        if buf.text.endswith("\\"):  # lanjutkan baris dengan backslash + Enter
            buf.delete_before_cursor(1)
            buf.insert_text("\n")
            return
        kind = completion_context(buf)
        if kind == "slash":
            # Seperti Claude Code: Enter menjalankan perintah yang di-highlight (item terpilih, atau yang teratas).
            items, current = _candidates(buf, completer)
            if items:
                buf.apply_completion(current or items[0])
            elif buf.text == "/":
                buf.start_completion()
                return
        elif kind == "file":
            items, current = _candidates(buf, completer)
            typed = _AT_FRAGMENT.search(buf.document.text_before_cursor).group(1)
            if items and (current is not None or not any(c.text == typed for c in items)):
                accept_completion(buf, current or items[0], kind)  # pilih file dulu; Enter berikutnya mengirim
                return
        buf.validate_and_handle()

    @kb.add("tab")
    def _tab(event):
        buf = event.current_buffer
        kind = completion_context(buf)
        items, current = _candidates(buf, completer) if kind else ([], None)
        if items:
            accept_completion(buf, current or items[0], kind)
        else:
            generate_completions(event)

    @kb.add("escape", filter=has_completions)
    def _close_menu(event):
        event.current_buffer.cancel_completion()

    @kb.add("escape", "enter")
    @kb.add("c-j")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    @kb.add("s-tab")
    def _cycle_mode(event):
        app.cycle_mode()
        event.app.invalidate()

    @kb.add("c-l")
    def _clear(event):
        event.app.renderer.clear()

    history_path = maba_home() / "history"
    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history = FileHistory(str(history_path))
    except OSError:
        history = InMemoryHistory()

    def open_menu() -> str | None:
        """'slash' | 'file' bila menu autocomplete sedang terbuka; toolbar menyesuaikan petunjuk tombolnya."""
        try:
            buf = get_app().current_buffer
            state = buf.complete_state
            return completion_context(buf) if state is not None and state.completions else None
        except Exception:  # noqa: BLE001 - toolbar tidak boleh merusak prompt
            return None

    def terminal_width() -> int | None:
        try:
            return get_app().output.get_size().columns
        except Exception:  # noqa: BLE001 - tanpa lebar, semua bagian toolbar ditampilkan
            return None

    def toolbar() -> FormattedText:
        return FormattedText(app.toolbar_fragments(open_menu(), terminal_width()))

    def placeholder() -> FormattedText:
        if app.total_in or app.total_out or app.agent.messages:
            text = "Ketik pesan Anda…   ( / perintah · @ file · ! shell )"
        else:
            text = 'Ketik tugas Anda, mis. "jelaskan struktur proyek ini"   ( / perintah · @ file · ! shell )'
        return FormattedText([("class:placeholder", text)])

    return PromptSession(
        message=FormattedText([("class:prompt", f"{symbol} ")]),
        multiline=True,
        prompt_continuation=lambda width, line_number, wrap_count: "  ",
        key_bindings=kb,
        history=history,
        completer=completer,
        complete_in_thread=True,  # indeks file (git ls-files) tidak boleh membuat ketikan tersendat
        complete_while_typing=True,
        reserve_space_for_menu=10,
        bottom_toolbar=toolbar,
        placeholder=placeholder,
        refresh_interval=1.0,  # toolbar ikut berubah saat task latar belakang selesai
        style=PROMPT_STYLE,
        # Wajib False: prompt_toolkit mematikan complete_while_typing bila history search aktif, sehingga menu
        # / dan @ hanya muncul setelah Tab. Riwayat tetap bisa dibuka dengan panah atas/bawah dan Ctrl+R.
        enable_history_search=False,
        mouse_support=False,
        input=input,
        output=output,
    )
