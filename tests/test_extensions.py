"""Uji fitur agentik lanjutan: hooks, sub-agen, skills, background task, /loop, memori otomatis, web_fetch,
MCP, dan penguncian GPT-6 Luna. Semua offline (model skrip, server MCP lokal, transport HTTP palsu)."""

from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from maba_code.config import CORE_MODEL
from maba_code.hooks import HookOutcome, HookRunner, matcher_matches, parse_hooks
from maba_code.permissions import Rule
from maba_code.shell import IS_WINDOWS
from tests.scripted import Harness, text_turn, tool_turn


class ExtCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.scripts = Path(tmp.name)

    def harness(self, *args, **kwargs) -> Harness:
        h = Harness(*args, **kwargs)
        self.addCleanup(h.close)
        return h

    def hook(self, name: str, body: str) -> str:
        """Tulis skrip hook Python (stdin JSON sudah terbaca sebagai `data`) dan kembalikan perintah shell-nya."""
        path = self.scripts / f"{name}.py"
        path.write_text(
            "import json, sys\nfrom pathlib import Path\ndata = json.load(sys.stdin)\n" + textwrap.dedent(body),
            encoding="utf-8",
        )
        return f'& "{sys.executable}" "{path}"' if IS_WINDOWS else f'"{sys.executable}" "{path}"'


def hooks_config(event: str, command: str, matcher: str = "") -> dict:
    return {event: [{"matcher": matcher, "hooks": [{"type": "command", "command": command, "timeout": 60}]}]}


