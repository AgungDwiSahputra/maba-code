"""Task shell latar belakang: proses yang tetap berjalan lintas giliran (dev server, watcher, build panjang).

Seperti Claude Code: ``run_command(run_in_background=true)`` langsung mengembalikan id; output ditulis ke file
log yang dibaca model dengan ``read_file``; ``task_stop`` menghentikannya; saat proses selesai, ringkasannya
disisipkan ke pesan berikutnya. Proses dimatikan (beserta anak-anaknya) saat MABA Code keluar.
"""

from __future__ import annotations

import atexit
import secrets
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from maba_code.shell import _ANSI, IS_WINDOWS, MAX_COMMAND_CHARS, _argv, _kill_tree, clean_env

MAX_RUNNING = 8
NOTICE_TAIL_LINES = 20
_TAIL_BYTES = 64 * 1024


@dataclass
class BackgroundTask:
    id: str
    command: str
    description: str
    log_path: Path
    started: float
    proc: subprocess.Popen | None = None
    exit_code: int | None = None
    finished: float | None = None
    stopped: bool = False

    @property
    def running(self) -> bool:
        return self.finished is None

    def status(self) -> str:
        if self.running:
            return "berjalan"
        if self.stopped:
            return "dihentikan"
        return f"selesai (exit {self.exit_code})"

    def runtime(self) -> float:
        return (self.finished or time.time()) - self.started


class BackgroundTasks:
    def __init__(self, folder: Path):
        self.folder = folder
        self.tasks: dict[str, BackgroundTask] = {}
        self._lock = threading.Lock()
        self._finished: deque[str] = deque()
        atexit.register(self.kill_all)

    def start(self, command: str, cwd: str, description: str = "") -> BackgroundTask:
        if len(command) > MAX_COMMAND_CHARS:
            raise RuntimeError(f"command too long ({len(command)} chars; max {MAX_COMMAND_CHARS})")
        if len(self.running()) >= MAX_RUNNING:
            raise RuntimeError(f"{MAX_RUNNING} background tasks are already running; stop one with task_stop first")
        self.folder.mkdir(parents=True, exist_ok=True)
        task_id = "bg-" + secrets.token_hex(3)
        log_path = self.folder / f"{task_id}.log"
        kwargs: dict = {}
        if IS_WINDOWS:
            # Grup proses sendiri: Ctrl+C di konsol MABA tidak ikut membunuh server yang sengaja dijalankan.
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(
                _argv(command), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                cwd=cwd, env=clean_env(), **kwargs,
            )
        task = BackgroundTask(task_id, command, description, log_path, time.time(), proc)
        with self._lock:
            self.tasks[task_id] = task
        threading.Thread(target=self._wait, args=(task,), daemon=True, name=f"maba-{task_id}").start()
        return task

    def _wait(self, task: BackgroundTask) -> None:
        assert task.proc is not None
        code = task.proc.wait()
        with self._lock:
            task.exit_code = code
            task.finished = time.time()
            self._finished.append(task.id)

    def stop(self, task_id: str) -> BackgroundTask:
        task = self.tasks[task_id.strip()]
        if task.running and task.proc is not None:
            task.stopped = True
            _kill_tree(task.proc.pid)
            try:
                task.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        return task

    def running(self) -> list[BackgroundTask]:
        return [t for t in self.tasks.values() if t.running]

    def take_finished(self) -> list[BackgroundTask]:
        """Task yang selesai sendiri sejak panggilan terakhir (yang dihentikan task_stop tidak dilaporkan ulang)."""
        with self._lock:
            ids = list(self._finished)
            self._finished.clear()
        return [self.tasks[i] for i in ids if not self.tasks[i].stopped]

    @staticmethod
    def tail(task: BackgroundTask, lines: int = NOTICE_TAIL_LINES) -> str:
        try:
            with open(task.log_path, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - _TAIL_BYTES))
                raw = fh.read()
        except OSError:
            return ""
        text = _ANSI.sub("", raw.decode("utf-8", errors="replace")).replace("\r\n", "\n")
        return "\n".join(text.strip("\n").splitlines()[-lines:])

    def kill_all(self) -> None:
        for task in self.running():
            task.stopped = True
            if task.proc is not None:
                _kill_tree(task.proc.pid)


def finished_notice(task: BackgroundTask, tail: str) -> str:
    return (
        f'<background-task-finished id="{task.id}" exit_code="{task.exit_code}" log="{task.log_path}">\n'
        f"$ {task.command}\n{tail or '(no output)'}\n</background-task-finished>"
    )
