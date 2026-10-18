"""REPL interaktif MABA Code."""

from __future__ import annotations

import time

from maba_code.commands import CommandRegistry, ExitRequested
from maba_code.mentions import expand_mentions
from maba_code.session import ChatApp
from maba_code.ui.prompt import create_prompt_session


async def handle_line(app: ChatApp, registry: CommandRegistry, line: str) -> None:
    text = line.strip()
    if not text:
        return
    if text.lower() in {"exit", "quit"}:
        raise ExitRequested
    if text.startswith("/"):
        await registry.dispatch(text)
        return
    if text.startswith("!"):
        command = text[1:].strip()
        if command:
            await app.run_user_shell(command)
        return
    expanded, attached = expand_mentions(text, app.ws)
    if attached:
        app.ui.notice("Menyertakan: " + ", ".join(attached), "dim")
    await app.run_prompt(expanded)


async def run_repl(app: ChatApp, registry: CommandRegistry, initial: str | None = None, session=None) -> None:
    """``session`` hanya untuk pengujian (PromptSession dengan pipe input)."""
    ui = app.ui
    ui.banner(app.settings, app.ws.cwd, app.policy.mode, app.session_id, app.resumed, notes=app.readiness())
    if app.resumed:
        ui.notice(f"Melanjutkan sesi: {len(app.agent.messages)} pesan dimuat.", "green")
    await app.start()
    session = session or create_prompt_session(app, ui.sym.prompt)
    last_ctrl_c = 0.0
    try:
        if initial:
            await handle_line(app, registry, initial)
        while True:
            app.collect_background()
            default = app.watcher.take_typeahead()
            try:
                line = await session.prompt_async(default=default)
            except KeyboardInterrupt:
                now = time.monotonic()
                if now - last_ctrl_c < 2.0:
                    break
                last_ctrl_c = now
                ui.notice("Tekan Ctrl+C sekali lagi (atau Ctrl+D) untuk keluar.", "yellow")
                continue
            except EOFError:
                break
            try:
                await handle_line(app, registry, line)
            except KeyboardInterrupt:
                ui.notice("Dibatalkan.", "yellow")
    except ExitRequested:
        pass
    finally:
        ui.clear_status()
        await app.shutdown("prompt_input_exit")
    ui.notice(f"Sampai jumpa. Lanjutkan nanti dengan: maba --continue   (sesi {app.session_id})", "dim")