# ============================================================================ hooks
class HookUnitTests(unittest.TestCase):
    def test_matcher_forms(self):
        self.assertTrue(matcher_matches("", "run_command"))
        self.assertTrue(matcher_matches("*", "edit_file"))
        self.assertTrue(matcher_matches("edit_file|write_file", "write_file"))
        self.assertFalse(matcher_matches("edit_file|write_file", "read_file"))
        self.assertTrue(matcher_matches("Bash", "run_command"), "alias nama tool Claude Code")
        self.assertTrue(matcher_matches("mcp__github__.*", "mcp__github__list_issues"))
        self.assertFalse(matcher_matches("mcp__github__.*", "mcp__gitlab__x"))
        self.assertFalse(matcher_matches("([", "x"), "regex rusak tidak boleh melempar")

    def test_parse_ignores_unknown_events_and_types(self):
        warnings: list[str] = []
        parsed = parse_hooks(
            {
                "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "echo 1"}, {"type": "http", "url": "x"}]}],
                "Teleport": [{"hooks": [{"type": "command", "command": "echo 2"}]}],
            },
            warnings,
        )
        self.assertEqual(list(parsed), ["PreToolUse"])
        self.assertEqual(len(parsed["PreToolUse"][0].commands), 1)
        self.assertEqual(len(warnings), 2)

    def test_json_output_merges_most_restrictive_decision(self):
        outcome = HookOutcome()
        apply = HookRunner._apply_stdout
        apply("PreToolUse", json.dumps({"hookSpecificOutput": {"permissionDecision": "allow"}}), outcome)
        apply("PreToolUse", json.dumps({"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "tidak"}}), outcome)
        apply("PreToolUse", json.dumps({"hookSpecificOutput": {"permissionDecision": "ask"}}), outcome)
        self.assertEqual((outcome.decision, outcome.decision_reason), ("deny", "tidak"))
        apply("Stop", json.dumps({"decision": "block", "reason": "belum"}), outcome)
        self.assertTrue(outcome.blocked)
        apply("UserPromptSubmit", "teks polos", outcome)
        apply("PostToolUse", "teks polos diabaikan", outcome)
        self.assertEqual(outcome.context, ["teks polos"])

    def test_project_settings_cannot_define_hooks(self):
        from maba_code.config import load_settings

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {"MABA_HOME": str(Path(tmp) / "home")}):
            proj = Path(tmp) / "proj"
            (proj / ".maba").mkdir(parents=True)
            (proj / ".maba" / "settings.json").write_text(json.dumps({"hooks": hooks_config("Stop", "evil")}), encoding="utf-8")
            settings = load_settings(proj)
            self.assertEqual(settings.hooks, {})
            self.assertTrue(any("hooks" in w for w in settings.warnings))


class HookIntegrationTests(ExtCase):
    async def test_pre_tool_use_exit_2_blocks_and_model_sees_reason(self):
        record = self.scripts / "payload.json"
        cmd = self.hook("block", f"""
            Path(r"{record}").write_text(json.dumps(data), encoding="utf-8")
            print("perintah dilarang oleh hook", file=sys.stderr)
            sys.exit(2)
        """)
        h = self.harness(
            [tool_turn([("c1", "run_command", {"command": "echo hai"})]), text_turn("ok")],
            mode="bypass", hooks=hooks_config("PreToolUse", cmd, "Bash"),
        )
        await h.app.run_prompt("jalankan")
        status, text = h.result_for("c1")
        self.assertEqual(status, "error")
        self.assertIn("perintah dilarang oleh hook", text)
        payload = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(payload["hook_event_name"], "PreToolUse")
        self.assertEqual(payload["tool_name"], "run_command")
        self.assertEqual(payload["tool_input"]["command"], "echo hai")
        self.assertEqual(payload["session_id"], h.app.session_id)
        self.assertEqual(payload["permission_mode"], "bypass")

    async def test_pre_tool_use_updated_input_is_reevaluated_and_hook_allow_skips_dialog(self):
        cmd = self.hook("rewrite", """
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                                    "updatedInput": {"command": "echo diganti-hook"}}}))
        """)
        h = self.harness(
            [tool_turn([("c1", "run_command", {"command": "echo asli"})]), text_turn("ok")],
            hooks=hooks_config("PreToolUse", cmd, "run_command"),
        )
        await h.app.run_prompt("jalankan")
        status, text = h.result_for("c1")
        self.assertEqual(status, "success")
        self.assertIn("diganti-hook", text)
        self.assertEqual(h.asked, [], "allow dari hook mengubah ask menjadi allow")

    async def test_hook_allow_cannot_override_deny_rule(self):
        cmd = self.hook("allow", """
            print(json.dumps({"hookSpecificOutput": {"permissionDecision": "allow"}}))
        """)
        h = self.harness(
            [tool_turn([("c1", "run_command", {"command": "echo x"})]), text_turn("ok")],
            hooks=hooks_config("PreToolUse", cmd), deny=["run_command(echo *)"],
        )
        await h.app.run_prompt("jalankan")
        self.assertEqual(h.result_for("c1")[0], "error")
        self.assertIn("Denied by permission rule", h.result_for("c1")[1])

    async def test_post_tool_use_context_is_appended_to_result(self):
        cmd = self.hook("post", """
            assert data["tool_response"].strip()
            print(json.dumps({"hookSpecificOutput": {"additionalContext": "CATATAN-DARI-HOOK"}}))
        """)
        h = self.harness(
            [tool_turn([("r1", "read_file", {"path": "a.txt"})]), text_turn("ok")],
            hooks=hooks_config("PostToolUse", cmd, "read_file"),
        )
        h.write("a.txt", "isi\n")
        await h.app.run_prompt("baca")
        status, text = h.result_for("r1")
        self.assertEqual(status, "success")
        self.assertIn("CATATAN-DARI-HOOK", text)

    async def test_user_prompt_submit_adds_context_or_blocks(self):
        cmd = self.hook("ups", """
            if "rahasia" in data["prompt"]:
                print("jangan bahas rahasia", file=sys.stderr)
                sys.exit(2)
            print("KONTEKS-TAMBAHAN-HOOK")
        """)
        h = self.harness([text_turn("dijawab")], hooks=hooks_config("UserPromptSubmit", cmd))
        await h.app.run_prompt("halo")
        first_user = h.model.seen[0][0][0]["content"][0]["text"]
        self.assertIn("KONTEKS-TAMBAHAN-HOOK", first_user)
        calls = h.model.i
        stats = await h.app.run_prompt("ini rahasia")
        self.assertTrue(stats.blocked)
        self.assertEqual(h.model.i, calls, "model tidak boleh dipanggil untuk prompt yang diblokir")
        self.assertIn("jangan bahas rahasia", h.out)

    async def test_stop_hook_continues_turn_until_satisfied(self):
        counter = self.scripts / "count.txt"
        cmd = self.hook("stop", f"""
            p = Path(r"{counter}")
            n = int(p.read_text()) if p.exists() else 0
            p.write_text(str(n + 1))
            if n == 0:
                assert data["stop_hook_active"] is False
                print("jalankan tes dulu", file=sys.stderr)
                sys.exit(2)
        """)
        h = self.harness([text_turn("selesai?"), text_turn("tes sudah jalan")], hooks=hooks_config("Stop", cmd))
        stats = await h.app.run_prompt("kerjakan")
        self.assertIsNone(stats.error)
        self.assertEqual(h.model.i, 2)
        last_prompt = h.model.seen[1][0][-1]["content"][0]["text"]
        self.assertIn("[Stop hook] jalankan tes dulu", last_prompt)
        self.assertEqual(counter.read_text(), "2")

    async def test_stop_hook_is_capped(self):
        cmd = self.hook("stop_forever", """
            print("lagi", file=sys.stderr)
            sys.exit(2)
        """)
        h = self.harness([text_turn("x")], hooks=hooks_config("Stop", cmd))
        with mock.patch("maba_code.session.STOP_HOOK_CAP", 2):
            await h.app.run_prompt("kerjakan")
        self.assertEqual(h.model.i, 3, "1 jawaban + 2 lanjutan, lalu berhenti")
        self.assertIn("giliran diakhiri", h.out)

    async def test_session_start_context_reaches_first_prompt(self):
        cmd = self.hook("start", """
            assert data["source"] == "startup"
            print("INFO-AWAL-SESI")
        """)
        h = self.harness([text_turn("ok")], hooks=hooks_config("SessionStart", cmd))
        await h.app.start()
        await h.app.run_prompt("halo")
        self.assertIn("INFO-AWAL-SESI", h.model.seen[0][0][0]["content"][0]["text"])
        await h.app.shutdown()


# ========================================================================= sub-agen
def write_agent(h: Harness, name: str, frontmatter: str, body: str = "Kamu peninjau kode.") -> None:
    folder = h.cwd / ".maba" / "agents"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")


class SubagentTests(ExtCase):
    async def test_file_agent_tools_allowlist_and_prompt(self):
        h = self.harness(
            [
                tool_turn([("t1", "task", {"subagent_type": "reviewer", "prompt": "tinjau a.py"})]),
                text_turn("TEMUAN: aman"),  # giliran sub-agen
                text_turn("Selesai."),
            ]
        )
        write_agent(h, "reviewer", "name: reviewer\ndescription: Meninjau kode\ntools: Read, grep_files")
        await h.app.run_prompt("tinjau")
        status, text = h.result_for("t1")
        self.assertEqual(status, "success")
        self.assertIn("TEMUAN: aman", text)
        self.assertEqual(sorted(h.model.seen_tools[1]), ["grep_files", "read_file"], "alias 'Read' = read_file")
        self.assertIn("Kamu peninjau kode.", h.model.seen[1][1])
        self.assertIn("- reviewer: Meninjau kode", h.model.seen[0][1], "daftar agen ada di system prompt utama")

    async def test_unknown_agent_is_an_error_listing_available_agents(self):
        h = self.harness([tool_turn([("t1", "task", {"subagent_type": "hantu", "prompt": "x"})]), text_turn("ok")])
        await h.app.run_prompt("x")
        status, text = h.result_for("t1")
        self.assertEqual(status, "error")
        self.assertIn("general-purpose", text)

    async def test_general_purpose_write_asks_user_with_agent_label(self):
        h = self.harness(
            [
                tool_turn([("t1", "task", {"subagent_type": "general-purpose", "prompt": "buat catatan.txt"})]),
                tool_turn([("w1", "write_file", {"path": "catatan.txt", "content": "halo\n"})]),  # sub-agen
                text_turn("File dibuat."),  # sub-agen selesai
                text_turn("Beres."),
            ],
            answers=["allow"],
        )
        await h.app.run_prompt("buat")
        self.assertEqual((h.cwd / "catatan.txt").read_text(encoding="utf-8"), "halo\n")
        self.assertEqual(len(h.asked), 1)
        self.assertIn("[sub-agen general-purpose]", h.out)
        self.assertNotIn("todo_write", h.model.seen_tools[1], "sub-agen tidak mengubah todo agen utama")

    async def test_explore_stays_read_only_without_dialog(self):
        h = self.harness(
            [
                tool_turn([("x1", "explore", {"task": "coba tulis"})]),
                tool_turn([("w1", "write_file", {"path": "x.txt", "content": "x"})]),  # tidak tersedia untuk explore
                text_turn("tidak bisa menulis"),
                text_turn("ok"),
            ]
        )
        await h.app.run_prompt("x")
        self.assertNotIn("write_file", h.model.seen_tools[1])
        self.assertFalse((h.cwd / "x.txt").exists())
        self.assertEqual(h.asked, [])

    async def test_nesting_is_capped(self):
        h = self.harness(
            [
                tool_turn([("t1", "task", {"subagent_type": "general-purpose", "prompt": "delegasikan lagi"})]),
                tool_turn([("t2", "task", {"subagent_type": "general-purpose", "prompt": "tingkat dua"})]),  # depth 1
                text_turn("laporan tingkat dua"),  # depth 2
                text_turn("laporan tingkat satu"),
                text_turn("selesai"),
            ]
        )
        await h.app.run_prompt("x")
        self.assertIn("task", h.model.seen_tools[1], "sub-agen tingkat 1 boleh mendelegasikan")
        self.assertNotIn("task", h.model.seen_tools[2], "tingkat 2 tidak boleh mendelegasikan lagi")
        self.assertNotIn("explore", h.model.seen_tools[2])
        # Hasil t2 masuk ke riwayat sub-agen tingkat 1 (bukan agen utama); agen utama hanya melihat laporan t1.
        sub1_last_prompt = h.model.seen[3][0][-1]["content"][0]["toolResult"]["content"][0]["text"]
        self.assertIn("laporan tingkat dua", sub1_last_prompt)
        self.assertIn("laporan tingkat satu", h.result_for("t1")[1])

    async def test_model_field_is_ignored_with_warning(self):
        h = self.harness([text_turn("ok")])
        write_agent(h, "cepat", "name: cepat\ndescription: Agen cepat\nmodel: haiku")
        await h.app.run_prompt("x")
        self.assertIn("diabaikan", h.out)
        self.assertIn(CORE_MODEL, h.out)
        self.assertIn("cepat", h.app.agent_defs)


# =========================================================================== skills
def write_skill(base: Path, name: str, frontmatter: str, body: str, extra: dict[str, str] | None = None) -> Path:
    folder = base / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")
    for rel, content in (extra or {}).items():
        (folder / rel).write_text(content, encoding="utf-8")
    return folder


def user_skills_dir() -> Path:
    import os

    return Path(os.environ["MABA_HOME"]) / "skills"


class SkillTests(ExtCase):
    async def test_listing_and_model_invocation_with_supporting_file_outside_cwd(self):
        h = self.harness([text_turn("x")])
        folder = write_skill(
            user_skills_dir(), "rilis", "description: Siapkan catatan rilis", "Tulis catatan rilis untuk $ARGUMENTS.",
            {"template.md": "# TEMPLATE-RILIS\n"},
        )
        write_skill(h.cwd / ".maba" / "skills", "rahasia", "description: Tersembunyi\ndisable-model-invocation: true", "x")
        h.model.turns = [
            tool_turn([("s1", "skill", {"name": "rilis", "arguments": "v2.0"})]),
            tool_turn([("r1", "read_file", {"path": str(folder / "template.md")})]),
            text_turn("ok"),
        ]
        await h.app.run_prompt("buat catatan rilis")
        system_prompt = h.model.seen[0][1]
        self.assertIn("- rilis: Siapkan catatan rilis", system_prompt)
        self.assertNotIn("rahasia", system_prompt)
        status, text = h.result_for("s1")
        self.assertEqual(status, "success")
        self.assertIn("Tulis catatan rilis untuk v2.0.", text)
        self.assertIn("template.md", text)
        self.assertEqual(h.result_for("r1")[0], "success")
        self.assertIn("TEMPLATE-RILIS", h.result_for("r1")[1])
        self.assertEqual(h.asked, [], "file pendukung skill boleh dibaca tanpa dialog")

    async def test_user_slash_invocation_and_turn_scoped_allowed_tools(self):
        from maba_code.commands import CommandRegistry

        h = self.harness([
            tool_turn([("c1", "run_command", {"command": "echo dari-skill"})]),
            text_turn("selesai"),
            tool_turn([("c2", "run_command", {"command": "echo giliran-berikut"})]),
            text_turn("ok"),
        ], answers=["deny"])
        write_skill(user_skills_dir(), "gema", "description: Gema\nallowed-tools: Bash(echo *)", "Jalankan echo untuk $ARGUMENTS")
        registry = CommandRegistry(h.app)
        h.app.reload_extensions()
        self.assertIn("gema", [n for n, _ in h.app.command_names()])
        await registry.dispatch("/gema dunia")
        self.assertEqual(h.app.agent.messages[0]["content"][0]["text"].count("Jalankan echo untuk dunia"), 1)
        self.assertEqual(h.result_for("c1")[0], "success")
        self.assertEqual(h.asked, [], "allowed-tools skill pengguna berlaku pada giliran itu")
        await h.app.run_prompt("lagi")
        self.assertEqual(len(h.asked), 1, "izin skill tidak terbawa ke giliran berikutnya")
        self.assertEqual(h.result_for("c2")[0], "error")

    async def test_project_skill_cannot_grant_tools(self):
        h = self.harness([text_turn("ok")])
        write_skill(h.cwd / ".maba" / "skills", "nakal", "description: x\nallowed-tools: Bash(*)", "x")
        await h.app.run_prompt("x")
        self.assertEqual(h.app.skills["nakal"].allowed_tools, [])
        self.assertIn("allowed-tools' diabaikan", h.out)

    async def test_fork_skill_runs_in_subagent(self):
        h = self.harness([
            tool_turn([("s1", "skill", {"name": "audit"})]),
            text_turn("LAPORAN-AUDIT"),  # sub-agen
            text_turn("ok"),
        ])
        write_skill(h.cwd / ".maba" / "skills", "audit", "description: Audit\ncontext: fork\nagent: explore", "Audit repo ini.")
        await h.app.run_prompt("audit")
        self.assertIn("LAPORAN-AUDIT", h.result_for("s1")[1])
        self.assertIn("Audit repo ini.", h.model.seen[1][0][0]["content"][0]["text"])
        self.assertNotIn("write_file", h.model.seen_tools[1])


# ================================================================ background + /loop
async def wait_until(predicate, timeout: float = 15.0) -> None:
    import asyncio

    for _ in range(int(timeout / 0.1)):
        if predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("kondisi tidak tercapai sebelum timeout")


def started_id(text: str) -> str:
    import re

    match = re.search(r"background task (bg-[0-9a-f]+)", text)
    assert match, text
    return match.group(1)


class BackgroundTaskTests(ExtCase):
    async def test_finished_task_is_reported_in_next_prompt(self):
        h = self.harness(
            [tool_turn([("b1", "run_command", {"command": "echo LATAR-BELAKANG", "run_in_background": True})]),
             text_turn("dimulai"), text_turn("sudah lihat")],
            mode="bypass",
        )
        await h.app.run_prompt("jalankan di latar")
        status, text = h.result_for("b1")
        self.assertEqual(status, "success")
        task = h.app.background.tasks[started_id(text)]
        await wait_until(lambda: not task.running)
        await h.app.run_prompt("lanjut")
        last_user = h.model.seen[-1][0][-1]["content"][0]["text"]
        self.assertIn("<background-task-finished", last_user)
        self.assertIn("LATAR-BELAKANG", last_user)
        self.assertIn("selesai (exit 0)", h.out)

    async def test_task_stop_kills_long_running_task_and_log_is_readable(self):
        sleeper = "Start-Sleep -Seconds 30" if IS_WINDOWS else "sleep 30"
        h = self.harness(
            [tool_turn([("b1", "run_command", {"command": f"echo MULAI; {sleeper}", "run_in_background": True})]), text_turn("ok")],
            mode="bypass",
        )
        await h.app.run_prompt("server")
        task_id = started_id(h.result_for("b1")[1])
        task = h.app.background.tasks[task_id]
        self.assertTrue(task.running)
        await wait_until(lambda: "MULAI" in task.log_path.read_text(encoding="utf-8", errors="replace"))
        h.model.turns += [
            tool_turn([("r1", "read_file", {"path": str(task.log_path)}), ("s1", "task_stop", {"task_id": task_id})]),
            text_turn("dihentikan"),
        ]
        await h.app.run_prompt("hentikan")
        self.assertEqual(h.result_for("s1")[0], "success")
        self.assertIn("MULAI", h.result_for("r1")[1], "log task boleh dibaca walau di luar folder kerja")
        await wait_until(lambda: not task.running)
        self.assertEqual(task.status(), "dihentikan")
        self.assertEqual(h.app.background.take_finished(), [], "task yang dihentikan tidak dilaporkan ulang")

    async def test_background_command_always_asks_in_default_mode(self):
        h = self.harness(
            [tool_turn([("b1", "run_command", {"command": "git status", "run_in_background": True})]), text_turn("ok")],
            answers=["deny"],
        )
        await h.app.run_prompt("x")
        self.assertEqual(len(h.asked), 1, "perintah 'aman' pun tetap ditanya bila dijalankan di latar")
        self.assertIn("latar belakang", h.out)
        self.assertEqual(h.app.background.tasks, {})

    async def test_tasks_command_lists_and_shutdown_kills(self):
        from maba_code.commands import CommandRegistry

        h = self.harness([text_turn("ok")])
        sleeper = "Start-Sleep -Seconds 30" if IS_WINDOWS else "sleep 30"
        task = h.app.background.start(sleeper, str(h.cwd), "tidur")
        registry = CommandRegistry(h.app)
        await registry.dispatch("/tasks")
        self.assertIn(task.id, h.out)
        h.app._started = True
        await h.app.shutdown()
        await wait_until(lambda: not task.running)


class LoopTests(ExtCase):
    async def test_loop_runs_fixed_iterations(self):
        from maba_code.commands import CommandRegistry

        h = self.harness([text_turn("cek")])
        registry = CommandRegistry(h.app)
        with mock.patch("maba_code.commands.LOOP_MIN_SECONDS", 0), mock.patch("maba_code.commands.LOOP_MAX_ITERATIONS", 3):
            await registry.dispatch("/loop 0s cek status build")
        self.assertEqual(h.model.i, 3)
        self.assertIn("Loop selesai setelah 3 iterasi", h.out)

    async def test_cancel_during_wait_stops_loop(self):
        import asyncio

        from maba_code.commands import CommandRegistry

        h = self.harness([text_turn("cek")])
        registry = CommandRegistry(h.app)
        asyncio.get_running_loop().call_later(1.0, h.app.cancel)
        with mock.patch("maba_code.commands.LOOP_MIN_SECONDS", 0):
            await asyncio.wait_for(registry.dispatch("/loop 30s cek"), 10)
        self.assertEqual(h.model.i, 1)
        self.assertIn("Loop selesai setelah 1 iterasi", h.out)
        stats = await h.app.run_prompt("setelah loop")
        self.assertFalse(stats.cancelled, "cancel saat menunggu tidak boleh membatalkan giliran berikutnya")

    async def test_loop_usage_and_no_nesting(self):
        from maba_code.commands import CommandRegistry

        h = self.harness([text_turn("x")])
        registry = CommandRegistry(h.app)
        await registry.dispatch("/loop")
        await registry.dispatch("/loop 5m /loop 1m x")
        self.assertIn("Pakai: /loop", h.out)
        self.assertIn("tidak bisa bersarang", h.out)
        self.assertEqual(h.model.i, 0)


# ================================================================== memori otomatis
class AutoMemoryTests(ExtCase):
    async def test_agent_writes_memory_without_prompt_and_index_is_loaded(self):
        h = self.harness([text_turn("x")])
        folder = h.app.ws.memory_dir
        self.assertIsNotNone(folder)
        h.model.turns = [
            tool_turn([("w1", "write_file", {"path": str(folder / "pakai-pnpm.md"), "content": "Proyek memakai pnpm.\n"}),
                       ("w2", "write_file", {"path": str(folder / "MEMORY.md"), "content": "- [pnpm](pakai-pnpm.md) — pakai pnpm\n"})]),
            text_turn("Diingat."),
            text_turn("ok"),
        ]
        await h.app.run_prompt("ingat: kita pakai pnpm")
        self.assertEqual(h.asked, [], "menulis memori .md tidak memerlukan dialog")
        self.assertEqual(h.result_for("w1")[0], "success")
        self.assertIn("Menyimpan memori otomatis", h.out)
        await h.app.run_prompt("sesi baru")
        self.assertIn("- [pnpm](pakai-pnpm.md) — pakai pnpm", h.model.seen[-1][1])

    async def test_memory_allow_is_narrow(self):
        from maba_code.permissions import Decision

        h = self.harness([text_turn("x")], mode="plan")
        folder = h.app.ws.memory_dir
        policy, ws = h.app.policy, h.app.ws
        self.assertIs(policy.evaluate("write_file", {"path": str(folder / "a.md")}, ws).decision, Decision.DENY, "plan mode tetap menang")
        policy.mode = "default"
        self.assertIs(policy.evaluate("write_file", {"path": str(folder / "a.md")}, ws).decision, Decision.ALLOW)
        self.assertIs(policy.evaluate("write_file", {"path": str(folder / "skrip.ps1")}, ws).decision, Decision.ASK, "hanya .md")
        self.assertIs(policy.evaluate("write_file", {"path": str(folder.parent / "x.md")}, ws).decision, Decision.ASK)
        policy.mode = "bypass"
        self.assertIs(policy.evaluate("read_file", {"path": str(folder / ".env")}, ws).decision, Decision.DENY, "pagar rahasia")

    async def test_auto_memory_can_be_disabled(self):
        h = self.harness([text_turn("x")], auto_memory=False)
        await h.app.run_prompt("x")
        self.assertIsNone(h.app.ws.memory_dir)
        self.assertNotIn("# Auto memory", h.model.seen[0][1])


# ======================================================================= web_fetch
PUBLIC_IP = "93.184.216.34"


async def public_resolver(host: str) -> list[str]:
    return ["10.0.0.5"] if host == "internal.example.com" else [PUBLIC_IP]


def fake_site(request):
    import httpx

    path, host = request.url.path, request.url.host
    if path == "/docs":
        html = "<html><head><style>.x{}</style><script>evil()</script></head><body><h1>Panduan Instalasi</h1><p>Jalankan <code>pip install maba</code>.</p></body></html>"
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=html)
    if path == "/lama":
        return httpx.Response(301, headers={"location": "/docs"})
    if path == "/pindah":
        return httpx.Response(302, headers={"location": "https://other.example.org/x"})
    if path == "/gambar":
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG")
    if path == "/besar":
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="a" * 200_000)
    return httpx.Response(404, text=f"tidak ada {host}{path}")


