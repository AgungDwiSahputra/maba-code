"""Mesin izin MABA Code.

Model (meniru perilaku terdokumentasi Claude Code):
  * Evaluasi berurutan: deny  ->  ask  ->  allow. Aturan allow tidak bisa membatalkan deny.
  * Tiga kelas tool: baca (otomatis), tulis (tanya), berbahaya = run_command (tanya).
  * Mode: default | acceptEdits | plan | bypass.
  * Penegakan dilakukan oleh harness (hook Strands), bukan oleh model.

Catatan keamanan: daftar pola kredensial & operator shell adalah pagar tambahan (best-effort),
bukan sandbox. Perlindungan sebenarnya adalah izin IAM profile yang minimal.
"""

from __future__ import annotations

import fnmatch
import json
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry

from maba_code import fsops
from maba_code.config import Settings
from maba_code.tools import DANGER_TOOLS, MCP_PREFIX, PLAN_TOOL, WRITE_TOOLS, tool_class
from maba_code.workspace import ToolNote, Workspace

PATH_TOOLS = frozenset({"read_file", "write_file", "edit_file", "glob_files", "grep_files"})
READ_PATH_TOOLS = frozenset({"read_file", "glob_files", "grep_files"})
MEMORY_REASON = "memori otomatis"
MAX_MCP_RESULT_CHARS = 100_000

PLAN_MODE_MESSAGE = (
    "Plan mode is active: only read-only tools are allowed. Finish exploring, then call "
    "exit_plan_mode with your plan so the user can approve it."
)
SENSITIVE_MESSAGE = "Blocked by policy: this path/command touches credential or secret files."

_SHELL_OPS = re.compile(r"[;&|<>`\r\n]|\$\(|%\w+%")
_CRED_CMD = re.compile(
    r"(\.aws[\\/]+(credentials|config|sso)|\.ssh[\\/]|\bid_(rsa|ed25519|ecdsa)\b"
    r"|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|aws\s+configure\s+(get|export-credentials)"
    r"|aws\s+sts\s+get-session-token|(^|[\s\\/'\"])\.env(\.[\w.-]+)?(?=$|[\s'\";|&)]))",
    re.IGNORECASE,
)
_SAFE_CMD = re.compile(
    r"^(git\s+(status|log|diff|show|rev-parse|ls-files)(\s|$)"
    r"|git\s+remote\s+-v$"
    r"|git\s+branch(\s+(--show-current|-a|-v|--list))?$"  # 'git branch -D x' sengaja TIDAK cocok
    r"|ls(\s|$)|dir(\s|$)|pwd$|get-location$|get-childitem(\s|$)|gci(\s|$)"
    r"|(where|where\.exe|which)\s+\S+$"
    r"|(python3?|node|dotnet|npm|java)\s+(--version|-v)$"
    r"|pip\s+(list|show)(\s|$))",
    re.IGNORECASE,
)
_SAFE_CMD_BANNED = ("--output", "--ext-diff", "--textconv", "-o ")
# Perintah yang tidak pernah diberi aturan wildcard otomatis (interpreter & destruktif).
_NO_WILDCARD = frozenset(
    "python python3 py node powershell pwsh cmd bash sh wsl ruby perl php java deno bun npx "
    "rm del erase rd rmdir remove-item ri mv move rename-item format dd curl wget invoke-webrequest iwr "
    "invoke-expression iex start-process".split()
)


_SUBCOMMAND_CLIS = frozenset(
    "git npm yarn pnpm dotnet docker kubectl aws pip pip3 cargo go gh az terraform helm poetry uv nuget".split()
)


