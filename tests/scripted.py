"""Model palsu offline + harness uji: mengalirkan event Converse sungguhan tanpa jaringan."""

from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import threading
from collections.abc import AsyncIterable
from pathlib import Path
from typing import Any

from rich.console import Console
from strands.models.model import Model

from maba_code.config import CORE_MODEL, Settings
from maba_code.session import ChatApp
from maba_code.ui.terminal import TerminalUI
from maba_code.ui.theme import ASCII


def text_turn(text: str, in_tok: int = 10, out_tok: int = 5, chunk: int = 8, delay: float = 0.0) -> list[Any]:
    ev: list[Any] = [{"messageStart": {"role": "assistant"}}, {"contentBlockStart": {"start": {}}}]
    for i in range(0, len(text), chunk):
        ev.append({"contentBlockDelta": {"delta": {"text": text[i : i + chunk]}}})
        if delay:
            ev.append(("sleep", delay))
    ev += [
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": in_tok, "outputTokens": out_tok, "totalTokens": in_tok + out_tok}, "metrics": {"latencyMs": 5}}},
    ]
    return ev


def tool_turn(calls: list[tuple[str, str, dict[str, Any]]], preface: str | None = None, in_tok: int = 20, out_tok: int = 9) -> list[Any]:
    ev: list[Any] = [{"messageStart": {"role": "assistant"}}]
    if preface:
        ev += [{"contentBlockStart": {"start": {}}}, {"contentBlockDelta": {"delta": {"text": preface}}}, {"contentBlockStop": {}}]
    for tool_id, name, args in calls:
        payload = json.dumps(args)
        ev.append({"contentBlockStart": {"start": {"toolUse": {"toolUseId": tool_id, "name": name}}}})
        for i in range(0, len(payload), 12):
            ev.append({"contentBlockDelta": {"delta": {"toolUse": {"input": payload[i : i + 12]}}}})
        ev.append({"contentBlockStop": {}})
    ev += [
        {"messageStop": {"stopReason": "tool_use"}},
        {"metadata": {"usage": {"inputTokens": in_tok, "outputTokens": out_tok, "totalTokens": in_tok + out_tok}, "metrics": {"latencyMs": 5}}},
    ]
    return ev


class ScriptedModel(Model):
    def __init__(self, turns: list[list[Any]]):
        self.turns = list(turns)
        self.i = 0
        self.config: dict[str, Any] = {"model_id": "mock", "context_window_limit": 100_000}
        self.seen: list[Any] = []
        self.seen_tools: list[list[str]] = []

    def update_config(self, **kw: Any) -> None:
        self.config.update(kw)

    def get_config(self) -> dict[str, Any]:
        return self.config

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):  # pragma: no cover
        raise NotImplementedError
        yield

    async def stream(
        self, messages, tool_specs=None, system_prompt=None, *, tool_choice=None, system_prompt_content=None,
        invocation_state=None, cancel_signal: threading.Event | None = None, **kwargs: Any,
    ) -> AsyncIterable[Any]:
        self.seen.append((messages, system_prompt))
        self.seen_tools.append([spec["name"] for spec in tool_specs or []])
        turn = self.turns[min(self.i, len(self.turns) - 1)]
        self.i += 1
        for ev in turn:
            if isinstance(ev, tuple) and ev[0] == "sleep":
                await asyncio.sleep(ev[1])
                continue
            await asyncio.sleep(0)
            yield ev


class Harness:
    """ChatApp lengkap (agen + izin + UI nyata) dengan model skrip dan jawaban dialog terskrip."""

    def __init__(self, turns: list[list[Any]], mode: str = "default", answers: list[str] | None = None,
                 texts: list[str] | None = None, max_turns: int = 50, core_turns: list[list[Any]] | None = None,
                 **settings_kw: Any):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmp.name)
        self._old_home = os.environ.get("MABA_HOME")
        os.environ["MABA_HOME"] = str(root / "home")
        self.cwd = root / "proj"
        self.cwd.mkdir()
        self.settings = Settings(mode=mode, max_turns=max_turns, **settings_kw)
        self.buf = io.StringIO()
        console = Console(file=self.buf, force_terminal=False, width=110, color_system=None, highlight=False)
        self.answers = list(answers or [])
        self.texts = list(texts or [])
        self.asked: list[dict[str, str]] = []

        async def choose(mapping: dict[str, str], message: str) -> str:
            self.asked.append(mapping)
            return self.answers.pop(0)

        async def ask_text(message: str) -> str:
            return self.texts.pop(0) if self.texts else ""

        self.ui = TerminalUI(console, choose=choose, ask_text=ask_text, symbols=ASCII)
        self.model = ScriptedModel(turns)
        # core_turns: skrip terpisah untuk GPT-6 Luna (CORE_MODEL) agar uji bisa membuktikan sub-agen/kompaksi/web
        # selalu memakai Luna. Tanpa core_turns, semua model berbagi satu skrip (perilaku uji lama).
        self.core_model = ScriptedModel(core_turns) if core_turns is not None else self.model
        self.factory_calls: list[str] = []
        self.app = ChatApp(self.settings, self.cwd, self.ui, model_factory=self.model_factory)

    def model_factory(self, settings: Settings) -> ScriptedModel:
        self.factory_calls.append(settings.model)
        return self.core_model if settings.model == CORE_MODEL else self.model

    def close(self) -> None:
        if self._old_home is None:
            os.environ.pop("MABA_HOME", None)
        else:
            os.environ["MABA_HOME"] = self._old_home
        self._tmp.cleanup()

    @property
    def out(self) -> str:
        return self.buf.getvalue()

    def write(self, rel: str, content: str, newline: str = "\n") -> Path:
        path = self.cwd / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.replace("\n", newline).encode("utf-8"))
        return path

    def tool_results(self) -> list[tuple[str, str, str]]:
        found = []
        for msg in self.app.agent.messages:
            for block in msg.get("content", []):
                if "toolResult" in block:
                    tr = block["toolResult"]
                    found.append((tr["toolUseId"], tr["status"], "\n".join(c.get("text", "") for c in tr["content"])))
        return found

    def result_for(self, tool_id: str) -> tuple[str, str]:
        for tid, status, text in self.tool_results():
            if tid == tool_id:
                return status, text
        raise KeyError(tool_id)