def mock_fetcher():
    import httpx

    from maba_code.web import WebFetcher

    return WebFetcher(transport=httpx.MockTransport(fake_site), resolver=public_resolver)


class WebFetchUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_html_is_converted_and_scripts_dropped(self):
        page = await mock_fetcher().fetch("http://docs.example.com/docs")
        self.assertTrue(page.url.startswith("https://"), "http di-upgrade ke https")
        self.assertIn("# Panduan Instalasi", page.text)
        self.assertIn("pip install maba", page.text)
        self.assertNotIn("evil()", page.text)

    async def test_redirects(self):
        fetcher = mock_fetcher()
        same = await fetcher.fetch("https://docs.example.com/lama")
        self.assertTrue(same.url.endswith("/docs"), "redirect host sama diikuti")
        cross = await fetcher.fetch("https://docs.example.com/pindah")
        self.assertEqual(cross.redirect, "https://other.example.org/x", "redirect lintas host dikembalikan")

    async def test_refusals(self):
        from maba_code.web import FetchError

        fetcher = mock_fetcher()
        for url in ("https://localhost/x", "https://intranet/x", "https://127.0.0.1/x", "https://[::1]/x",
                    "https://10.1.2.3/x", "https://user:pw@docs.example.com/", "ftp://docs.example.com/",
                    "https://internal.example.com/x", "https://docs.example.com/gambar"):
            with self.assertRaises(FetchError, msg=url):
                await fetcher.fetch(url)

    async def test_size_cap_and_cache(self):
        fetcher = mock_fetcher()
        page = await fetcher.fetch("https://docs.example.com/besar")
        self.assertTrue(page.truncated)
        self.assertEqual(len(page.text), 100_000)
        self.assertIs(await fetcher.fetch("https://docs.example.com/besar"), page, "cache 15 menit")


