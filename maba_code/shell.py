"""Eksekusi perintah shell lintas-platform: PowerShell di Windows, bash di POSIX.

Fitur: timeout, pembatalan (Esc/Ctrl+C), kill seluruh pohon proses, batas output,
dan environment yang dibersihkan dari kredensial (agen tidak mewarisi rahasia AWS di env).
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

from maba_code.fsops import truncate_middle

MAX_OUTPUT_CHARS = 30_000
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
MAX_COMMAND_CHARS = 8_000
IS_WINDOWS = os.name == "nt"

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
_SECRET_ENV = {
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN",
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN",
}

_PS_PREAMBLE = (
    "$ProgressPreference='SilentlyContinue';"
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
    "$OutputEncoding=[System.Text.Encoding]::UTF8;\n"
)
_PS_EPILOGUE = (
    "\n$__ok=$?;$__ec=$LASTEXITCODE;"
    "if($__ec){exit $__ec}elseif($__ok){exit 0}else{exit 1}"
)


@dataclass
class ShellResult:
    output: str
    exit_code: int | None
    timed_out: bool = False
    cancelled: bool = False
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancelled


def clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() not in _SECRET_ENV}
    env["MABA_CODE"] = "1"
    env.setdefault("PYTHONUTF8", "1")
    return env


def shell_name() -> str:
    if IS_WINDOWS:
        return "PowerShell"
    return "bash"


def _argv(command: str) -> list[str]:
    if IS_WINDOWS:
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
        script = _PS_PREAMBLE + command + _PS_EPILOGUE
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    return [shutil.which("bash") or "/bin/sh", "-c", command]


def _kill_tree(pid: int) -> None:
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False,
            )
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass


@dataclass
class HookProcessResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False


async def run_hook_command(
    command: str,
    cwd: str,
    stdin_data: bytes,
    timeout: float,
    cancel: threading.Event | None = None,
    extra_env: dict[str, str] | None = None,
) -> HookProcessResult:
    """Jalankan perintah hook pengguna: JSON lewat stdin, stdout & stderr terpisah (semantik exit code 2)."""
    kwargs: dict = {}
    if not IS_WINDOWS:
        kwargs["start_new_session"] = True
    env = clean_env()
    env.update(extra_env or {})
    proc = await asyncio.create_subprocess_exec(
        *_argv(command),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        **kwargs,
    )
    comm = asyncio.ensure_future(proc.communicate(stdin_data))
    timed_out = cancelled = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            done, _ = await asyncio.wait({comm}, timeout=0.2)
            if done:
                break
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            if time.monotonic() > deadline:
                timed_out = True
                break
    except asyncio.CancelledError:
        _kill_tree(proc.pid)
        raise
    finally:
        if not comm.done():
            _kill_tree(proc.pid)
            try:
                await asyncio.wait_for(comm, 5)
            except (asyncio.TimeoutError, ProcessLookupError):
                comm.cancel()
    if timed_out or cancelled or comm.cancelled():
        return HookProcessResult(None, "", "", timed_out=timed_out, cancelled=cancelled)
    out, err = comm.result()

    def clean(raw: bytes) -> str:
        return _ANSI.sub("", raw[:MAX_CAPTURE_BYTES].decode("utf-8", errors="replace")).replace("\r\n", "\n").strip()

    return HookProcessResult(proc.returncode, clean(out), clean(err))


async def run_shell(
    command: str,
    cwd: str,
    timeout: float = 120.0,
    cancel: threading.Event | None = None,
) -> ShellResult:
    if len(command) > MAX_COMMAND_CHARS:
        return ShellResult(f"Command too long ({len(command)} chars; max {MAX_COMMAND_CHARS}).", 2)

    started = time.monotonic()
    kwargs: dict = {}
    if not IS_WINDOWS:
        kwargs["start_new_session"] = True
    proc = await asyncio.create_subprocess_exec(
        *_argv(command),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env=clean_env(),
        **kwargs,
    )

    chunks: list[bytes] = []
    captured = 0
    dropped = 0

    async def pump() -> None:
        nonlocal captured, dropped
        assert proc.stdout is not None
        while True:
            data = await proc.stdout.read(8192)
            if not data:
                return
            if captured < MAX_CAPTURE_BYTES:
                chunks.append(data)
                captured += len(data)
            else:
                dropped += len(data)

    pump_task = asyncio.ensure_future(pump())
    wait_task = asyncio.ensure_future(proc.wait())
    timed_out = cancelled = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            done, _ = await asyncio.wait({wait_task}, timeout=0.2)
            if done:
                break
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            if time.monotonic() > deadline:
                timed_out = True
                break
    except asyncio.CancelledError:
        _kill_tree(proc.pid)
        raise
    finally:
        if not wait_task.done():
            _kill_tree(proc.pid)
            try:
                await asyncio.wait_for(wait_task, 5)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
        # Proses anak yang mewarisi pipe bisa menahan EOF; jangan menunggu selamanya.
        try:
            await asyncio.wait_for(pump_task, 2)
        except asyncio.TimeoutError:
            pump_task.cancel()

    text = b"".join(chunks).decode("utf-8", errors="replace")
    text = _ANSI.sub("", text).replace("\r\n", "\n").strip("\n")
    if dropped:
        text += f"\n... [{dropped} more bytes discarded]"
    return ShellResult(
        truncate_middle(text, MAX_OUTPUT_CHARS),
        None if (timed_out or cancelled) else proc.returncode,
        timed_out=timed_out,
        cancelled=cancelled,
        duration=time.monotonic() - started,
    )
