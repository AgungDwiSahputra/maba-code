"""ChatApp: merakit agen Strands + izin + UI, menjalankan giliran, dan mengelola sesi."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import math
import os
import secrets
import shutil
import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from strands import Agent
from strands.agent.conversation_manager import NullConversationManager, SummarizingConversationManager
from strands.agent.conversation_manager.summarizing_conversation_manager import DEFAULT_SUMMARIZATION_PROMPT
from strands.hooks import BeforeModelCallEvent, HookProvider, HookRegistry
from strands.session.file_session_manager import FileSessionManager

from maba_code.agents import AgentDef, load_agents, render_agents
from maba_code.config import AUTO_COMPACT_PCT, CORE_MODEL, MODES, Settings, maba_home, project_key, resolve_model
from maba_code.hooks import STOP_HOOK_CAP, HookRunner
from maba_code.mcp_support import McpManager
from maba_code.memory import auto_memory_dir, load_auto_memory, load_memory, render_memory
from maba_code.models import build_model, friendly_error
from maba_code.permissions import (
    NullFrontend, PermissionAnswer, PermissionHook, PermissionPolicy, PermissionRequest, Rule,
)
from maba_code.prompts import SUBAGENT_FOOTER, WEB_FETCH_PROMPT, build_system_prompt, environment_block
from maba_code.shell import run_shell
from maba_code.skills import Skill, expand_skill, load_skills, render_skills, skill_rules
from maba_code.tasks import BackgroundTasks, finished_notice
from maba_code.web import WebFetcher
from maba_code.tools import MAIN_AGENT_ONLY, SUBAGENT_SPAWN_TOOLS, build_tools
from maba_code.ui.keys import EscWatcher
from maba_code.ui.theme import (
    CTX_CRIT_PCT, CTX_WARN_PCT, MODE_HINT, MODE_SHORT, MODE_TAG, TOOL_LABELS, activity_label, describe_tool, meter,
    pick_symbols,
)
from maba_code.workspace import Workspace

log = logging.getLogger("maba")
CYCLE_MODES = ("default", "acceptEdits", "plan")
INTERRUPT_NOTE = "[Dihentikan oleh pengguna]"
TURN_WARN_FRACTION = 0.8  # peringatkan pengguna saat putaran model mencapai porsi ini dari batas per perintah
FLASH_SECONDS = 6.0  # lama keterangan sesaat (mis. arti mode baru) tampil di toolbar


MAX_SUBAGENT_DEPTH = 2  # agen utama = 0; sub-agen tingkat 2 tidak lagi mendapat tool task/explore
_SUBAGENT_DEPTH: ContextVar[int] = ContextVar("maba_subagent_depth", default=0)


class SubagentFrontend(NullFrontend):
    """Frontend sub-agen: aktivitas tampil di spinner agen utama.

    ``forward=False`` (bawaan read-only): semua permintaan izin otomatis ditolak, seperti sebelumnya.
    ``forward=True``: dialog izin diteruskan ke pengguna, diberi label nama sub-agen.
    """

    def __init__(self, parent: Any, agent: str, forward: bool):
        self.parent = parent
        self.agent = agent
        self.forward = forward

    def tool_started(self, name: str, inp: dict[str, Any]) -> None:
        text = f"{activity_label(name)} {describe_tool(name, inp, 50)}".strip()
        if hasattr(self.parent, "subagent_step"):
            self.parent.subagent_step(self.agent, text)
        else:
            self.parent.set_status(f"{self.agent} › {text}")

    def tool_finished(self, name, inp, ok, text, duration, note) -> None:
        if hasattr(self.parent, "subagent_tool_finished"):
            self.parent.subagent_tool_finished(name, inp, ok, note)

    async def ask_permission(self, request: PermissionRequest) -> PermissionAnswer:
        if not self.forward:
            return await super().ask_permission(request)
        request.title = f"[sub-agen {self.agent}] {request.title}"
        return await self.parent.ask_permission(request)

    def tool_denied(self, name: str, inp: dict[str, Any], reason: str) -> None:
        if self.forward:
            self.parent.notice(f"[{self.agent}] {TOOL_LABELS.get(name, name)} ditolak: {reason}", "yellow")

    def notice(self, message: str, style: str = "") -> None:
        self.parent.notice(f"[{self.agent}] {message}", style or "dim")

    def mode_changed(self, mode: str) -> None:
        self.parent.mode_changed(mode)


class _ModelCallCounter(HookProvider):
    """Hitung putaran model agen utama agar pengguna diperingatkan sebelum batas giliran per perintah tercapai."""

    def __init__(self, on_call: Callable[[], None]):
        self._on_call = on_call

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self._before)

    def _before(self, event: BeforeModelCallEvent) -> None:
        self._on_call()


def session_root(cwd: Path) -> Path:
    return maba_home() / "sessions" / project_key(cwd)


def new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


@dataclass
class TurnStats:
    tokens_in: int = 0
    tokens_out: int = 0
    seconds: float = 0.0
    ctx_pct: int | None = None
    stop_reason: str = ""
    error: str | None = None
    cancelled: bool = False
    blocked: bool = False  # prompt ditolak hook UserPromptSubmit (model tidak dipanggil)


class ChatApp:
    def __init__(
        self,
        settings: Settings,
        cwd: Path,
        ui: Any,
        *,
        session_id: str | None = None,
        model_factory: Callable[[Settings], Any] | None = None,
    ):
        self.settings = settings
        self.ui = ui
        self.ws = Workspace(cwd)
        self.policy = PermissionPolicy(settings)
        self.model_factory = model_factory or build_model
        self.model = self.model_factory(settings)
        # GPT-6 Luna adalah mesin inti: semua agen anak (sub-agen, skill fork, web_fetch, kompaksi) memakai
        # core_model, yang tidak pernah ikut berubah oleh /model.
        if settings.model == CORE_MODEL:
            self.core_model = self.model
        else:
            self.core_model = self.model_factory(dataclasses.replace(settings, model=CORE_MODEL))
        self.summarizer = Agent(
            model=self.core_model,
            system_prompt=DEFAULT_SUMMARIZATION_PROMPT,
            callback_handler=None,
            conversation_manager=NullConversationManager(),
            name="maba-summarizer",
        )
        self.tools = build_tools(self.ws)
        self.mcp_tools: list[Any] = []  # diisi saat start() bila ada server MCP
        self.hooks = HookRunner(settings.hooks, self.ws.cwd, self._hook_base_payload, settings.warnings)
        self.hook = PermissionHook(self.policy, self.ws, ui, hooks=self.hooks)
        self.session_dir = session_root(self.ws.cwd)
        self.total_in = 0
        self.total_out = 0
        self.last_ctx_pct: int | None = None
        self.pending_context: list[str] = []
        self.first_prompt = ""
        self.command_names: Callable[[], list[tuple[str, str]]] = lambda: []
        self.commands: Any = None  # diisi oleh CommandRegistry
        self._cancelling = False
        self._turn_active = False
        self._loop_wake: Callable[[], None] | None = None  # membangunkan /loop yang sedang menunggu
        self._compacting_manually = False
        self._model_calls = 0  # putaran model dalam satu stream_async (untuk peringatan batas giliran)
        self._flash: tuple[str, float] | None = None  # keterangan sesaat di toolbar (teks, batas waktu monotonic)
        self._started = False
        self._shown_warnings: set[str] = set(settings.warnings)  # peringatan settings sudah tampil di banner
        self._base_trusted_roots: list[Path] = []  # folder log task & memori otomatis (diisi di bawah)
        self.background = BackgroundTasks(maba_home() / "tasks" / project_key(self.ws.cwd))
        self.ws.background = self.background
        self._base_trusted_roots.append(self.background.folder)
        if settings.auto_memory:
            self.ws.memory_dir = auto_memory_dir(self.ws.cwd)
            try:
                self.ws.memory_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                log.debug("folder memori otomatis tidak bisa dibuat", exc_info=True)
            self._base_trusted_roots.append(self.ws.memory_dir)

        self.ws.get_mode = lambda: self.policy.mode
        self.ws.run_subagent = self.run_subagent
        self.ws.run_skill = self.run_skill
        self.web = WebFetcher()
        self.ws.web_fetch = self.web_fetch
        self.mcp = McpManager(self.ws.cwd, settings)  # hanya membaca konfigurasi; server dijalankan di start()
        if hasattr(ui, "show_todos"):
            self.ws.on_todos = ui.show_todos
        self.watcher = EscWatcher(self.cancel)
        if hasattr(ui, "watcher"):
            ui.watcher = self.watcher
        if hasattr(ui, "on_state_change"):
            ui.on_state_change = None

        self.session_id = session_id or new_session_id()
        self.resumed = self._meta_path(self.session_id).exists()
        self.agent = self._build_agent(self.session_id)

    # ----------------------------------------------------------- konstruksi
    def _warn_once(self, warnings: list[str]) -> None:
        for warning in warnings:
            if warning not in self._shown_warnings:
                self._shown_warnings.add(warning)
                self.ui.notice(f"! {warning}", "yellow")

    def reload_extensions(self) -> None:
        """Muat ulang sub-agen & skill (murah; dipanggil tiap giliran agar file baru langsung terpakai)."""
        warnings: list[str] = []
        self.agent_defs: dict[str, AgentDef] = load_agents(self.ws.cwd, warnings)
        self.skills: dict[str, Skill] = load_skills(self.ws.cwd, warnings)
        self.ws.trusted_read_roots = [*self._base_trusted_roots, *(s.folder for s in self.skills.values())]
        self._warn_once(warnings)

    def _system_prompt(self) -> str:
        self.reload_extensions()
        memory = render_memory(load_memory(self.ws.cwd))
        folder = self.ws.memory_dir
        return build_system_prompt(
            self.ws.cwd, self.settings.model, self.policy.mode, memory,
            agents=render_agents(self.agent_defs), skills=render_skills(self.skills),
            auto_memory=(folder, load_auto_memory(folder)) if folder is not None else None,
        )

    def all_tools(self) -> list[Any]:
        return [*self.tools, *self.mcp_tools]

    def _build_agent(self, session_id: str) -> Agent:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        cm = SummarizingConversationManager(
            summary_ratio=0.3,
            preserve_recent_messages=8,
            summarization_agent=self.summarizer,  # ringkasan selalu oleh GPT-6 Luna
            proactive_compression={"compression_threshold": AUTO_COMPACT_PCT / 100},
        )
        # Bungkus pada INSTANCE (bukan subclass: nama kelas disimpan di sesi dan dicek saat resume).
        original = cm.reduce_context

        def reduce_context(agent: Any, e: Exception | None = None, **kwargs: Any) -> None:
            if self._compacting_manually:  # /compact mengumumkan dirinya sendiri
                return original(agent, e, **kwargs)
            if self.hooks.has("PreCompact"):
                outcome = self.hooks.run_sync("PreCompact", {"trigger": "auto", "custom_instructions": None}, "auto")
                for error in outcome.errors:
                    log.debug("PreCompact hook: %s", error)
            before = len(agent.messages)
            reason = "Konteks penuh (batas model tercapai)" if e is not None else f"Konteks mendekati {AUTO_COMPACT_PCT}%"
            self.ui.notice(f"{reason}: percakapan lama diringkas otomatis…", "yellow")
            self.ui.set_phase("compact", cancellable=False)
            try:
                result = original(agent, e, **kwargs)
            finally:
                self.ui.set_phase("think", "melanjutkan setelah peringkasan")
            self.ui.notice(f"Percakapan diringkas otomatis: {before} → {len(agent.messages)} pesan.", "green")
            return result

        cm.reduce_context = reduce_context  # type: ignore[method-assign]
        return Agent(
            model=self.model,
            system_prompt=self._system_prompt(),
            tools=self.all_tools(),
            callback_handler=None,
            conversation_manager=cm,
            session_manager=FileSessionManager(session_id=session_id, storage_dir=str(self.session_dir)),
            hooks=[self.hook, _ModelCallCounter(self._on_model_call)],
            name="maba-code",
        )

    def _on_model_call(self) -> None:
        self._model_calls += 1
        limit = self.settings.max_turns
        if limit >= 5 and self._model_calls == max(2, math.ceil(limit * TURN_WARN_FRACTION)):
            self.ui.notice(
                f"Agen sudah memakai {self._model_calls} dari {limit} putaran model untuk perintah ini; "
                "batasnya hampir tercapai (atur dengan --max-turns).",
                "yellow",
            )

    # ------------------------------------------------------------- siklus
    def _hook_base_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "transcript_path": str(self.session_dir / f"session_{self.session_id}"),
            "cwd": str(self.ws.cwd),
            "permission_mode": self.policy.mode,
            "model": CORE_MODEL,
        }

    def _report_hook(self, outcome: Any, event: str) -> None:
        for message in outcome.messages:
            self.ui.notice(f"[hook {event}] {message}", "cyan")
        for error in outcome.errors:
            self.ui.notice(f"Hook {event} error (diabaikan): {error}", "yellow")

    async def session_start(self, source: str) -> None:
        """Hook SessionStart (startup | resume | clear | compact): konteksnya masuk ke pesan berikutnya."""
        if not self.hooks.has("SessionStart"):
            return
        outcome = await self.hooks.run("SessionStart", {"source": source}, match_value=source)
        self._report_hook(outcome, "SessionStart")
        if outcome.context_text:
            self.pending_context.append(f"<session-start-hook>\n{outcome.context_text}\n</session-start-hook>")

    async def start(self) -> None:
        """Dipanggil sekali oleh REPL / mode -p sebelum giliran pertama."""
        if self._started:
            return
        self._started = True
        await self.start_mcp()
        await self.session_start("resume" if self.resumed else "startup")

    async def _approve_mcp(self, cfg: Any) -> bool:
        request = PermissionRequest(
            "mcp", "Jalankan server MCP dari .mcp.json proyek?", cfg.name, cfg.describe(), "command",
            "server dari repo ini akan berjalan di komputer Anda", danger=True,
            impact="Program server itu dijalankan di komputer Anda dan tool-nya bisa dipakai agen. Setujui hanya bila Anda mempercayai repo ini.",
            mode=self.policy.mode,
        )
        async with self.ws.dialog_lock:
            answer = await self.ui.ask_permission(request)
        return answer.choice in ("allow", "allow_session")

    async def start_mcp(self) -> None:
        self._warn_once(self.mcp.warnings)
        if not self.mcp.configs:
            return
        approve = self._approve_mcp if getattr(self.ui, "interactive", False) else None
        self.ui.set_status("Menghubungkan server MCP…", cancellable=False)
        try:
            await self.mcp.start(approve)
        finally:
            self.ui.clear_status()
        self._warn_once(self.mcp.warnings)
        for state in self.mcp.states.values():
            ok = state.status == "terhubung"
            detail = f"{len(state.tools)} tool" if ok else state.status
            self.ui.notice(f"MCP {state.config.name}: {detail}", "dim" if ok else "yellow")
        if self.mcp.tools:
            self.mcp_tools = list(self.mcp.tools)
            for tool in self.mcp_tools:
                self.agent.tool_registry.register_tool(tool)

    def collect_background(self) -> None:
        """Umumkan task latar belakang yang sudah selesai & sisipkan ringkasannya ke pesan berikutnya."""
        for task in self.background.take_finished():
            self.ui.notice(f"Task latar belakang {task.id} {task.status()}: {task.command[:80]}", "cyan")
            self.pending_context.append(finished_notice(task, self.background.tail(task)))

    async def run_loop(self, once: Callable[[], Any], interval: float, max_iterations: int, label: str) -> int:
        """/loop: jalankan ``once`` berulang dengan jeda tetap; berhenti oleh Esc/Ctrl+C, error, atau blok hook."""
        self.ui.notice(
            f"Loop dimulai: '{label[:60]}' tiap {_fmt_seconds(interval)}, maks. {max_iterations}x · Esc/Ctrl+C untuk berhenti",
            "cyan",
        )
        done = 0
        for i in range(1, max_iterations + 1):
            self._cancelling = False
            stats = await once()
            done = i
            if stats is not None and (stats.error or stats.cancelled or stats.blocked):
                break
            if i == max_iterations or not await self._wait_interruptible(interval, f"Loop {i}/{max_iterations}"):
                break
        self.ui.notice(f"Loop selesai setelah {done} iterasi.", "cyan")
        return done

    async def _wait_interruptible(self, seconds: float, label: str) -> bool:
        """Tunggu ``seconds``; False bila dibangunkan cancel() (Esc/Ctrl+C)."""
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()
        self._cancelling = False
        self._loop_wake = lambda: loop.call_soon_threadsafe(wake.set)
        deadline = loop.time() + seconds
        try:
            with self._interrupts():
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        return True
                    self.ui.set_status(f"{label} · berikutnya {_fmt_seconds(remaining)}")
                    try:
                        await asyncio.wait_for(wake.wait(), timeout=min(1.0, remaining))
                        return False
                    except asyncio.TimeoutError:
                        pass
        finally:
            self._loop_wake = None
            self.ui.clear_status()

    async def shutdown(self, reason: str = "other") -> None:
        await asyncio.to_thread(self.background.kill_all)
        await asyncio.to_thread(self.mcp.stop_all)
        if not self._started:
            return
        self._started = False
        if self.hooks.has("SessionEnd"):
            outcome = await self.hooks.run("SessionEnd", {"reason": reason}, match_value=reason)
            for error in outcome.errors:
                log.debug("SessionEnd hook: %s", error)

    def _reset_workspace_state(self) -> None:
        self.ws.read_state.clear()
        self.ws.checkpoints.clear()
        self.ws.todos = []
        self.ws.notes.clear()
        self.pending_context.clear()

    # ------------------------------------------------------------ sub-agen
    def _sub_agent(self, system_prompt: str, tools: list[Any], hooks: list[Any], name: str) -> Agent:
        """SATU-SATUNYA tempat agen anak dibuat: model selalu core_model (GPT-6 Luna)."""
        return Agent(
            model=self.core_model,
            system_prompt=system_prompt,
            tools=tools,
            callback_handler=None,
            hooks=hooks,
            name=name,
        )

    def _count_sub_usage(self, agent: Agent) -> None:
        usage = agent.event_loop_metrics.accumulated_usage
        self.ws.sub_usage["in"] += sum(int(usage.get(k, 0) or 0) for k in ("inputTokens", "cacheReadInputTokens", "cacheWriteInputTokens"))
        self.ws.sub_usage["out"] += int(usage.get("outputTokens", 0) or 0)

    def _subagent_tools(self, d: AgentDef, depth: int) -> list[Any]:
        tools = []
        for t in self.all_tools():
            name = getattr(t, "tool_name", "")
            if name in MAIN_AGENT_ONLY:
                continue
            if depth >= MAX_SUBAGENT_DEPTH and name in SUBAGENT_SPAWN_TOOLS:
                continue  # batas sarang tercapai
            if d.allows(name):
                tools.append(t)
        return tools

    def _subagent_prompt(self, d: AgentDef) -> str:
        parts = [d.prompt.rstrip(), SUBAGENT_FOOTER.rstrip(), "", environment_block(self.ws.cwd, CORE_MODEL)]
        if not d.builtin_readonly:
            memory = render_memory(load_memory(self.ws.cwd))
            if memory.strip():
                parts += ["", "# Project instructions (MABA.md)", memory.strip()]
        return "\n".join(parts)

    async def run_subagent(self, name: str, task: str, cancel: threading.Event | None) -> str:
        """Jalankan sub-agen di konteks baru (selalu GPT-6 Luna); hanya laporan akhirnya yang kembali."""
        d = self.agent_defs.get(name)
        if d is None:
            raise LookupError(f"Unknown sub-agent '{name}'. Available: {', '.join(sorted(self.agent_defs))}")
        depth = _SUBAGENT_DEPTH.get() + 1
        if d.builtin_readonly:
            policy = PermissionPolicy(self.settings, "default")
        else:
            policy = self.policy  # mode & aturan sesi yang sama dengan agen utama
        frontend = SubagentFrontend(self.ui, d.name, forward=not d.builtin_readonly)
        hook = PermissionHook(policy, self.ws, frontend, hooks=self.hooks, agent_type=d.name)
        sub = self._sub_agent(self._subagent_prompt(d), self._subagent_tools(d, depth), [hook], f"maba-{d.name}")
        token = _SUBAGENT_DEPTH.set(depth)
        try:
            result = await sub.invoke_async(task, cancel_signal=cancel, limits={"turns": d.max_turns})
            report = str(result).strip()
            rounds = 0
            while self.hooks.has("SubagentStop") and str(result.stop_reason) == "end_turn" and rounds < STOP_HOOK_CAP:
                outcome = await self.hooks.run(
                    "SubagentStop",
                    {"agent_type": d.name, "stop_hook_active": rounds > 0, "last_assistant_message": report},
                    match_value=d.name,
                    cancel=cancel,
                )
                self._report_hook(outcome, "SubagentStop")
                if not outcome.blocked or outcome.stop:
                    break
                rounds += 1
                result = await sub.invoke_async(f"[SubagentStop hook] {outcome.reason}", cancel_signal=cancel, limits={"turns": d.max_turns})
                report = str(result).strip()
        finally:
            _SUBAGENT_DEPTH.reset(token)
            self._count_sub_usage(sub)
        if str(result.stop_reason) == "limit_turns":
            report += f"\n\n[sub-agent stopped after its limit of {d.max_turns} turns; the report may be incomplete]"
        return report or "(the sub-agent returned no report)"

    # -------------------------------------------------------------- skills
    def _grant_skill_tools(self, skill: Skill) -> None:
        for text in skill_rules(skill):
            rule = Rule.parse(text)
            if rule is not None:
                self.policy.turn_allow.append(rule)

    async def run_skill(self, name: str, arguments: str, cancel: threading.Event | None) -> str:
        """Dipanggil tool `skill` (oleh model)."""
        skill = self.skills.get(name.strip().lower())
        if skill is None or not skill.model_invocable:
            available = ", ".join(s.name for s in self.skills.values() if s.model_invocable) or "(none)"
            raise LookupError(f"Unknown skill '{name}'. Available: {available}")
        text = expand_skill(skill, arguments)
        if skill.fork:
            return await self.run_subagent(skill.agent, text, cancel)  # sub-agen GPT-6 Luna
        self._grant_skill_tools(skill)
        return text

    async def invoke_skill(self, name: str, arguments: str) -> TurnStats:
        """``/nama args`` oleh pengguna: isi skill dikirim sebagai pesan, izin allowed-tools berlaku giliran ini."""
        skill = self.skills[name]
        self._grant_skill_tools(skill)
        return await self.run_prompt(expand_skill(skill, arguments))

    # ------------------------------------------------------------- web_fetch
    async def web_fetch(self, url: str, prompt: str, cancel: threading.Event | None) -> str:
        """Ambil halaman lalu minta sub-agen GPT-6 Luna (tanpa tool) menjawab ``prompt`` dari isinya."""
        page = await self.web.fetch(url)
        if page.redirect:
            return (
                f"REDIRECT: {url} redirects to a different host: {page.redirect}\n"
                "Call web_fetch again with that URL if you want to follow it."
            )
        content = page.text + ("\n\n[content truncated]" if page.truncated else "")
        task = f'<web-page url="{page.url}" content-type="{page.content_type}">\n{content}\n</web-page>\n\nQuestion: {prompt}'
        sub = self._sub_agent(WEB_FETCH_PROMPT, [], [], "maba-web")
        try:
            result = await sub.invoke_async(task, cancel_signal=cancel, limits={"turns": 1})
        finally:
            self._count_sub_usage(sub)
        return f"Source: {page.url}\n\n{str(result).strip()}"

    # ------------------------------------------------------------ interupsi
    def cancel(self) -> None:
        if self._cancelling:
            return
        self._cancelling = True
        if self._loop_wake is not None:
            self._loop_wake()  # /loop sedang menunggu: cukup bangunkan, jangan sentuh agen yang diam
        if not self._turn_active:
            # Agent.cancel() saat agen diam akan membatalkan giliran BERIKUTNYA, jadi hanya saat giliran berjalan.
            return
        try:
            self.ui.notice("Menghentikan… (menunggu model/tool berhenti)", "yellow")
        except Exception:  # noqa: BLE001 - jangan gagal hanya karena UI
            pass
        self.agent.cancel()

    @contextmanager
    def _interrupts(self) -> Iterator[None]:
        previous = None
        try:
            previous = signal.signal(signal.SIGINT, lambda *_: self.cancel())
        except ValueError:  # bukan thread utama
            previous = None
        self.watcher.start()
        try:
            yield
        finally:
            self.watcher.stop()
            if previous is not None:
                signal.signal(signal.SIGINT, previous)

    # --------------------------------------------------------- giliran agen
    def _usage(self) -> tuple[int, int]:
        tokens_in = tokens_out = 0
        # Agen utama + agen peringkas (Luna) yang dipanggil saat kompaksi otomatis/manual.
        for agent in (self.agent, self.summarizer):
            usage = agent.event_loop_metrics.accumulated_usage
            # Bedrock melaporkan token yang dilayani dari cache terpisah dari inputTokens; "masuk" = ketiganya.
            tokens_in += sum(int(usage.get(k, 0) or 0) for k in ("inputTokens", "cacheReadInputTokens", "cacheWriteInputTokens"))
            tokens_out += int(usage.get("outputTokens", 0) or 0)
        return tokens_in, tokens_out

    def _compose_prompt(self, text: str) -> Any:
        messages = self.agent.messages
        if messages and messages[-1].get("role") == "user":
            # Giliran sebelumnya terputus (Esc/error): sisipkan jawaban asisten agar peran tetap bergantian.
            return [
                {"role": "assistant", "content": [{"text": INTERRUPT_NOTE}]},
                {"role": "user", "content": [{"text": text}]},
            ]
        return text

    def _on_event(self, event: dict[str, Any]) -> None:
        ui = self.ui
        if "data" in event:
            ui.set_phase("write")
            ui.text_delta(event["data"])
        elif event.get("reasoning") and "reasoningText" in event:
            if ui.activity.phase != "think":
                ui.set_phase("think")
        elif event.get("type") == "tool_use_stream":
            name = (event.get("current_tool_use") or {}).get("name")
            if name:
                ui.set_phase("prepare", f"langkah berikutnya ({TOOL_LABELS.get(name, name)})")
        elif "event_loop_throttled_delay" in event:
            ui.notice(f"Bedrock membatasi laju; mencoba lagi dalam {event['event_loop_throttled_delay']}s…", "yellow")

    async def _stream_once(self, prompt: Any, stats: TurnStats, detail: str = "memahami permintaan Anda") -> Any:
        """Satu putaran stream_async. Mengisi stats.error/stop_reason/ctx_pct dan mengembalikan AgentResult."""
        ui = self.ui
        result = None
        ui.set_phase("think", detail)
        self._turn_active = True
        self._model_calls = 0
        gen = self.agent.stream_async(prompt, limits={"turns": self.settings.max_turns})
        try:
            with self._interrupts():
                async for event in gen:
                    self._on_event(event)
                    if "result" in event:
                        result = event["result"]
        except Exception as exc:  # noqa: BLE001 - semua error model/AWS ditampilkan ramah
            log.debug("run_prompt error", exc_info=True)
            title, hint = friendly_error(exc, self.settings)
            stats.error = title
            ui.error(title, hint, session=True)
        finally:
            self._turn_active = False
            ui.clear_status()
            ui.text_end()
            try:
                await gen.aclose()
            except Exception:  # noqa: BLE001
                pass
        stats.stop_reason = str(getattr(result, "stop_reason", "")) if result is not None else ""
        if result is not None:
            ctx = getattr(result, "context_size", None)
            if ctx:
                self.last_ctx_pct = min(999, round(100 * ctx / self.settings.context_window))
            stats.ctx_pct = self.last_ctx_pct
        if stats.stop_reason == "cancelled" or (self._cancelling and result is None):
            stats.cancelled = True
        return result

    def _last_assistant_text(self) -> str:
        for msg in reversed(self.agent.messages):
            if msg.get("role") == "assistant":
                return "\n".join(b.get("text", "") for b in msg.get("content", []) if "text" in b).strip()
        return ""

    async def run_prompt(self, text: str) -> TurnStats:
        ui = self.ui
        self.ws.begin_turn()
        ui.begin_turn()
        self.agent.system_prompt = self._system_prompt()
        if not self.first_prompt:
            self.first_prompt = text.strip().splitlines()[0][:80] if text.strip() else ""
        stats = TurnStats()
        started = time.monotonic()
        before_in, before_out = self._usage()
        sub_before = dict(self.ws.sub_usage)
        self._cancelling = False
        self.collect_background()
        try:
            if self.hooks.has("UserPromptSubmit"):
                outcome = await self.hooks.run("UserPromptSubmit", {"prompt": text})
                self._report_hook(outcome, "UserPromptSubmit")
                if outcome.blocked or outcome.stop:
                    reason = outcome.reason or outcome.stop_reason or "tanpa alasan"
                    ui.notice(f"Prompt diblokir oleh hook UserPromptSubmit: {reason}", "red")
                    stats.blocked = True
                    ui.end_turn()
                    return stats
                if outcome.context_text:
                    self.pending_context.append(f"<user-prompt-submit-hook>\n{outcome.context_text}\n</user-prompt-submit-hook>")
            if self.pending_context:
                text = "\n\n".join(self.pending_context) + "\n\n" + text
                self.pending_context.clear()
            await self._stream_once(self._compose_prompt(text), stats)

            # Hook Stop: boleh memaksa agen melanjutkan (exit 2 / decision:block), dibatasi STOP_HOOK_CAP kali.
            rounds = 0
            while self.hooks.has("Stop") and stats.error is None and not stats.cancelled and stats.stop_reason == "end_turn":
                outcome = await self.hooks.run(
                    "Stop", {"stop_hook_active": rounds > 0, "last_assistant_message": self._last_assistant_text()}
                )
                self._report_hook(outcome, "Stop")
                if not outcome.blocked or outcome.stop:
                    break
                if rounds >= STOP_HOOK_CAP:
                    ui.notice(f"Hook Stop sudah meminta lanjut {STOP_HOOK_CAP} kali; giliran diakhiri.", "yellow")
                    break
                rounds += 1
                ui.notice(f"Hook Stop meminta agen melanjutkan: {outcome.reason}", "cyan")
                await self._stream_once(self._compose_prompt(f"[Stop hook] {outcome.reason}"), stats, "menindaklanjuti permintaan hook Stop")
        finally:
            self.policy.turn_allow.clear()

        after_in, after_out = self._usage()
        stats.tokens_in = after_in - before_in + self.ws.sub_usage["in"] - sub_before["in"]
        stats.tokens_out = after_out - before_out + self.ws.sub_usage["out"] - sub_before["out"]
        self.total_in += stats.tokens_in
        self.total_out += stats.tokens_out
        stats.seconds = time.monotonic() - started
        if stats.error is None:
            outcome = "cancelled" if stats.cancelled else "limit" if stats.stop_reason == "limit_turns" else "done"
            ui.turn_footer(
                stats.tokens_in, stats.tokens_out, stats.ctx_pct, stats.seconds,
                outcome=outcome, limit_turns=self.settings.max_turns,
            )
        self._save_meta()
        ui.end_turn()
        return stats

    async def run_user_shell(self, command: str) -> None:
        """Mode `!perintah`: dijalankan langsung oleh pengguna; hasilnya dijadikan konteks untuk pesan berikutnya."""
        verdict = self.policy.evaluate("run_command", {"command": command}, self.ws)
        if verdict.decision.value == "deny" and "secret" in verdict.reason.lower():
            self.ui.notice("Diblokir: perintah menyentuh file kredensial/rahasia.", "red")
            return
        result = await run_shell(command, str(self.ws.cwd), 120, None)
        text = result.output or "(tanpa output)"
        self.ui.chrome.print(text, markup=False, highlight=False, style="dim")
        status = "ok" if result.ok else f"exit {result.exit_code}"
        self.ui.notice(f"selesai ({status}) · output ditambahkan ke konteks pesan berikutnya", "dim")
        self.pending_context.append(f"<user-shell-command>\n$ {command}\n{text}\n[{status}]\n</user-shell-command>")

    # ----------------------------------------------------------- mode/model
    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"Mode tidak dikenal: {mode}")
        self.policy.mode = mode
        self.agent.system_prompt = self._system_prompt()
        self.ui.mode_changed(mode)

    def cycle_mode(self) -> str:
        current = self.policy.mode
        nxt = CYCLE_MODES[(CYCLE_MODES.index(current) + 1) % len(CYCLE_MODES)] if current in CYCLE_MODES else CYCLE_MODES[0]
        self.set_mode(nxt)
        # Shift+Tab terjadi di dalam prompt: pesan tidak boleh dicetak (merusak tampilan), jadi arti mode
        # baru ditampilkan sesaat di toolbar.
        self._flash = (MODE_HINT[nxt], time.monotonic() + FLASH_SECONDS)
        return nxt

    def set_model(self, name: str) -> str:
        self.settings.model = resolve_model(name)
        self.model = self.model_factory(self.settings)
        self.agent.model = self.model
        self.agent.system_prompt = self._system_prompt()
        return self.settings.model

    # --------------------------------------------------------------- sesi
    def _meta_path(self, session_id: str) -> Path:
        return self.session_dir / "_meta" / f"{session_id}.json"

    def _save_meta(self) -> None:
        path = self._meta_path(self.session_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            old: dict[str, Any] = {}
            if path.exists():
                old = json.loads(path.read_text(encoding="utf-8"))
            meta = {
                "id": self.session_id,
                "title": old.get("title") or self.first_prompt or "(kosong)",
                "cwd": str(self.ws.cwd),
                "model": self.settings.model,
                "created": old.get("created", time.time()),
                "updated": time.time(),
                "messages": len(self.agent.messages),
            }
            path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        except (OSError, ValueError):
            log.debug("gagal menyimpan meta sesi", exc_info=True)

    def list_sessions(self, limit: int = 15) -> list[dict[str, Any]]:
        metas: list[dict[str, Any]] = []
        folder = self.session_dir / "_meta"
        if folder.is_dir():
            for file in folder.glob("*.json"):
                try:
                    metas.append(json.loads(file.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
        metas.sort(key=lambda m: m.get("updated", 0), reverse=True)
        return metas[:limit]

    def new_session(self) -> str:
        self.session_id = new_session_id()
        self.first_prompt = ""
        self._reset_workspace_state()
        self.agent = self._build_agent(self.session_id)
        self.last_ctx_pct = None
        return self.session_id

    def resume_session(self, session_id: str) -> int:
        if not self._meta_path(session_id).exists():
            raise KeyError(session_id)
        self.session_id = session_id
        self._reset_workspace_state()
        self.agent = self._build_agent(session_id)
        meta = json.loads(self._meta_path(session_id).read_text(encoding="utf-8"))
        self.first_prompt = meta.get("title", "")
        self.last_ctx_pct = None
        return len(self.agent.messages)

    def latest_session_id(self) -> str | None:
        sessions = self.list_sessions(1)
        return sessions[0]["id"] if sessions else None

    async def compact(self, focus: str = "") -> tuple[int, int]:
        """Ringkas percakapan lama (tahap 2 kompaksi). Mengembalikan (jumlah pesan sebelum, sesudah)."""
        if self.hooks.has("PreCompact"):
            outcome = await self.hooks.run("PreCompact", {"trigger": "manual", "custom_instructions": focus or None}, "manual")
            self._report_hook(outcome, "PreCompact")
        cm = self.agent.conversation_manager
        before = len(self.agent.messages)
        saved = (cm.summary_ratio, self.summarizer.system_prompt)
        cm.summary_ratio = 0.8
        if focus:
            self.summarizer.system_prompt = f"{DEFAULT_SUMMARIZATION_PROMPT}\n\nPay special attention to: {focus}"
        self._compacting_manually = True
        try:
            await asyncio.to_thread(cm.reduce_context, self.agent)
        finally:
            self._compacting_manually = False
            cm.summary_ratio, self.summarizer.system_prompt = saved
        self.last_ctx_pct = None
        await self.session_start("compact")
        return before, len(self.agent.messages)

    # ------------------------------------------------------------ toolbar
    def readiness(self) -> list[tuple[str, str]]:
        """Catatan kesiapan lokal untuk banner: (level, teks). Tidak menyentuh jaringan."""
        notes: list[tuple[str, str]] = []
        cwd = self.ws.cwd
        if not ((cwd / "MABA.md").is_file() or (cwd / ".maba" / "MABA.md").is_file()):
            notes.append(("info", "Belum ada MABA.md di proyek ini: ketik /init agar agen memahami proyek Anda."))
        if shutil.which("rg") is None:
            notes.append(("warn", "ripgrep (rg) tidak ditemukan: pencarian teks memakai mode Python yang lebih lambat."))
        return notes

    def toolbar_fragments(self, menu: str | None = None, width: int | None = None) -> list[tuple[str, str]]:
        """Toolbar bawah.

        ``menu``: 'slash' | 'file' bila menu autocomplete terbuka (petunjuk tombol ikut berubah).
        ``width``: lebar terminal; bagian yang kurang penting dijatuhkan (token, todo, task) agar petunjuk tombol
        tidak terpotong. Tanpa ``width`` semua bagian ditampilkan.
        """
        mode = self.policy.mode
        sym = getattr(self.ui, "sym", None) or pick_symbols()
        sep = sym.sep
        short_model = self.settings.model.rsplit(".", 1)[-1]
        head: list[tuple[str, str]] = [
            (f"class:tb.mode.{mode.lower()}", f" {MODE_SHORT.get(mode, mode)} "),
            ("class:tb.tag", f" {MODE_TAG.get(mode, '')} "),
            ("class:bottom-toolbar", f"{sep} {short_model} "),
        ]
        pct = self.last_ctx_pct
        if pct is not None:
            style = "tb.ctx.crit" if pct >= CTX_CRIT_PCT else "tb.ctx.warn" if pct >= CTX_WARN_PCT else "bottom-toolbar"
            head.append((f"class:{style}", f"{sep} konteks {meter(pct, 5, sym)} {pct}% "))
        optional: dict[str, tuple[str, str]] = {}  # urutan = dari yang pertama dijatuhkan saat sempit
        if self.total_in or self.total_out:
            optional["tokens"] = ("class:bottom-toolbar", f"{sep} {_k(self.total_in)}↑ {_k(self.total_out)}↓ ")
        todos = self.ws.todos
        if todos:
            done = sum(1 for t in todos if t.get("status") == "completed")
            optional["todo"] = ("class:bottom-toolbar", f"{sep} todo {done}/{len(todos)} ")
        running = len(self.background.running())
        if running:
            optional["tasks"] = ("class:bottom-toolbar", f"{sep} {running} task latar ")

        if self._flash is not None and time.monotonic() < self._flash[1]:
            hints = [("class:tb.flash", f"  {self._flash[0]}")] * 2
        elif menu == "slash":
            hints = [("class:tb.hint", f"  ↑↓ pilih {sep} Tab isi {sep} Enter jalankan {sep} Esc tutup menu"), ("class:tb.hint", "  ↑↓ Tab Enter Esc")]
        elif menu == "file":
            hints = [
                ("class:tb.hint", f"  ↑↓ pilih {sep} Tab/Enter pilih (folder: masuk ke isinya) {sep} Esc tutup menu"),
                ("class:tb.hint", "  ↑↓ Tab/Enter Esc"),
            ]
        else:
            hints = [
                ("class:tb.hint", f"  Shift+Tab ganti mode {sep} / perintah {sep} @ file {sep} Alt+Enter baris baru {sep} /help"),
                ("class:tb.hint", f"  Shift+Tab mode {sep} /help"),
            ]

        def assemble(drop: tuple[str, ...], hint: tuple[str, str]) -> list[tuple[str, str]]:
            return [*head, *(frag for key, frag in optional.items() if key not in drop), hint]

        if not width:
            return assemble((), hints[0])
        order = tuple(optional)
        for hint in hints:
            for count in range(len(order) + 1):
                frags = assemble(order[:count], hint)
                if sum(len(text) for _, text in frags) <= width:
                    return frags
        return assemble(order, hints[1])


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _fmt_seconds(seconds: float) -> str:
    total = int(seconds + 0.999)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def configure_logging() -> None:
    """Strands/boto cukup berisik pada level WARNING; tampilkan hanya bila MABA_DEBUG=1 (ke file, bukan layar)."""
    debug = bool(os.environ.get("MABA_DEBUG"))
    for name in ("strands", "botocore", "boto3", "urllib3", "mcp", "httpx"):
        logging.getLogger(name).setLevel(logging.DEBUG if debug else logging.ERROR)
    if debug:
        folder = maba_home()
        folder.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(folder / "debug.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
