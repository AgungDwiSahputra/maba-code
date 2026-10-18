"""Skills ala Claude Code: folder berisi ``SKILL.md`` (+ file pendukung), dimuat bertahap.

Lokasi: ``~/.maba/skills/<nama>/SKILL.md`` (pengguna) dan ``.maba/skills/<nama>/SKILL.md`` (proyek);
skill pengguna menang atas skill proyek bernama sama. Hanya ``name: description`` yang masuk system prompt;
isi lengkap baru dimuat saat skill dipanggil (tool ``skill`` oleh model, atau ``/nama args`` oleh pengguna).

Frontmatter yang didukung::

    name: deploy-check                 # default: nama folder
    description: Kapan skill dipakai   # + when_to_use (opsional)
    allowed-tools: run_command(git *)  # izin tanpa dialog selama giliran itu — HANYA untuk skill pengguna
    disable-model-invocation: false    # true = hanya lewat /nama
    user-invocable: true               # false = hanya model yang memanggil
    context: fork                      # jalankan di sub-agen (GPT-6 Luna) dan kembalikan laporannya
    agent: general-purpose             # sub-agen untuk context: fork

``$ARGUMENTS`` diganti argumen; ``${MABA_SKILL_DIR}`` (atau ``${CLAUDE_SKILL_DIR}``) diganti path folder skill.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from maba_code.config import CORE_MODEL, maba_home, resolve_model
from maba_code.frontmatter import as_bool, as_list, split_frontmatter
from maba_code.hooks import TOOL_ALIASES

MAX_LISTING_CHARS = 1536  # batas deskripsi per skill di system prompt (sama dengan Claude Code)
MAX_SUPPORTING_FILES = 30
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


@dataclass
class Skill:
    name: str
    description: str
    body: str
    folder: Path
    source: str  # "pengguna" | "proyek"
    allowed_tools: list[str] = field(default_factory=list)
    model_invocable: bool = True
    user_invocable: bool = True
    fork: bool = False
    agent: str = "general-purpose"
    argument_hint: str = ""


def skill_folders(cwd: Path) -> list[tuple[Path, str]]:
    # Proyek dulu, lalu pengguna (yang belakangan menimpa) -> skill pengguna menang.
    return [(cwd / ".maba" / "skills", "proyek"), (maba_home() / "skills", "pengguna")]


def load_skills(cwd: Path, warnings: list[str]) -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    for base, source in skill_folders(cwd):
        if not base.is_dir():
            continue
        for skill_md in sorted(base.glob("*/SKILL.md")):
            try:
                raw = skill_md.read_text(encoding="utf-8-sig")
            except OSError:
                continue
            meta, body = split_frontmatter(raw)
            name = str(meta.get("name") or skill_md.parent.name).strip().lower()
            if not _NAME.match(name):
                warnings.append(f"{skill_md}: nama skill tidak valid '{name}' (huruf kecil/angka/-/_)")
                continue
            description = " ".join(f"{meta.get('description') or ''} {meta.get('when_to_use') or ''}".split())
            if not description:
                description = next((ln.strip("# ").strip() for ln in body.splitlines() if ln.strip()), name)
            model = str(meta.get("model") or "").strip()
            if model and model != "inherit" and resolve_model(model) != CORE_MODEL:
                warnings.append(f"{skill_md}: 'model: {model}' diabaikan — skill MABA selalu memakai {CORE_MODEL}")
            allowed = as_list(meta.get("allowed-tools")) or []
            if allowed and source == "proyek":
                warnings.append(f"{skill_md}: 'allowed-tools' diabaikan — skill proyek tidak boleh memberi izin sendiri")
                allowed = []
            skills[name] = Skill(
                name=name,
                description=description[:MAX_LISTING_CHARS],
                body=body.strip(),
                folder=skill_md.parent.resolve(),
                source=source,
                allowed_tools=allowed,
                model_invocable=not as_bool(meta.get("disable-model-invocation"), False),
                user_invocable=as_bool(meta.get("user-invocable"), True),
                fork=str(meta.get("context") or "").strip().lower() == "fork",
                agent=str(meta.get("agent") or "general-purpose").strip(),
                argument_hint=str(meta.get("argument-hint") or ""),
            )
    return skills


def render_skills(skills: dict[str, Skill]) -> str:
    return "\n".join(f"- {s.name}: {s.description}" for s in skills.values() if s.model_invocable)


def skill_rules(skill: Skill) -> list[str]:
    """``allowed-tools`` -> teks aturan izin MABA (nama tool Claude Code diterjemahkan, mis. Bash -> run_command)."""
    rules = []
    for item in skill.allowed_tools:
        match = re.match(r"^\s*([\w*-]+)\s*(\(.*\))?\s*$", item)
        if match:
            rules.append(TOOL_ALIASES.get(match.group(1), match.group(1)) + (match.group(2) or ""))
    return rules


def supporting_files(skill: Skill) -> list[str]:
    files = []
    for path in sorted(skill.folder.rglob("*")):
        if path.is_file() and path.name != "SKILL.md":
            files.append(path.relative_to(skill.folder).as_posix())
            if len(files) >= MAX_SUPPORTING_FILES:
                break
    return files


def expand_skill(skill: Skill, arguments: str = "") -> str:
    folder = skill.folder.as_posix()
    body = skill.body.replace("${MABA_SKILL_DIR}", folder).replace("${CLAUDE_SKILL_DIR}", folder)
    if "$ARGUMENTS" in body:
        body = body.replace("$ARGUMENTS", arguments)
    elif arguments.strip():
        body += f"\n\nARGUMENTS: {arguments}"
    parts = [f'<skill name="{skill.name}" dir="{folder}">', body]
    files = supporting_files(skill)
    if files:
        parts.append("\nSupporting files (read with read_file when needed):\n" + "\n".join(f"- {folder}/{f}" for f in files))
    parts.append("</skill>")
    return "\n".join(parts)
