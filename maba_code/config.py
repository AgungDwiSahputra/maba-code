"""Konfigurasi MABA Code: default, alias model, dan pemuatan settings berlapis.

Aturan kepercayaan (mencegah repo asing menyetel kredensial/izin Anda):
  * ``~/.maba/settings.json``  -> boleh mengatur semuanya (file milik pengguna), termasuk ``hooks``,
    ``mcpServers`` dan ``autoMemory`` yang menjalankan perintah/menulis di luar proyek.
  * ``.maba/settings*.json`` di proyek -> hanya ``model``, ``contextWindow``, ``maxTokens``,
    ``maxTurns`` dan aturan ``permissions.ask`` / ``permissions.deny``. Kunci lain
    (``profile``, ``region``, ``mode``, ``permissions.allow``) diabaikan dan dilaporkan.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PROFILE = "agung"
# Mesin inti MABA Code: GPT-6 Luna. Bedrock mewajibkan inference profile untuk model ini (ID polos
# "openai.gpt-6-luna" ditolak). Semua fitur ber-model selain agen utama (sub-agen, skill fork, web_fetch,
# kompaksi) SELALU memakai CORE_MODEL, walau agen utama diganti lewat /model.
CORE_MODEL = "global.openai.gpt-6-luna"
DEFAULT_MODEL = CORE_MODEL
# gpt-6-luna belum ada di tabel context window Strands, jadi nilai ini asumsi konservatif
# (dipakai untuk persen konteks & kompaksi otomatis). Ubah via settings bila perlu.
DEFAULT_CONTEXT_WINDOW = 256_000
DEFAULT_MAX_TOKENS = 8192
DEFAULT_MAX_TURNS = 50
AUTO_COMPACT_PCT = 75  # konteks (persen window) tempat percakapan lama diringkas otomatis

MODES = ("default", "acceptEdits", "plan", "bypass")
MODE_LABELS = {
    "default": "Standar (tanya sebelum menulis/menjalankan)",
    "acceptEdits": "Terima edit (edit file otomatis, perintah tetap ditanya)",
    "plan": "Rencana (hanya baca, ajukan rencana dulu)",
    "bypass": "Bypass (semua otomatis, hati-hati!)",
}

# Semua ID sudah diverifikasi ada sebagai inference profile di akun/profile agung.
MODEL_ALIASES = {
    "luna": CORE_MODEL,
    "gpt-6-luna": CORE_MODEL,
    "sol": "global.openai.gpt-6-sol",
    "astra": "global.openai.gpt-6-astra",
    "sonnet": "global.anthropic.claude-sonnet-5-5",
    "opus": "global.anthropic.claude-opus-5-5",
    "haiku": "global.anthropic.claude-haiku-5-5",
    "fable": "global.anthropic.claude-fable-5-1",
    "kimi": "global.moonshotai.kimi-k3",
    "glm": "global.zai.glm-5.3",
    "grok": "global.xai.grok-4.7",
}

_PROJECT_TRUSTED_KEYS = {"model", "contextWindow", "maxTokens", "maxTurns", "permissions"}


def maba_home() -> Path:
    return Path(os.environ.get("MABA_HOME") or Path.home() / ".maba")


def project_key(cwd: Path) -> str:
    """Nama folder stabil per proyek (dipakai untuk sesi, memori otomatis, dan log task)."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(cwd)).strip("-")[-50:]
    digest = hashlib.sha1(str(cwd).lower().encode("utf-8")).hexdigest()[:6]
    return f"{slug}-{digest}"


def resolve_model(name: str) -> str:
    """Terjemahkan alias ("luna") ke model ID Bedrock; ID lengkap dibiarkan apa adanya."""
    key = name.strip()
    return MODEL_ALIASES.get(key.lower(), key)


@dataclass
class Settings:
    profile: str = DEFAULT_PROFILE
    model: str = DEFAULT_MODEL
    region: str | None = None
    mode: str = "default"
    context_window: int = DEFAULT_CONTEXT_WINDOW
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_turns: int = DEFAULT_MAX_TURNS
    allow: list[str] = field(default_factory=list)
    ask: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    hooks: dict = field(default_factory=dict)
    mcp_servers: dict = field(default_factory=dict)
    auto_memory: bool = True
    warnings: list[str] = field(default_factory=list)


def _read_json(path: Path, warnings: list[str]) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        warnings.append(f"Settings tidak terbaca ({path}): {exc}")
        return {}
    if not isinstance(data, dict):
        warnings.append(f"Settings harus berupa objek JSON: {path}")
        return {}
    return data


def _apply(settings: Settings, data: dict, *, trusted: bool, source: str) -> None:
    ignored: list[str] = []
    for key, value in data.items():
        if not trusted and key not in _PROJECT_TRUSTED_KEYS:
            ignored.append(key)
            continue
        if key == "profile" and isinstance(value, str):
            settings.profile = value
        elif key == "model" and isinstance(value, str):
            settings.model = resolve_model(value)
        elif key == "region" and isinstance(value, str):
            settings.region = value
        elif key == "mode" and value in MODES:
            settings.mode = value
        elif key == "contextWindow" and isinstance(value, int) and value > 0:
            settings.context_window = value
        elif key == "maxTokens" and isinstance(value, int) and value > 0:
            settings.max_tokens = value
        elif key == "maxTurns" and isinstance(value, int) and value > 0:
            settings.max_turns = value
        elif key == "hooks" and isinstance(value, dict):
            for event, groups in value.items():
                if isinstance(groups, list):
                    settings.hooks.setdefault(str(event), []).extend(groups)
        elif key == "mcpServers" and isinstance(value, dict):
            settings.mcp_servers.update(value)
        elif key == "autoMemory" and isinstance(value, bool):
            settings.auto_memory = value
        elif key == "permissions" and isinstance(value, dict):
            for kind in ("allow", "ask", "deny"):
                rules = value.get(kind, [])
                if not isinstance(rules, list):
                    continue
                if kind == "allow" and not trusted:
                    if rules:
                        ignored.append("permissions.allow")
                    continue
                getattr(settings, kind).extend(str(r) for r in rules)
    if ignored:
        settings.warnings.append(
            f"{source}: kunci diabaikan karena file proyek tidak dipercaya untuk itu: {', '.join(ignored)}"
        )


def load_settings(cwd: Path, overrides: dict | None = None) -> Settings:
    """Muat settings: user -> proyek -> proyek.local -> env -> CLI (belakangan menang)."""
    settings = Settings()
    _apply(settings, _read_json(maba_home() / "settings.json", settings.warnings), trusted=True, source="~/.maba/settings.json")
    for name in ("settings.json", "settings.local.json"):
        path = cwd / ".maba" / name
        _apply(settings, _read_json(path, settings.warnings), trusted=False, source=f".maba/{name}")

    env = os.environ
    if env.get("MABA_PROFILE"):
        settings.profile = env["MABA_PROFILE"]
    if env.get("MABA_MODEL"):
        settings.model = resolve_model(env["MABA_MODEL"])
    if env.get("MABA_REGION"):
        settings.region = env["MABA_REGION"]
    if env.get("MABA_MODE") in MODES:
        settings.mode = env["MABA_MODE"]

    for key, value in (overrides or {}).items():
        if value is None:
            continue
        if key == "model":
            settings.model = resolve_model(value)
        elif hasattr(settings, key):
            setattr(settings, key, value)
    return settings
