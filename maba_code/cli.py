"""Titik masuk CLI: ``maba`` / ``python -m maba_code``."""

from __future__ import annotations

import argparse
import asyncio
import atexit
import os
import sys
from pathlib import Path

from rich.console import Console

from maba_code import __version__
from maba_code.config import DEFAULT_MODEL, DEFAULT_PROFILE, MODES, load_settings


def configure_stdio() -> None:
    """Paksa UTF-8 (cp1252 Windows melempar UnicodeEncodeError pada simbol/aksara non-ASCII)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    if os.name == "nt":
        try:
            import ctypes

            kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
            old_out, old_in = kernel.GetConsoleOutputCP(), kernel.GetConsoleCP()
            if old_out and old_out != 65001:
                kernel.SetConsoleOutputCP(65001)
                kernel.SetConsoleCP(65001)
                atexit.register(lambda: (kernel.SetConsoleOutputCP(old_out), kernel.SetConsoleCP(old_in)))
        except Exception:  # noqa: BLE001 - kosmetik saja
            pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="maba",
        description="MABA Code — asisten coding agentik di terminal (Strands Agents + Amazon Bedrock).",
    )
    p.add_argument("prompt", nargs="*", help="Prompt awal (mode interaktif) atau prompt tunggal bila memakai -p")
    p.add_argument("-p", "--print", dest="print_mode", action="store_true", help="Mode non-interaktif: jalankan satu prompt lalu keluar")
    p.add_argument("--profile", help=f"Profile AWS (default: {DEFAULT_PROFILE})")
    p.add_argument("--model", help=f"Model ID atau alias (default: {DEFAULT_MODEL})")
    p.add_argument("--region", help="Override region AWS (default: region profile)")
    p.add_argument("--mode", choices=MODES, help="Mode izin awal")
    p.add_argument("--max-turns", type=int, help="Batas giliran model per perintah")
    p.add_argument("-c", "--continue", dest="continue_", action="store_true", help="Lanjutkan sesi terakhir di folder ini")
    p.add_argument("--resume", metavar="ID", help="Lanjutkan sesi tertentu")
    p.add_argument("--cwd", help="Folder kerja (default: folder saat ini)")
    p.add_argument("--doctor", action="store_true", help="Uji profile AWS, kredensial, dan model lalu keluar")
    p.add_argument("--version", action="version", version=f"MABA Code {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = build_parser().parse_args(argv)

    from maba_code.commands import CommandRegistry, run_doctor
    from maba_code.mentions import expand_mentions
    from maba_code.repl import run_repl
    from maba_code.session import ChatApp, configure_logging
    from maba_code.ui.terminal import TerminalUI

    configure_logging()
    cwd = Path(args.cwd or os.getcwd()).resolve()
    if not cwd.is_dir():
        print(f"Folder tidak ditemukan: {cwd}", file=sys.stderr)
        return 2
    settings = load_settings(
        cwd, {"profile": args.profile, "model": args.model, "region": args.region, "mode": args.mode, "max_turns": args.max_turns}
    )

    prompt = " ".join(args.prompt).strip()
    stdin_is_tty = sys.stdin is not None and sys.stdin.isatty()
    # Tanpa terminal (mis. `echo "..." | maba`) mode interaktif tidak mungkin: otomatis jadi mode -p.
    print_mode = args.print_mode or not (stdin_is_tty or args.doctor)
    if print_mode and not prompt and not stdin_is_tty:
        prompt = sys.stdin.read().strip() if sys.stdin is not None else ""

    if print_mode:
        ui = TerminalUI(Console(highlight=False), Console(stderr=True, highlight=False), interactive=False, render_markdown=False)
    else:
        ui = TerminalUI(Console(highlight=False))

    session_id = args.resume
    try:
        app = ChatApp(settings, cwd, ui, session_id=session_id)
    except Exception as exc:  # noqa: BLE001
        from maba_code.models import friendly_error

        title, hint = friendly_error(exc, settings)
        ui.error(title, hint)
        return 1
    if args.continue_ and not session_id:
        latest = app.latest_session_id()
        if latest:
            app.resume_session(latest)
            app.resumed = True
    registry = CommandRegistry(app)

    if args.doctor:
        return 0 if asyncio.run(run_doctor(app)) else 1

    if print_mode:
        if not prompt:
            print("Mode -p memerlukan prompt (argumen atau stdin).", file=sys.stderr)
            return 2
        expanded, _ = expand_mentions(prompt, app.ws)

        async def run_once():
            await app.start()
            try:
                return await app.run_prompt(expanded)
            finally:
                await app.shutdown("other")

        stats = asyncio.run(run_once())
        return 1 if stats.error else 2 if (stats.cancelled or stats.blocked or stats.stop_reason == "limit_turns") else 0

    try:
        asyncio.run(run_repl(app, registry, prompt or None))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