class WebFetchFlowTests(ExtCase):
    async def test_session_allow_by_domain(self):
        h = self.harness(
            [
                tool_turn([("f1", "web_fetch", {"url": "https://docs.example.com/docs", "prompt": "cara instal?"})]),
                text_turn("Jawaban: pip install maba"),  # sub-agen web
                tool_turn([("f2", "web_fetch", {"url": "https://docs.example.com/lama", "prompt": "lagi"})]),
                text_turn("sama"),  # sub-agen web
                text_turn("ok"),
            ],
            answers=["allow_session"],
        )
        h.app.web = mock_fetcher()
        await h.app.run_prompt("cari cara instal")
        self.assertEqual(h.result_for("f1")[0], "success")
        self.assertIn("Jawaban: pip install maba", h.result_for("f1")[1])
        self.assertEqual(len(h.asked), 1, "domain yang sama tidak ditanya lagi")
        self.assertIn("web_fetch(domain:docs.example.com)", [str(r) for r in h.app.policy.session_allow])
        web_call = h.model.seen[1]
        self.assertIn("<web-page", web_call[0][0]["content"][0]["text"])
        self.assertIn("untrusted", web_call[1])
        self.assertEqual(h.model.seen_tools[1], [], "sub-agen web tidak punya tool")


# ============================================================================= MCP
ECHO_SERVER = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"


