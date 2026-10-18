"""Klien MCP (Model Context Protocol), di atas ``strands.tools.mcp.MCPClient``.

Sumber konfigurasi (format ``mcpServers`` Claude Code)::

    ~/.maba/mcp.json  atau  "mcpServers" di ~/.maba/settings.json   -> dipercaya (milik pengguna)
    .mcp.json di folder proyek                                      -> perlu persetujuan sekali per server

    {"mcpServers": {
        "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                   "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}},
        "docs":   {"type": "http", "url": "https://example.com/mcp", "headers": {"X-Key": "${DOCS_KEY}"}}
    }}

Tool server muncul sebagai ``mcp__<server>__<tool>`` dan selalu ditanya sebelum dipakai (aturan
``mcp__github`` / ``mcp__github__*`` / ``mcp__github__list_issues`` di permissions). Persetujuan server proyek
disimpan di ``~/.maba/mcp-trust.json`` beserta sidik jari konfigurasinya: bila konfigurasi berubah, ditanya lagi.
Variabel ``${VAR}`` bernama mirip rahasia tidak diekspansi untuk konfigurasi PROYEK (pindahkan server ke
konfigurasi pengguna bila memang butuh token).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, TextIO

from maba_code.config import Settings, maba_home, project_key

log = logging.getLogger("maba.mcp")

TOOL_NAME_LIMIT = 64  # batas nama tool Bedrock: ^[a-zA-Z0-9_-]{1,64}$
STARTUP_TIMEOUT_SECONDS = 30
DEFAULT_TOOL_TIMEOUT_SECONDS = 120
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_SECRETISH = re.compile(r"SECRET|TOKEN|KEY|PASSWORD|PASSWD|CREDENTIAL|SESSION|AUTH", re.I)


def _sanitize(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", text)


def mcp_tool_name(server: str, tool: str) -> str:
    """``mcp__<server>__<tool>``, aman untuk Bedrock (karakter & panjang maks. 64)."""
    name = f"mcp__{_sanitize(server)}__{_sanitize(tool)}"
    if len(name) > TOOL_NAME_LIMIT:
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        name = f"{name[:TOOL_NAME_LIMIT - 9]}_{digest}"
    return name


@dataclass
class ServerConfig:
    name: str
    transport: str  # stdio | http | sse
    source: str  # pengguna | proyek
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    raw: dict[str, Any] = field(default_factory=dict)  # konfigurasi apa adanya (untuk dialog & sidik jari)

    def describe(self) -> str:
        if self.transport == "stdio":
            line = " ".join([self.command, *self.args])
            env = ", ".join(sorted(self.raw.get("env") or {}))
            return line + (f"\nenv: {env}" if env else "")
        return f"{self.transport} {self.url}"

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.raw, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _expand(value: str, source: str, blocked: list[str]) -> str:
    def repl(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if source == "proyek" and _SECRETISH.search(name):
            blocked.append(name)
            return ""
        found = os.environ.get(name)
        if found is not None:
            return found
        return default if default is not None else match.group(0)

    return _VAR.sub(repl, value)


def parse_servers(servers: Any, source: str, warnings: list[str]) -> dict[str, ServerConfig]:
    parsed: dict[str, ServerConfig] = {}
    if not isinstance(servers, dict):
        return parsed
    for name, spec in servers.items():
        if not isinstance(spec, dict) or spec.get("disabled"):
            continue
        name = str(name)
        kind = str(spec.get("type") or spec.get("transport") or ("http" if spec.get("url") else "stdio")).lower()
        kind = {"streamable-http": "http", "streamable_http": "http"}.get(kind, kind)
        if kind not in ("stdio", "http", "sse"):
            warnings.append(f"MCP '{name}': tipe '{kind}' tidak didukung (stdio, http, sse)")
            continue
        blocked: list[str] = []
        cfg = ServerConfig(name=name, transport=kind, source=source, raw=spec)
        if kind == "stdio":
            cfg.command = _expand(str(spec.get("command") or ""), source, blocked)
            cfg.args = [_expand(str(a), source, blocked) for a in spec.get("args") or []]
            cfg.env = {str(k): _expand(str(v), source, blocked) for k, v in (spec.get("env") or {}).items()}
            cfg.cwd = _expand(str(spec["cwd"]), source, blocked) if spec.get("cwd") else None
            if not cfg.command:
                warnings.append(f"MCP '{name}': 'command' kosong")
                continue
        else:
            cfg.url = _expand(str(spec.get("url") or ""), source, blocked)
            cfg.headers = {str(k): _expand(str(v), source, blocked) for k, v in (spec.get("headers") or {}).items()}
            if not cfg.url.startswith(("http://", "https://")):
                warnings.append(f"MCP '{name}': 'url' tidak valid")
                continue
        if blocked:
            warnings.append(
                f"MCP '{name}' (.mcp.json proyek) dilewati: variabel rahasia {', '.join(sorted(set(blocked)))} tidak "
                "diteruskan ke server dari repo. Pindahkan server ini ke ~/.maba/mcp.json bila Anda mempercayainya."
            )
            continue
        try:
            if spec.get("timeout"):
                cfg.tool_timeout = max(1.0, float(spec["timeout"]) / 1000)  # ms, seperti Claude Code
        except (TypeError, ValueError):
            pass
        parsed[name] = cfg
    return parsed


def _read_json(path: Path, warnings: list[str]) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        warnings.append(f"Konfigurasi MCP tidak terbaca ({path}): {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def load_server_configs(cwd: Path, settings: Settings, warnings: list[str]) -> list[ServerConfig]:
    servers = parse_servers(_read_json(cwd / ".mcp.json", warnings).get("mcpServers"), "proyek", warnings)
    # Konfigurasi pengguna menang atas proyek untuk nama yang sama.
    servers.update(parse_servers(_read_json(maba_home() / "mcp.json", warnings).get("mcpServers"), "pengguna", warnings))
    servers.update(parse_servers(settings.mcp_servers, "pengguna", warnings))
    return list(servers.values())


class TrustStore:
    """Keputusan pengguna atas server MCP proyek, per folder proyek + sidik jari konfigurasi."""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _key(cwd: Path) -> str:
        return os.path.normcase(str(cwd))

    def decision(self, cwd: Path, cfg: ServerConfig) -> bool | None:
        entry = self._load().get(self._key(cwd), {}).get(cfg.name)
        if not isinstance(entry, dict) or entry.get("fingerprint") != cfg.fingerprint():
            return None
        return bool(entry.get("approved"))

    def record(self, cwd: Path, cfg: ServerConfig, approved: bool) -> None:
        data = self._load()
        data.setdefault(self._key(cwd), {})[cfg.name] = {"fingerprint": cfg.fingerprint(), "approved": approved}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        except OSError:
            log.debug("gagal menyimpan mcp-trust.json", exc_info=True)


@dataclass
class ServerState:
    config: ServerConfig
    status: str
    tools: list[str] = field(default_factory=list)
    client: Any = None


class McpManager:
    def __init__(self, cwd: Path, settings: Settings):
        self.cwd = cwd
        self.warnings: list[str] = []
        self.configs = load_server_configs(cwd, settings, self.warnings)
        self.trust = TrustStore(maba_home() / "mcp-trust.json")
        self.log_path = maba_home() / "logs" / f"mcp-{project_key(cwd)}.log"
        self.states: dict[str, ServerState] = {}
        self.tools: list[Any] = []
        self._errlog: TextIO | None = None

    async def start(self, approve: Callable[[ServerConfig], Awaitable[bool]] | None) -> None:
        to_start: list[ServerConfig] = []
        for cfg in self.configs:
            if cfg.source == "proyek":
                decision = self.trust.decision(self.cwd, cfg)
                if decision is None:
                    if approve is None:
                        self.states[cfg.name] = ServerState(cfg, "belum disetujui (jalankan MABA interaktif untuk menyetujui)")
                        continue
                    decision = await approve(cfg)
                    self.trust.record(self.cwd, cfg, decision)
                if not decision:
                    self.states[cfg.name] = ServerState(cfg, "ditolak pengguna")
                    continue
            to_start.append(cfg)
        if not to_start:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # stderr server MCP ke file log, bukan ke terminal (agar tidak merusak tampilan REPL).
        self._errlog = open(self.log_path, "a", encoding="utf-8", errors="replace")  # noqa: SIM115
        results = await asyncio.gather(*(asyncio.to_thread(self._start_one, cfg) for cfg in to_start))
        taken: set[str] = set()
        for cfg, (client, tools, error) in zip(to_start, results):
            if client is None:
                self.states[cfg.name] = ServerState(cfg, f"gagal: {error}")
                continue
            kept = []
            for t in tools:
                key = t.tool_name.lower().replace("-", "_")  # registry Strands menolak nama yang hanya beda -/_
                if key in taken:
                    self.warnings.append(f"MCP '{cfg.name}': tool {t.tool_name} dilewati (nama bentrok)")
                    continue
                taken.add(key)
                kept.append(t)
            self.tools.extend(kept)
            self.states[cfg.name] = ServerState(cfg, "terhubung", [t.tool_name for t in kept], client)

    def _transport(self, cfg: ServerConfig) -> Callable[[], Any]:
        if cfg.transport == "stdio":
            from mcp.client.stdio import StdioServerParameters, stdio_client

            params = StdioServerParameters(command=cfg.command, args=cfg.args, env=cfg.env or None, cwd=cfg.cwd)
            errlog = self._errlog
            return lambda: stdio_client(params, errlog=errlog) if errlog else stdio_client(params)
        if cfg.transport == "sse":
            from mcp.client.sse import sse_client

            return lambda: sse_client(cfg.url, headers=cfg.headers or None)
        from mcp.client.streamable_http import streamablehttp_client

        return lambda: streamablehttp_client(cfg.url, headers=cfg.headers or None)

    def _start_one(self, cfg: ServerConfig) -> tuple[Any, list[Any], str]:
        from strands.tools.mcp import MCPClient
        from strands.tools.mcp.mcp_agent_tool import MCPAgentTool

        client = MCPClient(self._transport(cfg), startup_timeout=STARTUP_TIMEOUT_SECONDS)
        try:
            client.start()
            found: list[Any] = []
            token: str | None = None
            while True:
                page = client.list_tools_sync(pagination_token=token)
                found.extend(page)
                token = page.pagination_token
                if not token:
                    break
            timeout = timedelta(seconds=cfg.tool_timeout)
            tools = [
                MCPAgentTool(t.mcp_tool, client, name_override=mcp_tool_name(cfg.name, t.mcp_tool.name), timeout=timeout)
                for t in found
            ]
            return client, tools, ""
        except Exception as exc:  # noqa: BLE001 - server rusak tidak boleh menggagalkan sesi
            log.debug("MCP %s gagal start", cfg.name, exc_info=True)
            try:
                client.stop(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            cause = exc.__cause__ or exc
            return None, [], f"{type(cause).__name__}: {str(cause)[:200]} (log: {self.log_path})"

    def stop_all(self) -> None:
        for state in self.states.values():
            if state.client is not None:
                try:
                    state.client.stop(None, None, None)
                except Exception:  # noqa: BLE001
                    log.debug("MCP stop gagal", exc_info=True)
                state.client = None
                state.status = "dihentikan"
        if self._errlog is not None:
            self._errlog.close()
            self._errlog = None