class Decision(Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass
class Verdict:
    decision: Decision
    reason: str = ""


@dataclass(frozen=True)
class Rule:
    tool: str
    pattern: str | None = None

    _RE = re.compile(r"^\s*([\w*-]+)\s*(?:\((.*)\))?\s*$")

    @classmethod
    def parse(cls, text: str) -> Rule | None:
        m = cls._RE.match(text)
        if not m:
            return None
        return cls(m.group(1), m.group(2) if m.group(2) not in (None, "") else None)

    def __str__(self) -> str:
        return f"{self.tool}({self.pattern})" if self.pattern is not None else self.tool

    def tool_matches(self, tool: str) -> bool:
        if self.tool in ("*", tool):
            return True
        # "mcp__server" (tanpa nama tool) berlaku untuk semua tool server itu, seperti di Claude Code.
        if self.tool.startswith(MCP_PREFIX) and self.tool.count("__") == 1:
            return tool.startswith(self.tool + "__")
        return "*" in self.tool and fnmatch.fnmatchcase(tool, self.tool)

    def matches(self, tool: str, candidates: list[str]) -> bool:
        if not self.tool_matches(tool):
            return False
        if self.pattern is None:
            return True
        pat = self.pattern.replace("\\", "/").lower() if tool in PATH_TOOLS else self.pattern.lower()
        for cand in candidates:
            value = cand.replace("\\", "/").lower() if tool in PATH_TOOLS else cand.lower()
            if fnmatch.fnmatchcase(value, pat):
                return True
        return False


def primary_arg(tool: str, inp: dict[str, Any]) -> str:
    if tool == "run_command":
        return str(inp.get("command", ""))
    if tool in PATH_TOOLS:
        return str(inp.get("path") or ".")
    if tool == "explore":
        return str(inp.get("task", ""))
    if tool == "task":
        return str(inp.get("prompt", ""))
    if tool == "skill":
        return str(inp.get("name", ""))
    if tool == "web_fetch":
        return str(inp.get("url", ""))
    return ""


def url_host(url: str) -> str:
    try:
        return (urlsplit(url.strip()).hostname or "").lower()
    except ValueError:
        return ""


def has_shell_operators(command: str) -> bool:
    return bool(_SHELL_OPS.search(command))


def is_safe_readonly_command(command: str) -> bool:
    cmd = command.strip()
    if not cmd or has_shell_operators(cmd):
        return False
    if any(flag in cmd.lower() for flag in _SAFE_CMD_BANNED):
        return False
    return bool(_SAFE_CMD.match(cmd))


def _command_fragments(command: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"[;&|\r\n]+", command) if p.strip()]
    return [command.strip(), *parts]


