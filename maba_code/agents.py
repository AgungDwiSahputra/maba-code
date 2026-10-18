"""Sub-agen: bawaan (explore, plan, general-purpose) + definisi markdown pengguna/proyek.

Format file (``~/.maba/agents/*.md`` atau ``.maba/agents/*.md``, mengikuti Claude Code)::

    ---
    name: reviewer
    description: Meninjau diff untuk bug & keamanan. Gunakan setelah perubahan kode.
    tools: read_file, grep_files, glob_files      # opsional (allowlist; nama Claude Code juga diterima)
    disallowedTools: run_command                  # opsional
    maxTurns: 20                                  # opsional
    ---
    Instruksi sistem untuk sub-agen…

Field ``model`` diabaikan: setiap sub-agen MABA berjalan di atas GPT-6 Luna (CORE_MODEL).
File pengguna menang atas file proyek bernama sama. ``tools`` hanya MEMPERSEMPIT; setiap panggilan tool
sub-agen tetap melewati kebijakan izin yang sama dengan agen utama.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

from maba_code.config import CORE_MODEL, maba_home, resolve_model
from maba_code.frontmatter import as_list, split_frontmatter
from maba_code.hooks import TOOL_ALIASES
from maba_code.prompts import EXPLORER_PROMPT, GENERAL_AGENT_PROMPT, PLANNER_PROMPT
from maba_code.tools import READ_ONLY_FOR_SUBAGENT

DEFAULT_MAX_TURNS = 25
MAX_DESCRIPTION_CHARS = 500
_NAME = re.compile(r"^[A-Za-z0-9][\w.-]{0,63}$")


@dataclass
class AgentDef:
    name: str
    description: str
    prompt: str
    tools: list[str] | None = None  # None = semua tool yang tersedia untuk sub-agen
    disallowed: list[str] = field(default_factory=list)
    max_turns: int = DEFAULT_MAX_TURNS
    builtin_readonly: bool = False  # bawaan read-only: permintaan izin otomatis ditolak, tanpa dialog
    source: str = "bawaan"

    def allows(self, tool_name: str) -> bool:
        def hit(patterns: list[str]) -> bool:
            return any(fnmatch.fnmatchcase(tool_name, TOOL_ALIASES.get(p, p)) for p in patterns)

        if self.tools is not None and not hit(self.tools):
            return False
        return not hit(self.disallowed)


BUILTIN_AGENTS: dict[str, AgentDef] = {
    "explore": AgentDef(
        "explore",
        "Fast read-only agent for finding files, searching code and answering questions about the codebase.",
        EXPLORER_PROMPT,
        list(READ_ONLY_FOR_SUBAGENT),
        builtin_readonly=True,
    ),
    "plan": AgentDef(
        "plan",
        "Read-only software architect: researches the codebase and returns a step-by-step implementation plan.",
        PLANNER_PROMPT,
        list(READ_ONLY_FOR_SUBAGENT),
        builtin_readonly=True,
    ),
    "general-purpose": AgentDef(
        "general-purpose",
        "General agent for complex multi-step tasks; can read, edit files and run commands (same permissions as you).",
        GENERAL_AGENT_PROMPT,
        None,
        max_turns=40,
    ),
}


def agent_folders(cwd: Path) -> list[tuple[Path, str]]:
    # Urutan muat: proyek dulu, lalu pengguna (yang belakangan menimpa) -> file pengguna menang.
    return [(cwd / ".maba" / "agents", "proyek"), (maba_home() / "agents", "pengguna")]


def load_agents(cwd: Path, warnings: list[str]) -> dict[str, AgentDef]:
    agents = dict(BUILTIN_AGENTS)
    for folder, source in agent_folders(cwd):
        if not folder.is_dir():
            continue
        for file in sorted(folder.rglob("*.md")):
            try:
                raw = file.read_text(encoding="utf-8-sig")
            except OSError:
                continue
            meta, body = split_frontmatter(raw)
            name = str(meta.get("name") or "").strip()
            description = " ".join(str(meta.get("description") or "").split())
            if not _NAME.match(name) or not description:
                warnings.append(f"{file}: sub-agen butuh 'name' (huruf/angka/-/_) dan 'description' di frontmatter")
                continue
            model = str(meta.get("model") or "").strip()
            if model and model != "inherit" and resolve_model(model) != CORE_MODEL:
                warnings.append(f"{file}: 'model: {model}' diabaikan — sub-agen MABA selalu memakai {CORE_MODEL}")
            try:
                max_turns = max(1, min(int(meta.get("maxTurns") or DEFAULT_MAX_TURNS), 100))
            except (TypeError, ValueError):
                max_turns = DEFAULT_MAX_TURNS
            agents[name] = AgentDef(
                name=name,
                description=description[:MAX_DESCRIPTION_CHARS],
                prompt=body.strip() or GENERAL_AGENT_PROMPT,
                tools=as_list(meta.get("tools")),
                disallowed=as_list(meta.get("disallowedTools")) or [],
                max_turns=max_turns,
                source=f"{source}: {file}",
            )
    return agents


def render_agents(agents: dict[str, AgentDef]) -> str:
    return "\n".join(f"- {a.name}: {a.description}" for a in agents.values())
