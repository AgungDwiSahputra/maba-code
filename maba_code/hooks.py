"""Hooks pengguna ala Claude Code: perintah shell yang dijalankan harness pada titik-titik siklus agen.

Konfigurasi (HANYA dari ``~/.maba/settings.json`` — file proyek tidak boleh menjalankan perintah)::

    "hooks": {
      "PreToolUse": [
        {"matcher": "run_command|Bash", "hooks": [{"type": "command", "command": "python cek.py", "timeout": 30}]}
      ]
    }

Kontrak (mengikuti dokumentasi publik Claude Code):
  * stdin  : JSON ``{session_id, transcript_path, cwd, permission_mode, hook_event_name, ...}``.
  * exit 0 : sukses; stdout JSON diproses, teks polos menjadi konteks (UserPromptSubmit/SessionStart).
  * exit 2 : blokir; stderr menjadi alasan (dikirim ke model untuk PreToolUse/PostToolUse/Stop).
  * lainnya: error non-blokir (ditampilkan ke pengguna, aksi tetap berjalan).
  * JSON   : ``continue``/``stopReason``, ``decision: "block"``/``reason``, ``systemMessage``,
             ``hookSpecificOutput.{permissionDecision, permissionDecisionReason, updatedInput, additionalContext}``.
Beberapa hook yang cocok berjalan paralel; keputusan paling ketat menang (deny > ask > allow).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from maba_code.shell import run_hook_command

log = logging.getLogger("maba.hooks")

HOOK_EVENTS = (
    "PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop", "SubagentStop",
    "SessionStart", "SessionEnd", "PreCompact",
)
DEFAULT_TIMEOUT = 600.0
SESSION_END_TIMEOUT = 5.0
MAX_CONTEXT_CHARS = 10_000
STOP_HOOK_CAP = 8  # batas berapa kali hook Stop boleh memaksa agen melanjutkan dalam satu giliran
# Event yang menjadikan stdout polos (bukan JSON) sebagai konteks tambahan untuk model.
PLAIN_STDOUT_AS_CONTEXT = frozenset({"UserPromptSubmit", "SessionStart"})
# Nama tool Claude Code -> nama tool MABA, agar matcher yang disalin dari konfigurasi Claude Code tetap jalan.
TOOL_ALIASES = {
    "Bash": "run_command", "Read": "read_file", "Write": "write_file", "Edit": "edit_file",
    "Glob": "glob_files", "Grep": "grep_files", "WebFetch": "web_fetch", "Task": "task", "Agent": "task",
    "Skill": "skill", "TodoWrite": "todo_write", "ExitPlanMode": "exit_plan_mode", "TaskStop": "task_stop",
}
_DECISION_RANK = {"allow": 1, "ask": 2, "deny": 3}
_LIST_MATCHER = re.compile(r"^[\w\s,|-]+$")


@dataclass
class HookCommand:
    command: str
    timeout: float


@dataclass
class HookGroup:
    matcher: str
    commands: list[HookCommand]


@dataclass
class HookOutcome:
    ran: int = 0
    blocked: bool = False
    reason: str = ""
    decision: str | None = None  # PreToolUse: allow | ask | deny
    decision_reason: str = ""
    updated_input: dict[str, Any] | None = None
    context: list[str] = field(default_factory=list)
    stop: bool = False  # continue:false
    stop_reason: str = ""
    messages: list[str] = field(default_factory=list)  # systemMessage untuk pengguna
    errors: list[str] = field(default_factory=list)

    @property
    def context_text(self) -> str:
        return "\n\n".join(c for c in self.context if c.strip())[:MAX_CONTEXT_CHARS]


def matcher_matches(matcher: str | None, value: str) -> bool:
    """Kosong/``*`` = semua; huruf/angka/``|``/``,`` = daftar nama persis; selain itu regex (tidak berjangkar)."""
    m = (matcher or "").strip()
    if m in ("", "*"):
        return True
    if _LIST_MATCHER.match(m):
        names = {part.strip() for part in re.split(r"[|,]", m) if part.strip()}
        names |= {TOOL_ALIASES.get(n, n) for n in names}
        return value in names
    try:
        return re.search(m, value) is not None
    except re.error:
        return False


def parse_hooks(config: dict[str, Any], warnings: list[str]) -> dict[str, list[HookGroup]]:
    parsed: dict[str, list[HookGroup]] = {}
    for event, groups in (config or {}).items():
        if event not in HOOK_EVENTS:
            warnings.append(f"hooks: event '{event}' tidak didukung (didukung: {', '.join(HOOK_EVENTS)})")
            continue
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            commands: list[HookCommand] = []
            for spec in group.get("hooks") or []:
                if not isinstance(spec, dict):
                    continue
                if spec.get("type", "command") != "command":
                    warnings.append(f"hooks.{event}: tipe '{spec.get('type')}' belum didukung (hanya 'command')")
                    continue
                command = str(spec.get("command") or "").strip()
                if not command:
                    continue
                default = SESSION_END_TIMEOUT if event == "SessionEnd" else DEFAULT_TIMEOUT
                try:
                    timeout = float(spec.get("timeout") or default)
                except (TypeError, ValueError):
                    timeout = default
                commands.append(HookCommand(command, max(1.0, timeout)))
            if commands:
                parsed.setdefault(event, []).append(HookGroup(str(group.get("matcher") or ""), commands))
    return parsed


class HookRunner:
    def __init__(
        self,
        config: dict[str, Any],
        cwd: Path,
        base_payload: Callable[[], dict[str, Any]],
        warnings: list[str] | None = None,
    ):
        self.cwd = cwd
        self.base_payload = base_payload
        self.groups = parse_hooks(config, warnings if warnings is not None else [])

    def has(self, event: str) -> bool:
        return bool(self.groups.get(event))

    def _select(self, event: str, match_value: str | None) -> list[HookCommand]:
        seen: set[str] = set()
        selected: list[HookCommand] = []
        for group in self.groups.get(event, []):
            # Seperti Claude Code: UserPromptSubmit & Stop mengabaikan matcher.
            if match_value is not None and event not in ("UserPromptSubmit", "Stop", "SubagentStop"):
                if not matcher_matches(group.matcher, match_value):
                    continue
            for cmd in group.commands:
                if cmd.command not in seen:  # handler identik hanya dijalankan sekali
                    seen.add(cmd.command)
                    selected.append(cmd)
        return selected

    async def run(
        self,
        event: str,
        payload: dict[str, Any] | None = None,
        match_value: str | None = None,
        cancel: threading.Event | None = None,
    ) -> HookOutcome:
        outcome = HookOutcome()
        commands = self._select(event, match_value)
        if not commands:
            return outcome
        data = {**self.base_payload(), "hook_event_name": event, **(payload or {})}
        stdin = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        env = {"MABA_PROJECT_DIR": str(self.cwd), "CLAUDE_PROJECT_DIR": str(self.cwd)}
        results = await asyncio.gather(
            *(run_hook_command(c.command, str(self.cwd), stdin, c.timeout, cancel, env) for c in commands),
            return_exceptions=True,
        )
        for cmd, result in zip(commands, results):
            outcome.ran += 1
            label = cmd.command if len(cmd.command) <= 60 else cmd.command[:59] + "…"
            if isinstance(result, BaseException):
                log.debug("hook gagal dijalankan", exc_info=result)
                outcome.errors.append(f"{label}: {type(result).__name__}: {result}")
                continue
            if result.cancelled:
                outcome.errors.append(f"{label}: dibatalkan")
                continue
            if result.timed_out:
                outcome.errors.append(f"{label}: timeout {cmd.timeout:.0f}s")
                continue
            if result.exit_code == 2:
                outcome.blocked = True
                _append_reason(outcome, result.stderr or f"Blocked by {event} hook.")
                continue
            if result.exit_code != 0:
                detail = (result.stderr or result.stdout).splitlines()[:1]
                outcome.errors.append(f"{label}: exit {result.exit_code}" + (f" — {detail[0][:160]}" if detail else ""))
                continue
            self._apply_stdout(event, result.stdout, outcome)
        return outcome

    def run_sync(self, event: str, payload: dict[str, Any] | None = None, match_value: str | None = None) -> HookOutcome:
        """Untuk titik sinkron (kompaksi otomatis berjalan di dalam loop Strands): jalankan di thread sendiri."""
        if not self._select(event, match_value):
            return HookOutcome()
        box: list[HookOutcome] = []
        thread = threading.Thread(target=lambda: box.append(asyncio.run(self.run(event, payload, match_value))), daemon=True)
        thread.start()
        thread.join()
        return box[0] if box else HookOutcome(errors=[f"{event}: hook gagal dijalankan"])

    @staticmethod
    def _apply_stdout(event: str, stdout: str, outcome: HookOutcome) -> None:
        text = stdout.strip()
        if not text:
            return
        data: Any = None
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except ValueError:
                data = None
        if not isinstance(data, dict):
            if event in PLAIN_STDOUT_AS_CONTEXT:
                outcome.context.append(text)
            return
        if data.get("continue") is False:
            outcome.stop = True
            outcome.stop_reason = str(data.get("stopReason") or "")
        if data.get("systemMessage"):
            outcome.messages.append(str(data["systemMessage"]))
        decision = str(data.get("decision") or "").lower()
        if decision == "block":
            outcome.blocked = True
            _append_reason(outcome, str(data.get("reason") or f"Blocked by {event} hook."))
        elif decision == "approve":  # bentuk lama
            _merge_decision(outcome, "allow", str(data.get("reason") or ""))
        specific = data.get("hookSpecificOutput")
        if isinstance(specific, dict):
            perm = str(specific.get("permissionDecision") or "").lower()
            if perm in _DECISION_RANK:
                _merge_decision(outcome, perm, str(specific.get("permissionDecisionReason") or ""))
            if isinstance(specific.get("updatedInput"), dict):
                outcome.updated_input = specific["updatedInput"]
            if specific.get("additionalContext"):
                outcome.context.append(str(specific["additionalContext"]))


def _append_reason(outcome: HookOutcome, reason: str) -> None:
    reason = reason.strip()
    outcome.reason = f"{outcome.reason}\n{reason}".strip() if outcome.reason else reason


def _merge_decision(outcome: HookOutcome, decision: str, reason: str) -> None:
    if outcome.decision is None or _DECISION_RANK[decision] > _DECISION_RANK[outcome.decision]:
        outcome.decision = decision
        outcome.decision_reason = reason
    elif decision == outcome.decision and reason:
        outcome.decision_reason = f"{outcome.decision_reason}\n{reason}".strip()