class PermissionPolicy:
    def __init__(self, settings: Settings, mode: str | None = None):
        self.mode = mode or settings.mode
        self.allow = [r for r in map(Rule.parse, settings.allow) if r]
        self.ask = [r for r in map(Rule.parse, settings.ask) if r]
        self.deny = [r for r in map(Rule.parse, settings.deny) if r]
        self.session_allow: list[Rule] = []
        # Aturan allow yang hanya berlaku sampai giliran berjalan selesai (allowed-tools milik skill pengguna).
        self.turn_allow: list[Rule] = []

    # ------------------------------------------------------------ helpers
    def _path_arg(self, tool: str, inp: dict[str, Any], ws: Workspace) -> Path | None:
        if tool not in PATH_TOOLS:
            return None
        return ws.resolve(primary_arg(tool, inp))

    def _candidates(self, tool: str, inp: dict[str, Any], ws: Workspace) -> list[str]:
        if tool == "run_command":
            return _command_fragments(primary_arg(tool, inp))
        if tool == "web_fetch":
            url = primary_arg(tool, inp)
            return [f"domain:{url_host(url)}", url]
        path = self._path_arg(tool, inp, ws)
        if path is not None:
            return [ws.display(path), path.as_posix()]
        return [primary_arg(tool, inp)]

    def _guard_reason(self, tool: str, inp: dict[str, Any], ws: Workspace) -> str | None:
        path = self._path_arg(tool, inp, ws)
        if path is None:
            return None
        if not ws.inside(path):
            if tool in READ_PATH_TOOLS and ws.in_trusted_root(path):
                return None
            return "di luar folder kerja"
        if tool in WRITE_TOOLS and ws.is_protected(path):
            return "path terlindungi (.git / .maba / .vscode / .idea)"
        return None

    # ----------------------------------------------------------- evaluate
    def evaluate(self, tool: str, inp: dict[str, Any] | None, ws: Workspace) -> Verdict:
        inp = inp if isinstance(inp, dict) else {}
        command = primary_arg(tool, inp) if tool == "run_command" else ""
        path = self._path_arg(tool, inp, ws)

        # 1. Pagar rahasia (tidak bisa dilewati, termasuk di mode bypass).
        if path is not None and fsops.is_sensitive_path(path):
            return Verdict(Decision.DENY, SENSITIVE_MESSAGE)
        if command:
            scrubbed = re.sub(r"\.env\.(example|sample|template|dist)", "", command, flags=re.IGNORECASE)
            if _CRED_CMD.search(scrubbed):
                return Verdict(Decision.DENY, SENSITIVE_MESSAGE)

        candidates = self._candidates(tool, inp, ws)

        # 2. Aturan deny.
        for rule in self.deny:
            if rule.matches(tool, candidates):
                return Verdict(Decision.DENY, f"Denied by permission rule {rule}.")

        # 3. Plan mode.
        if self.mode == "plan":
            if tool == PLAN_TOOL:
                return Verdict(Decision.ASK, "Persetujuan rencana")
            if tool in WRITE_TOOLS or (tool in DANGER_TOOLS and not is_safe_readonly_command(command)):
                return Verdict(Decision.DENY, PLAN_MODE_MESSAGE)
        elif tool == PLAN_TOOL:
            return Verdict(Decision.ALLOW)  # tool sendiri akan menolak bila bukan plan mode

        # 4. Aturan ask.
        for rule in self.ask:
            if rule.matches(tool, candidates):
                return Verdict(Decision.ASK, f"aturan ask: {rule}")

        # 5. Memori otomatis: agen boleh membaca/menulis catatan .md di folder memorinya sendiri.
        if path is not None and path.suffix.lower() == ".md" and ws.in_memory_dir(path):
            return Verdict(Decision.ALLOW, MEMORY_REASON)

        # 6. Bypass.
        if self.mode == "bypass":
            return Verdict(Decision.ALLOW)

        # 7. Pagar path: hanya aturan allow BERPOLA yang boleh melewatinya.
        guard = self._guard_reason(tool, inp, ws)
        rules = [*self.allow, *self.session_allow, *self.turn_allow]
        if guard:
            if any(r.pattern is not None and r.matches(tool, candidates) for r in rules):
                return Verdict(Decision.ALLOW)
            return Verdict(Decision.ASK, guard)

        # 8. Aturan allow.
        for rule in rules:
            if rule.pattern is None and rule.matches(tool, candidates):
                return Verdict(Decision.ALLOW)
            if rule.pattern is not None and rule.matches(tool, candidates):
                if tool != "run_command" or not has_shell_operators(command):
                    return Verdict(Decision.ALLOW)

        # 9. Default per kelas tool.
        kind = tool_class(tool)
        if kind == "read":
            return Verdict(Decision.ALLOW)
        if kind == "write":
            return Verdict(Decision.ALLOW) if self.mode == "acceptEdits" else Verdict(Decision.ASK, "menulis file")
        if kind == "danger":
            if is_safe_readonly_command(command) and not inp.get("run_in_background"):
                return Verdict(Decision.ALLOW)
            return Verdict(Decision.ASK, "menjalankan perintah shell")
        if kind == "network":
            return Verdict(Decision.ASK, "mengakses internet")
        if kind == "mcp":
            return Verdict(Decision.ASK, "tool server MCP")
        return Verdict(Decision.ASK, "tool tidak dikenal")

    # ------------------------------------------------ "jangan tanya lagi"
    def _remember_target(self, tool: str, inp: dict[str, Any], ws: Workspace) -> tuple[str, Rule | None]:
        """Tentukan efek 'izinkan selama sesi': ('rule', Rule) | ('mode', None) | ('none', None)."""
        guard = self._guard_reason(tool, inp, ws)
        if tool == "run_command":
            return "rule", self._command_rule(primary_arg(tool, inp))
        if tool == "web_fetch":
            host = url_host(primary_arg(tool, inp))
            return ("rule", Rule(tool, f"domain:{host}")) if host else ("none", None)
        if guard:
            path = self._path_arg(tool, inp, ws)
            if path is None or "terlindungi" in guard:
                return "none", None
            folder = path if path.is_dir() else path.parent
            return "rule", Rule(tool, f"{folder.as_posix()}/**")
        if tool in WRITE_TOOLS:
            return "mode", None
        return "rule", Rule(tool)

    def remember_label(self, tool: str, inp: dict[str, Any], ws: Workspace) -> str:
        """Teks opsi 'a' pada dialog izin ('' bila opsi tidak tersedia)."""
        kind, rule = self._remember_target(tool, inp, ws)
        if kind == "none":
            return ""
        if kind == "mode":
            return "Ya, terima semua edit file selama sesi ini"
        return f"Ya, jangan tanya lagi untuk `{rule}` (sesi ini)"

    def remember(self, tool: str, inp: dict[str, Any], ws: Workspace) -> str:
        """Catat persetujuan 'sesi ini' dan kembalikan penjelasan untuk pengguna."""
        kind, rule = self._remember_target(tool, inp, ws)
        if kind == "none":
            return "Hanya untuk kali ini (path terlindungi tidak bisa diizinkan permanen)."
        if kind == "mode":
            self.mode = "acceptEdits"
            return "Mode diubah ke acceptEdits: edit file di folder kerja kini otomatis."
        assert rule is not None
        if rule not in self.session_allow:
            self.session_allow.append(rule)
        return f"Aturan sesi ditambahkan: {rule}"

    @staticmethod
    def _command_rule(command: str) -> Rule:
        cmd = command.strip()
        tokens = cmd.split()
        if not tokens or has_shell_operators(cmd) or tokens[0].lower() in _NO_WILDCARD:
            return Rule("run_command", cmd)
        head = tokens[0]
        # Hanya CLI ber-subcommand yang diberi prefiks 2 kata ("git commit *"); selain itu 1 kata ("pytest *").
        if tokens[0].lower() in _SUBCOMMAND_CLIS and len(tokens) > 1 and re.fullmatch(r"[A-Za-z][\w:-]*", tokens[1]):
            head = f"{tokens[0]} {tokens[1]}"
        return Rule("run_command", f"{head} *" if len(tokens) > len(head.split()) else head)