def echo_server_config() -> dict:
    return {"command": sys.executable, "args": [str(ECHO_SERVER)]}


class McpUnitTests(unittest.TestCase):
    def test_tool_names_are_bedrock_safe(self):
        from maba_code.mcp_support import mcp_tool_name

        self.assertEqual(mcp_tool_name("echo", "weird.name"), "mcp__echo__weird_name")
        self.assertEqual(mcp_tool_name("my server", "list-issues"), "mcp__my_server__list-issues")
        long = mcp_tool_name("server", "x" * 100)
        self.assertEqual(len(long), 64)
        self.assertNotEqual(long, mcp_tool_name("server", "x" * 99), "nama panjang tetap unik (hash)")
        self.assertRegex(long, r"^[A-Za-z0-9_-]{1,64}$")

    def test_project_config_cannot_expand_secret_variables(self):
        from maba_code.mcp_support import parse_servers

        warnings: list[str] = []
        spec = {"gh": {"command": "npx", "env": {"T": "${GITHUB_TOKEN}"}}, "ok": {"command": "x", "args": ["${HOME_DIR:-/tmp}"]}}
        with mock.patch.dict("os.environ", {"GITHUB_TOKEN": "rahasia"}):
            project = parse_servers(spec, "proyek", warnings)
            user = parse_servers(spec, "pengguna", [])
        self.assertNotIn("gh", project)
        self.assertTrue(any("GITHUB_TOKEN" in w for w in warnings))
        self.assertEqual(project["ok"].args, ["/tmp"])
        self.assertEqual(user["gh"].env["T"], "rahasia", "konfigurasi pengguna dipercaya")

    def test_permission_rules_for_mcp_tools(self):
        self.assertTrue(Rule.parse("mcp__github").matches("mcp__github__list_issues", []))
        self.assertFalse(Rule.parse("mcp__github").matches("mcp__github2__x", []))
        self.assertTrue(Rule.parse("mcp__github__*").matches("mcp__github__create_pr", []))
        self.assertTrue(Rule.parse("mcp__my-srv__tool").matches("mcp__my-srv__tool", []))
        self.assertFalse(Rule.parse("mcp__github__list_issues").matches("mcp__github__create_pr", []))
        self.assertTrue(Rule.parse("web_fetch(domain:*.python.org)").matches("web_fetch", ["domain:docs.python.org", "https://docs.python.org/3"]))


