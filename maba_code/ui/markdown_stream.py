"""Render Markdown secara progresif per-blok (paragraf/kode utuh), tanpa kedipan.

Teks dari model datang sebagai potongan kecil. Daripada menggambar ulang seluruh layar,
kita menahan teks sampai satu blok lengkap (dipisah baris kosong, dan tidak sedang di dalam
pagar kode ```), lalu mencetaknya sebagai Markdown. Hasilnya tetap terasa 'hidup' tetapi
stabil di terminal Windows mana pun.
"""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from maba_code.ui.theme import ACCENT, Symbols


class BlockMarkdownStream:
    def __init__(self, console: Console, symbols: Symbols, *, render: bool = True):
        self.console = console
        self.sym = symbols
        self.render = render
        self._buf = ""
        self._first = True

    def feed(self, text: str) -> None:
        self._buf += text
        while True:
            cut = self._split_point()
            if cut is None:
                return
            block, self._buf = self._buf[:cut], self._buf[cut:].lstrip("\n")
            self._emit(block)

    def flush(self) -> None:
        if self._buf.strip():
            self._emit(self._buf)
        self._buf = ""
        self._first = True

    @property
    def pending(self) -> bool:
        return bool(self._buf.strip())

    def _split_point(self) -> int | None:
        pos = 0
        while True:
            idx = self._buf.find("\n\n", pos)
            if idx == -1:
                return None
            if self._buf[:idx].count("```") % 2 == 0:
                return idx
            pos = idx + 2

    def _emit(self, block: str) -> None:
        block = block.strip("\n")
        if not block.strip():
            return
        if not self.render:
            self.console.out(block + "\n", highlight=False)
            self._first = False
            return
        grid = Table.grid(padding=0)
        grid.add_column(width=2, no_wrap=True)
        grid.add_column(ratio=1)
        marker = Text(f"{self.sym.bullet} ", style=ACCENT) if self._first else Text("  ")
        grid.add_row(marker, Markdown(block))
        self.console.print(grid)
        self._first = False