# ----------------------------------------------------------------------- UI contract
@dataclass
class PermissionRequest:
    tool: str
    title: str
    target: str
    detail: str = ""
    kind: str = "text"  # "diff" | "command" | "plan" | "text"
    reason: str = ""
    danger: bool = False
    session_label: str = ""  # teks opsi "jangan tanya lagi"; kosong = opsi disembunyikan
    impact: str = ""  # satu kalimat: apa yang terjadi bila disetujui (untuk pengguna, bukan model)
    mode: str = ""  # mode izin aktif saat dialog muncul
    intent: str = ""  # kutipan kalimat terakhir agen sebelum memanggil tool; diisi oleh UI bila ada


@dataclass
class PermissionAnswer:
    choice: str  # "allow" | "allow_session" | "deny"
    comment: str = ""


class Frontend(Protocol):
    async def ask_permission(self, request: PermissionRequest) -> PermissionAnswer: ...
    async def ask_plan(self, plan: str) -> str: ...  # "acceptEdits" | "default" | "keep"
    def tool_started(self, name: str, inp: dict[str, Any]) -> None: ...
    def tool_denied(self, name: str, inp: dict[str, Any], reason: str) -> None: ...
    def tool_finished(
        self, name: str, inp: dict[str, Any], ok: bool, text: str, duration: float, note: ToolNote | None
    ) -> None: ...
    def notice(self, message: str, style: str = "") -> None: ...
    def mode_changed(self, mode: str) -> None: ...