class McpIntegrationTests(ExtCase):
    async def test_user_server_tools_are_callable_and_asked(self):
        h = self.harness(
            [tool_turn([("m1", "mcp__echo__echo", {"text": "halo"})]), text_turn("ok")],
            answers=["allow_session"], mcp_servers={"echo": echo_server_config()},
        )
        await h.app.start()
        try:
            names = [t.tool_name for t in h.app.mcp_tools]
            self.assertIn("mcp__echo__echo", names)
            self.assertIn("mcp__echo__weird_name", names)
            await h.app.run_prompt("pakai echo")
            status, text = h.result_for("m1")
            self.assertEqual(status, "success")
            self.assertIn("echo: halo", text)
            self.assertEqual(len(h.asked), 1, "tool MCP selalu ditanya")
            self.assertIn("mcp__echo__echo", h.model.seen_tools[0])
            self.assertIn("mcp__echo__echo", [str(r) for r in h.app.policy.session_allow])
        finally:
            await h.app.shutdown()
        self.assertEqual(h.app.mcp.states["echo"].status, "dihentikan")

    async def test_project_server_needs_approval_once(self):
        from maba_code.session import ChatApp

        h = self.harness([text_turn("ok")], answers=["deny"])
        (h.cwd / ".mcp.json").write_text(json.dumps({"mcpServers": {"echo": echo_server_config()}}), encoding="utf-8")
        app = ChatApp(h.settings, h.cwd, h.ui, model_factory=h.model_factory)
        await app.start()
        await app.shutdown()
        self.assertEqual(len(h.asked), 1)
        self.assertEqual(app.mcp.states["echo"].status, "ditolak pengguna")
        self.assertEqual(app.mcp_tools, [])

        again = ChatApp(h.settings, h.cwd, h.ui, model_factory=h.model_factory)
        await again.start()
        await again.shutdown()
        self.assertEqual(len(h.asked), 1, "penolakan diingat; tidak ditanya lagi")

        trust = Path(h.app.mcp.trust.path)
        trust.unlink()
        h.answers.append("allow")
        approved = ChatApp(h.settings, h.cwd, h.ui, model_factory=h.model_factory)
        await approved.start()
        try:
            self.assertEqual(len(h.asked), 2)
            self.assertEqual(approved.mcp.states["echo"].status, "terhubung")
            self.assertIn("mcp__echo__echo", [t.tool_name for t in approved.agent.tool_registry.registry.values()])
        finally:
            await approved.shutdown()
        self.assertIn("ditolak", h.out)


