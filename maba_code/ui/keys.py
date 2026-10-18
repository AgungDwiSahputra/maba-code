"""Pengamat tombol Esc saat agen bekerja (Windows: msvcrt; POSIX: hanya Ctrl+C).

Selama agen berjalan, prompt_toolkit tidak aktif sehingga Esc tidak tertangkap. Thread kecil ini
memantau tombol; teks yang diketik pengguna saat agen bekerja disimpan sebagai 'type-ahead'
dan dipasang di prompt berikutnya (tidak hilang). Dialog izin memakai ``paused()`` agar
tidak berebut input.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager


class EscWatcher:
    def __init__(self, on_escape: Callable[[], None]):
        self._on_escape = on_escape
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.typeahead = ""

    @property
    def enabled(self) -> bool:
        try:
            return os.name == "nt" and sys.stdin is not None and sys.stdin.isatty()
        except (ValueError, OSError):
            return False

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="maba-esc-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=0.5)

    @contextmanager
    def paused(self) -> Iterator[None]:
        with self._lock:
            yield

    def take_typeahead(self) -> str:
        text, self.typeahead = self.typeahead, ""
        return text

    def _loop(self) -> None:
        import msvcrt  # hanya tersedia di Windows

        while not self._stop.is_set():
            with self._lock:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\x1b", "\x03"):
                        self._on_escape()
                    elif ch in ("\x00", "\xe0"):
                        msvcrt.getwch()  # buang kode tombol khusus (panah, dsb.)
                    elif ch == "\x08":
                        self.typeahead = self.typeahead[:-1]
                    elif ch.isprintable():
                        self.typeahead += ch
                    continue
            time.sleep(0.03)