class NullFrontend:
    """Frontend senyap untuk sub-agen: semua permintaan izin otomatis ditolak."""

    async def ask_permission(self, request: PermissionRequest) -> PermissionAnswer:
        return PermissionAnswer("deny", "Sub-agent runs read-only; this action is not allowed.")

    async def ask_plan(self, plan: str) -> str:
        return "keep"

    def tool_started(self, name: str, inp: dict[str, Any]) -> None: ...
    def tool_denied(self, name: str, inp: dict[str, Any], reason: str) -> None: ...
    def tool_finished(self, name, inp, ok, text, duration, note) -> None: ...
    def notice(self, message: str, style: str = "") -> None: ...
    def mode_changed(self, mode: str) -> None: ...


def _result_text(result: dict[str, Any]) -> str:
    parts = []
    for block in result.get("content", []):
        if isinstance(block, dict) and "text" in block:
            parts.append(str(block["text"]))
    return "\n".join(parts)


class PermissionHook(HookProvider):
    """Menahan setiap tool call sampai kebijakan/pengguna mengizinkan, lalu melaporkan hasilnya ke UI."""

    def __init__(self, policy: PermissionPolicy, ws: Workspace, ui: Frontend, hooks: Any = None, agent_type: str | None = None):
        self.policy = policy
        self.ws = ws
        self.ui = ui
        self.hooks = hooks  # hooks.HookRunner (opsional)
        self.agent_type = agent_type  # nama sub-agen, untuk payload hook
        self._started: dict[str, float] = {}
        self._blocked: set[str] = set()

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before)
        registry.add_callback(AfterToolCallEvent, self._after)

    # ----------------------------------------------------------------- helpers
    def _lock(self):
        # Lock milik Workspace dibagi agen utama & semua sub-agen: dialog tidak pernah tumpang tindih.
        return self.ws.dialog_lock

    def build_request(self, tool: str, inp: dict[str, Any], reason: str) -> PermissionRequest | None:
        """Susun isi dialog. Return None bila aksi pasti ditolak tool sendiri (tak perlu mengganggu pengguna)."""
        request = self._build_request(tool, inp, reason)
        if request is not None:
            request.session_label = self.policy.remember_label(tool, inp, self.ws)
            request.mode = self.policy.mode
        return request

    def _build_request(self, tool: str, inp: dict[str, Any], reason: str) -> PermissionRequest | None:
        ws = self.ws
        if tool == "run_command":
            title = "Jalankan perintah (latar belakang)" if inp.get("run_in_background") else "Jalankan perintah"
            impact = (
                "Perintah berjalan di komputer Anda (shell) pada folder kerja; hasilnya dikirim kembali ke agen."
                + (" Berjalan di latar belakang sampai selesai atau dihentikan (/tasks)." if inp.get("run_in_background") else "")
            )
            return PermissionRequest(
                tool, title, str(inp.get("description") or "shell"), str(inp.get("command", "")),
                "command", reason, danger=True, impact=impact,
            )
        if tool == "web_fetch":
            return PermissionRequest(
                tool, "Ambil halaman web", str(inp.get("url", "")), str(inp.get("prompt", "")), "text", reason,
                impact="Mengunduh halaman dari internet; isinya diringkas model lalu dikirim ke agen.",
            )
        if tool not in PATH_TOOLS:
            detail = json.dumps(inp, ensure_ascii=False, indent=1)
            if len(detail) > 1500:
                detail = detail[:1500] + "\n…"
            is_mcp = tool_class(tool) == "mcp"
            impact = (
                "Memanggil tool dari server MCP eksternal; argumen di atas dikirim ke server itu."
                if is_mcp else "Menjalankan tool yang tidak dikenal harness; periksa argumennya dulu."
            )
            return PermissionRequest(tool, "Jalankan tool", tool, detail, "text", reason, danger=is_mcp, impact=impact)
        path = ws.resolve(primary_arg(tool, inp))
        label = ws.display(path)
        where = "" if ws.inside(path) else " Berkas ini DI LUAR folder kerja."
        undo = " Bisa dibatalkan dengan /undo."
        if tool == "edit_file":
            if not path.is_file() or not ws.was_read_fresh(path):
                return None
            try:
                tf = fsops.decode_text(path.read_bytes())
            except (OSError, fsops.NotUtf8Error):
                return None
            plan = fsops.plan_edit(
                tf.text, str(inp.get("old_string", "")), str(inp.get("new_string", "")), bool(inp.get("replace_all"))
            )
            if plan.error or plan.new_text is None:
                return None
            diff = fsops.unified_diff(tf.text, plan.new_text, label)
            return PermissionRequest(tool, "Edit file", label, diff, "diff", reason, impact=f"Mengubah isi file yang sudah ada.{where}{undo}")
        if tool == "write_file":
            content = str(inp.get("content", ""))
            if path.is_file():
                if not ws.was_read_fresh(path):
                    return None
                try:
                    old = fsops.decode_text(path.read_bytes()).text
                except (OSError, fsops.NotUtf8Error):
                    return None
                return PermissionRequest(
                    tool, "Timpa file", label, fsops.unified_diff(old, content, label), "diff", reason,
                    impact=f"Menimpa seluruh isi file yang sudah ada.{where}{undo}",
                )
            lines = content.splitlines()
            preview = "\n".join(f"+{ln}" for ln in lines[:60])
            if len(lines) > 60:
                preview += f"\n+... ({len(lines) - 60} baris lagi)"
            return PermissionRequest(
                tool, "Buat file baru", label, preview, "diff", reason, impact=f"Membuat file baru.{where}{undo}"
            )
        return PermissionRequest(tool, tool, label, "", "text", reason)

    # ------------------------------------------------------------------ hooks
    def _hook_payload(self, name: str, inp: dict[str, Any], tool_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"tool_name": name, "tool_input": inp, "tool_use_id": tool_id}
        if name.startswith(MCP_PREFIX):
            payload["mcp_server"] = name.split("__")[1] if name.count("__") >= 2 else ""
        if self.agent_type:
            payload["agent_type"] = self.agent_type
        return payload

    def _report_hook(self, outcome: Any, event_name: str) -> None:
        for message in outcome.messages:
            self.ui.notice(f"[hook {event_name}] {message}", "cyan")
        for error in outcome.errors:
            self.ui.notice(f"Hook {event_name} error (diabaikan): {error}", "yellow")

    def _block(self, event: BeforeToolCallEvent, tool_id: str, name: str, inp: dict[str, Any], message: str, shown: str) -> None:
        event.cancel_tool = message
        self._blocked.add(tool_id)
        self.ui.tool_denied(name, inp, shown)

    async def _before(self, event: BeforeToolCallEvent) -> None:
        if event.selected_tool is None:
            return
        tool_id = str(event.tool_use.get("toolUseId", ""))
        name = event.tool_use["name"]
        inp = event.tool_use.get("input") or {}

        # Hook PreToolUse berjalan SEBELUM kebijakan; ia bisa memblokir, meminta dialog, atau mengubah input.
        hook_decision: str | None = None
        if self.hooks is not None and self.hooks.has("PreToolUse"):
            outcome = await self.hooks.run("PreToolUse", self._hook_payload(name, inp, tool_id), match_value=name)
            self._report_hook(outcome, "PreToolUse")
            if outcome.stop:
                self._block(event, tool_id, name, inp, f"Stopped by PreToolUse hook. {outcome.stop_reason}".strip(), "dihentikan hook")
                event.agent.cancel()
                return
            if outcome.blocked or outcome.decision == "deny":
                reason = outcome.reason or outcome.decision_reason or "no reason given"
                self._block(event, tool_id, name, inp, f"Blocked by PreToolUse hook: {reason}", f"diblokir hook: {reason}")
                return
            if outcome.updated_input is not None:
                inp = outcome.updated_input
                event.tool_use = {**event.tool_use, "input": inp}
            hook_decision = outcome.decision

        verdict = self.policy.evaluate(name, inp, self.ws)
        if verdict.decision is not Decision.DENY and name != PLAN_TOOL:
            # Hook hanya bisa MELONGGARKAN ask -> allow, atau MEMPERKETAT allow -> ask. Pagar rahasia,
            # aturan deny, dan plan mode sudah diputuskan di atas dan tidak bisa dilewati hook.
            if hook_decision == "allow" and verdict.decision is Decision.ASK:
                verdict = Verdict(Decision.ALLOW, "diizinkan hook")
            elif hook_decision == "ask":
                verdict = Verdict(Decision.ASK, "diminta hook PreToolUse")
        if verdict.reason == MEMORY_REASON and name in WRITE_TOOLS:
            self.ui.notice(f"Menyimpan memori otomatis: {primary_arg(name, inp)}", "dim")

        if verdict.decision is Decision.DENY:
            event.cancel_tool = verdict.reason
            self._blocked.add(tool_id)
            self.ui.tool_denied(name, inp, verdict.reason)
            return

        if verdict.decision is Decision.ASK:
            if name == PLAN_TOOL:
                async with self._lock():
                    choice = await self.ui.ask_plan(str(inp.get("plan", "")))
                if choice == "keep":
                    event.cancel_tool = "The user did not approve the plan. Revise it based on their feedback and call exit_plan_mode again."
                    self._blocked.add(tool_id)
                    self.ui.tool_denied(name, inp, "rencana belum disetujui")
                    return
                self.policy.mode = choice
                self.ws.plan_approved = True
                self.ui.mode_changed(choice)
            else:
                request = self.build_request(name, inp, verdict.reason)
                if request is not None:
                    async with self._lock():
                        answer = await self.ui.ask_permission(request)
                    if answer.choice == "deny":
                        suffix = f" Reason from user: {answer.comment}" if answer.comment else ""
                        event.cancel_tool = f"The user denied this {name} call.{suffix} Do not retry the same action; ask or adapt."
                        self._blocked.add(tool_id)
                        self.ui.tool_denied(name, inp, answer.comment or "ditolak pengguna")
                        return
                    if answer.choice == "allow_session":
                        message = self.policy.remember(name, inp, self.ws)
                        self.ui.notice(message, "dim")
                        self.ui.mode_changed(self.policy.mode)

        self._started[tool_id] = time.monotonic()
        self.ui.tool_started(name, inp)

    async def _after(self, event: AfterToolCallEvent) -> None:
        tool_id = str(event.tool_use.get("toolUseId", ""))
        note = self.ws.pop_note(tool_id)
        if tool_id in self._blocked:
            self._blocked.discard(tool_id)
            return
        started = self._started.pop(tool_id, None)
        duration = time.monotonic() - started if started else 0.0
        result = event.result or {}
        name = event.tool_use["name"]
        inp = event.tool_use.get("input") or {}
        ok = result.get("status") != "error" and event.exception is None
        if name.startswith(MCP_PREFIX) and len(_result_text(result)) > MAX_MCP_RESULT_CHARS:
            result = {**result, "content": [{"text": fsops.truncate_middle(_result_text(result), MAX_MCP_RESULT_CHARS)}]}
            event.result = result
        text = _result_text(result)
        if event.exception is not None and not text:
            text = f"{type(event.exception).__name__}: {event.exception}"
        self.ui.tool_finished(name, inp, ok, text, duration, note)

        if self.hooks is not None and self.hooks.has("PostToolUse"):
            payload = {**self._hook_payload(name, inp, tool_id), "tool_response": text}
            outcome = await self.hooks.run("PostToolUse", payload, match_value=name)
            self._report_hook(outcome, "PostToolUse")
            extra = []
            if outcome.blocked:
                extra.append(f"[PostToolUse hook feedback] {outcome.reason}")
            if outcome.context_text:
                extra.append(f"[PostToolUse hook context] {outcome.context_text}")
            if extra:
                event.result = {**result, "content": [*result.get("content", []), {"text": "\n".join(extra)}]}
            if outcome.stop:
                event.agent.cancel()