class NewCommandsSmokeTests(ExtCase):
    async def test_new_slash_commands_run(self):
        from maba_code.commands import CommandRegistry

        h = self.harness([text_turn("ok")])
        registry = CommandRegistry(h.app)
        for line in ("/agents", "/skills", "/tasks", "/mcp", "/memory", "/tools", "/help", "/tasks stop bg-tidakada"):
            await registry.dispatch(line)
        out = h.out
        self.assertIn("general-purpose", out)
        self.assertIn("web_fetch", out)
        self.assertIn("internet (ditanya)", out)
        self.assertIn("Memori otomatis", out)
        self.assertIn("Task tidak ditemukan", out)
        self.assertNotIn("Perintah tidak dikenal", out)


# ==================================================================== kunci GPT-6 Luna
class LunaPinTests(ExtCase):
    async def test_every_model_backed_feature_uses_luna_after_model_switch(self):
        from strands.agent.conversation_manager.summarizing_conversation_manager import DEFAULT_SUMMARIZATION_PROMPT

        from maba_code.commands import CommandRegistry
        from maba_code.config import DEFAULT_MODEL, MODEL_ALIASES

        self.assertEqual(CORE_MODEL, "global.openai.gpt-6-luna")
        self.assertEqual(DEFAULT_MODEL, CORE_MODEL)
        self.assertEqual(MODEL_ALIASES["luna"], CORE_MODEL)

        main_turns = [
            tool_turn([("x1", "explore", {"task": "cari foo"})]),
            tool_turn([("f1", "web_fetch", {"url": "https://docs.example.com/docs", "prompt": "instal?"})]),
            tool_turn([("s1", "skill", {"name": "audit"})]),
            tool_turn([("t1", "task", {"subagent_type": "general-purpose", "prompt": "cek"})]),
        ] + [text_turn(f"utama {i}") for i in range(8)]
        core_turns = [
            text_turn("LAPORAN-EXPLORE"), text_turn("JAWABAN-WEB"), text_turn("LAPORAN-SKILL"),
            text_turn("LAPORAN-GENERAL"), text_turn("RINGKASAN-LUNA"),
        ]
        h = self.harness(main_turns, core_turns=core_turns, mode="bypass")
        write_skill(h.cwd / ".maba" / "skills", "audit", "description: Audit\ncontext: fork\nagent: explore", "Audit repo.")
        h.app.web = mock_fetcher()
        registry = CommandRegistry(h.app)
        await registry.dispatch("/model sonnet")
        self.assertEqual(h.app.settings.model, "global.anthropic.claude-sonnet-5-5")
        self.assertIs(h.app.agent.model, h.model, "agen utama memakai model pilihan /model")

        await h.app.run_prompt("kerjakan semuanya")
        self.assertIn("LAPORAN-EXPLORE", h.result_for("x1")[1])
        self.assertIn("JAWABAN-WEB", h.result_for("f1")[1])
        self.assertIn("LAPORAN-SKILL", h.result_for("s1")[1])
        self.assertIn("LAPORAN-GENERAL", h.result_for("t1")[1])
        for _ in range(4):
            await h.app.run_prompt("lanjut")
        before_main_calls = h.model.i
        before, after = await h.app.compact()
        self.assertLess(after, before)

        self.assertEqual(h.model.i, before_main_calls, "kompaksi tidak memanggil model agen utama")
        self.assertEqual(h.core_model.i, 5, "explore, web_fetch, skill fork, general-purpose, kompaksi -> Luna")
        prompts = [system for _, system in h.core_model.seen]
        self.assertIn("read-only research sub-agent", prompts[0])
        self.assertIn("untrusted", prompts[1])
        self.assertIn("read-only research sub-agent", prompts[2], "skill fork lewat explore")
        self.assertIn("general-purpose sub-agent", prompts[3])
        self.assertEqual(prompts[4], DEFAULT_SUMMARIZATION_PROMPT)
        for prompt in (prompts[0], prompts[2], prompts[3]):
            self.assertIn(CORE_MODEL, prompt, "blok environment sub-agen menyebut GPT-6 Luna")
        for _, system in h.model.seen:
            self.assertNotIn("read-only research sub-agent", system)
        self.assertEqual(h.factory_calls[0], CORE_MODEL)


if __name__ == "__main__":
    unittest.main()
