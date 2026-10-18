"""Uji integrasi: loop agen Strands nyata + hook izin + UI + tool, dengan model skrip (offline)."""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from maba_code.commands import CommandRegistry
from maba_code.session import ChatApp
from tests.scripted import Harness, text_turn, tool_turn


class AgentCase(unittest.IsolatedAsyncioTestCase):
    def harness(self, *args, **kwargs) -> Harness:
        h = Harness(*args, **kwargs)
        self.addCleanup(h.close)
        return h


class EditFlowTests(AgentCase):
    async def test_read_then_edit_in_accept_edits_mode(self):
        h = self.harness(
            [
                tool_turn([("r1", "read_file", {"path": "a.txt"})]),
                tool_turn([("e1", "edit_file", {"path": "a.txt", "old_string": "hello", "new_string": "halo"})]),
                text_turn("Selesai mengedit."),
            ],
            mode="acceptEdits",
        )
        h.write("a.txt", "hello\nworld\n")
        stats = await h.app.run_prompt("ganti hello")
        self.assertIsNone(stats.error)
        self.assertEqual((h.cwd / "a.txt").read_text(), "halo\nworld\n")
        self.assertEqual(h.result_for("r1")[0], "success")
        self.assertEqual(h.result_for("e1")[0], "success")
        self.assertEqual(h.asked, [], "acceptEdits tidak boleh memunculkan dialog untuk edit di folder kerja")
        self.assertIn("Selesai mengedit.", h.out)
        self.assertIn("+halo", h.out)  # diff ringkas ditampilkan setelah edit
        self.assertGreater(stats.tokens_in, 0)

    async def test_edit_without_read_is_rejected_without_prompting(self):
        h = self.harness(
            [tool_turn([("e1", "edit_file", {"path": "a.txt", "old_string": "hello", "new_string": "x"})]), text_turn("ok")],
        )
        h.write("a.txt", "hello\n")
        await h.app.run_prompt("edit")
        status, text = h.result_for("e1")
        self.assertEqual(status, "error")
        self.assertIn("read_file", text)
        self.assertEqual((h.cwd / "a.txt").read_text(), "hello\n")
        self.assertEqual(h.asked, [])

    async def test_edit_fails_if_file_changed_on_disk_after_read(self):
        h = self.harness(
            [
                tool_turn([("r1", "read_file", {"path": "a.txt"})]),
                text_turn("sudah dibaca"),
                tool_turn([("e1", "edit_file", {"path": "a.txt", "old_string": "v1", "new_string": "v3"})]),
                text_turn("selesai"),
            ],
            mode="acceptEdits",
        )
        path = h.write("a.txt", "v1\n")
        await h.app.run_prompt("baca")
        self.assertTrue(h.app.ws.was_read_fresh(path))
        path.write_text("v1 diubah dari luar\n", encoding="utf-8")  # pengguna/editor mengubah file
        self.assertFalse(h.app.ws.was_read_fresh(path))
        await h.app.run_prompt("edit")
        status, text = h.result_for("e1")
        self.assertEqual(status, "error")
        self.assertIn("read_file", text)
        self.assertEqual(path.read_text(encoding="utf-8"), "v1 diubah dari luar\n")

    async def test_write_existing_preserves_crlf_and_creates_checkpoint(self):
        h = self.harness(
            [
                tool_turn([("r1", "read_file", {"path": "w.txt"})]),
                tool_turn([("w1", "write_file", {"path": "w.txt", "content": "x\ny\n"}),
                           ("w2", "write_file", {"path": "baru/n.txt", "content": "baru\n"})]),
                text_turn("ok"),
            ],
            mode="acceptEdits",
        )
        h.write("w.txt", "satu\ndua\n", newline="\r\n")
        await h.app.run_prompt("tulis")
        self.assertEqual((h.cwd / "w.txt").read_bytes(), b"x\r\ny\r\n")
        self.assertTrue((h.cwd / "baru" / "n.txt").exists())
        report = h.app.ws.undo_last_turn()
        self.assertEqual(len(report), 2)
        self.assertEqual((h.cwd / "w.txt").read_bytes(), b"satu\r\ndua\r\n")
        self.assertFalse((h.cwd / "baru" / "n.txt").exists())

    async def test_write_existing_file_requires_prior_read(self):
        h = self.harness([tool_turn([("w1", "write_file", {"path": "w.txt", "content": "baru"})]), text_turn("ok")], mode="acceptEdits")
        h.write("w.txt", "lama")
        await h.app.run_prompt("tulis")
        self.assertEqual(h.result_for("w1")[0], "error")
        self.assertEqual((h.cwd / "w.txt").read_text(), "lama")


class PermissionFlowTests(AgentCase):
    async def test_dialog_shows_diff_and_allow_applies_edit(self):
        h = self.harness(
            [
                tool_turn([("r1", "read_file", {"path": "a.txt"})]),
                tool_turn([("e1", "edit_file", {"path": "a.txt", "old_string": "hello", "new_string": "halo"})]),
                text_turn("ok"),
            ],
            answers=["allow"],
        )
        h.write("a.txt", "hello\n")
        await h.app.run_prompt("x")
        self.assertEqual((h.cwd / "a.txt").read_text(), "halo\n")
        self.assertEqual(len(h.asked), 1)
        self.assertIn("Izin diperlukan", h.out)
        self.assertIn("-hello", h.out)
        self.assertIn("+halo", h.out)

    async def test_deny_with_comment_blocks_and_informs_model(self):
        h = self.harness(
            [tool_turn([("w1", "write_file", {"path": "x.py", "content": "print(1)"})]), text_turn("baik")],
            answers=["comment"], texts=["jangan buat file ini"],
        )
        await h.app.run_prompt("buat x.py")
        self.assertFalse((h.cwd / "x.py").exists())
        status, text = h.result_for("w1")
        self.assertEqual(status, "error")
        self.assertIn("denied", text)
        self.assertIn("jangan buat file ini", text)
        self.assertIn("a", h.asked[0])  # opsi 'jangan tanya lagi' tersedia untuk file di folder kerja

    async def test_allow_for_session_stops_further_prompts_for_same_command_prefix(self):
        h = self.harness(
            [
                tool_turn([("c1", "run_command", {"command": "echo satu"})]),
                text_turn("a"),
                tool_turn([("c2", "run_command", {"command": "echo dua"})]),
                text_turn("b"),
            ],
            answers=["allow_session"],
        )
        await h.app.run_prompt("1")
        await h.app.run_prompt("2")
        self.assertEqual(len(h.asked), 1)
        self.assertIn("satu", h.result_for("c1")[1])
        self.assertIn("dua", h.result_for("c2")[1])
        self.assertIn("run_command(echo *)", [str(r) for r in h.app.policy.session_allow])

    async def test_secret_file_read_is_blocked_by_policy(self):
        h = self.harness([tool_turn([("r1", "read_file", {"path": ".env"})]), text_turn("ok")], mode="bypass")
        h.write(".env", "TOKEN=abc123\n")
        await h.app.run_prompt("baca env")
        status, text = h.result_for("r1")
        self.assertEqual(status, "error")
        self.assertIn("Blocked by policy", text)
        self.assertNotIn("abc123", text)
        self.assertIn("ditolak", h.out)

    async def test_plan_mode_blocks_writes_then_plan_approval_switches_mode(self):
        h = self.harness(
            [
                tool_turn([("w1", "write_file", {"path": "x.py", "content": "1"})]),
                tool_turn([("p1", "exit_plan_mode", {"plan": "# Rencana\n1. buat x.py"})]),
                text_turn("siap"),
            ],
            mode="plan", answers=["acceptEdits"],
        )
        await h.app.run_prompt("rencanakan")
        self.assertFalse((h.cwd / "x.py").exists())
        self.assertIn("Plan mode is active", h.result_for("w1")[1])
        self.assertEqual(h.result_for("p1")[0], "success")
        self.assertEqual(h.app.policy.mode, "acceptEdits")
        self.assertIn("Rencana dari agen", h.out)

    async def test_plan_rejection_keeps_plan_mode(self):
        h = self.harness([tool_turn([("p1", "exit_plan_mode", {"plan": "rencana"})]), text_turn("revisi")], mode="plan", answers=["keep"])
        await h.app.run_prompt("x")
        self.assertEqual(h.result_for("p1")[0], "error")
        self.assertEqual(h.app.policy.mode, "plan")

    async def test_non_interactive_denies_prompts(self):
        h = self.harness([tool_turn([("w1", "write_file", {"path": "x.py", "content": "1"})]), text_turn("ok")])
        h.ui.interactive = False
        await h.app.run_prompt("x")
        self.assertFalse((h.cwd / "x.py").exists())
        self.assertIn("--mode acceptEdits", h.result_for("w1")[1])


class ToolBehaviourTests(AgentCase):
    async def test_parallel_reads_all_succeed(self):
        h = self.harness(
            [tool_turn([(f"r{i}", "read_file", {"path": f"f{i}.txt"}) for i in range(3)]), text_turn("ok")]
        )
        for i in range(3):
            h.write(f"f{i}.txt", f"isi {i}\n")
        await h.app.run_prompt("baca semua")
        for i in range(3):
            status, text = h.result_for(f"r{i}")
            self.assertEqual(status, "success")
            self.assertIn(f"isi {i}", text)
            self.assertIn("     1\t", text)  # nomor baris gaya cat -n

    async def test_run_command_failure_is_reported_as_error(self):
        h = self.harness([tool_turn([("c1", "run_command", {"command": "exit 3"})]), text_turn("ok")], mode="bypass")
        await h.app.run_prompt("x")
        status, text = h.result_for("c1")
        self.assertEqual(status, "error")
        self.assertIn("[exit code: 3]", text)

    async def test_run_command_timeout(self):
        h = self.harness(
            [tool_turn([("c1", "run_command", {"command": "Start-Sleep -Seconds 30", "timeout_seconds": 1})]), text_turn("ok")],
            mode="bypass",
        )
        await h.app.run_prompt("x")
        status, text = h.result_for("c1")
        self.assertEqual(status, "error")
        self.assertIn("timed out", text)

    async def test_todo_write_validates_and_renders(self):
        todos = [{"content": "Baca kode", "status": "completed"}, {"content": "Tulis tes", "status": "in_progress"}, {"content": "Rilis", "status": "pending"}]
        h = self.harness([tool_turn([("t1", "todo_write", {"todos": todos})]), text_turn("ok")])
        await h.app.run_prompt("x")
        self.assertEqual(h.result_for("t1")[0], "success")
        self.assertEqual([t["status"] for t in h.app.ws.todos], ["completed", "in_progress", "pending"])
        for marker in ("[x] Baca kode", "[~] Tulis tes", "[ ] Rilis"):
            self.assertIn(marker, h.out)

    async def test_todo_write_rejects_invalid_status(self):
        h = self.harness([tool_turn([("t1", "todo_write", {"todos": [{"content": "x", "status": "ngawur"}]})]), text_turn("ok")])
        await h.app.run_prompt("x")
        self.assertEqual(h.result_for("t1")[0], "error")

    async def _search(self, h: Harness):
        h.write("src/a.py", "def foo():\n    return 1\n")
        h.write("src/b.txt", "foo bar\n")
        h.write("node_modules/x.py", "def foo(): pass\n")
        await h.app.run_prompt("cari")

    async def test_grep_and_glob_with_ripgrep(self):
        h = self.harness(
            [
                tool_turn([
                    ("g1", "grep_files", {"pattern": "def foo", "output_mode": "content"}),
                    ("g2", "glob_files", {"pattern": "**/*.py"}),
                    ("g3", "grep_files", {"pattern": "foo", "glob": "*.txt"}),
                ]),
                text_turn("ok"),
            ]
        )
        await self._search(h)
        self.assertIn("src/a.py:1:def foo", h.result_for("g1")[1].replace("\\", "/"))
        self.assertNotIn("node_modules", h.result_for("g1")[1])
        self.assertEqual(h.result_for("g2")[1].strip(), "src/a.py")
        self.assertEqual(h.result_for("g3")[1].replace("\\", "/").strip(), "src/b.txt")

    async def test_grep_python_fallback_matches_ripgrep(self):
        h = self.harness([tool_turn([("g1", "grep_files", {"pattern": "def foo", "output_mode": "content"})]), text_turn("ok")])
        with mock.patch("maba_code.tools.shutil.which", return_value=None):
            await self._search(h)
        out = h.result_for("g1")[1].replace("\\", "/")
        self.assertIn("src/a.py:1:def foo", out)
        self.assertNotIn("node_modules", out)

    async def test_grep_never_returns_secret_files(self):
        h = self.harness([tool_turn([("g1", "grep_files", {"pattern": "SECRET", "output_mode": "content"})]), text_turn("ok")])
        h.write(".env", "SECRET=1\n")
        h.write("app.py", "# SECRET comment\n")
        await h.app.run_prompt("x")
        out = h.result_for("g1")[1]
        self.assertIn("app.py", out)
        self.assertNotIn(".env", out)

    async def test_explore_subagent_returns_report(self):
        h = self.harness(
            [
                tool_turn([("x1", "explore", {"task": "di mana foo?"})]),
                tool_turn([("s1", "grep_files", {"pattern": "foo"})]),  # giliran sub-agen
                text_turn("LAPORAN: foo ada di src/a.py"),  # sub-agen selesai
                text_turn("Sudah ketemu."),
            ]
        )
        h.write("src/a.py", "def foo(): pass\n")
        stats = await h.app.run_prompt("cari foo")
        status, text = h.result_for("x1")
        self.assertEqual(status, "success")
        self.assertIn("LAPORAN", text)
        self.assertIn("Sudah ketemu.", h.out)
        self.assertGreater(h.app.ws.sub_usage["in"], 0)
        self.assertGreaterEqual(stats.tokens_in, h.app.ws.sub_usage["in"])


class SessionTests(AgentCase):
    async def test_memory_file_is_injected_into_system_prompt(self):
        h = self.harness([text_turn("ok")])
        h.write("MABA.md", "ATURAN-PROYEK: gunakan tab.\n")
        await h.app.run_prompt("halo")
        _, system_prompt = h.model.seen[0]
        self.assertIn("ATURAN-PROYEK: gunakan tab.", system_prompt)
        self.assertIn("MABA Code", system_prompt)

    async def test_turn_limit_stops_runaway_loop(self):
        h = self.harness([tool_turn([("r1", "glob_files", {"pattern": "*.py"})])], max_turns=2)
        stats = await h.app.run_prompt("loop")
        self.assertEqual(stats.stop_reason, "limit_turns")
        self.assertIn("Batas 2 giliran", h.out)

    async def test_cancel_midstream_then_continue(self):
        h = self.harness([text_turn("kata " * 80, delay=0.03, chunk=5), text_turn("pulih")])
        asyncio.get_running_loop().call_later(0.4, h.app.cancel)
        stats = await h.app.run_prompt("panjang")
        self.assertTrue(stats.cancelled)
        self.assertIn("Dihentikan", h.out)
        stats2 = await h.app.run_prompt("lanjut")
        self.assertIsNone(stats2.error)
        self.assertIn("pulih", h.out)
        roles = [m["role"] for m in h.app.agent.messages]
        self.assertTrue(all(a != b for a, b in zip(roles, roles[1:])), f"peran harus bergantian: {roles}")

    async def test_session_persists_and_resumes(self):
        h = self.harness([text_turn("jawaban pertama")])
        await h.app.run_prompt("pertanyaan pertama")
        sid = h.app.session_id
        self.assertEqual(h.app.list_sessions()[0]["id"], sid)
        again = ChatApp(h.settings, h.cwd, h.ui, session_id=sid, model_factory=lambda s: h.model)
        self.assertTrue(again.resumed)
        texts = [b.get("text", "") for m in again.agent.messages for b in m["content"]]
        self.assertTrue(any("pertanyaan pertama" in t for t in texts))
        self.assertTrue(any("jawaban pertama" in t for t in texts))

    async def test_new_session_and_resume_by_number(self):
        h = self.harness([text_turn("a"), text_turn("b")])
        await h.app.run_prompt("sesi satu")
        first = h.app.session_id
        h.app.new_session()
        self.assertNotEqual(first, h.app.session_id)
        self.assertEqual(len(h.app.agent.messages), 0)
        count = h.app.resume_session(first)
        self.assertGreaterEqual(count, 2)

    async def test_compose_prompt_pads_when_last_message_is_user(self):
        h = self.harness([text_turn("ok")])
        h.app.agent.messages.append({"role": "user", "content": [{"text": "menggantung"}]})
        composed = h.app._compose_prompt("baru")
        self.assertIsInstance(composed, list)
        self.assertEqual([m["role"] for m in composed], ["assistant", "user"])

    async def test_user_shell_blocks_secrets_and_adds_context(self):
        h = self.harness([text_turn("ok")])
        await h.app.run_user_shell("type .env")
        self.assertIn("Diblokir", h.out)
        self.assertEqual(h.app.pending_context, [])
        await h.app.run_user_shell("echo halo")
        self.assertEqual(len(h.app.pending_context), 1)
        self.assertIn("halo", h.app.pending_context[0])

    async def test_mode_cycle_and_system_prompt_refresh(self):
        h = self.harness([text_turn("ok")])
        self.assertEqual(h.app.cycle_mode(), "acceptEdits")
        self.assertEqual(h.app.cycle_mode(), "plan")
        self.assertIn("Plan mode is ACTIVE", h.app.agent.system_prompt)
        self.assertEqual(h.app.cycle_mode(), "default")
        self.assertNotIn("Plan mode is ACTIVE", h.app.agent.system_prompt)

    async def test_compact_reduces_messages(self):
        turns = [text_turn(f"jawab {i}") for i in range(12)] + [text_turn("RINGKASAN percakapan sebelumnya")]
        h = self.harness(turns)
        for i in range(12):
            await h.app.run_prompt(f"pertanyaan {i}")
        before, after = await h.app.compact()
        self.assertLess(after, before)


class CommandTests(AgentCase):
    async def test_custom_command_expands_arguments(self):
        h = self.harness([text_turn("ok")])
        folder = h.cwd / ".maba" / "commands"
        folder.mkdir(parents=True)
        (folder / "review.md").write_text("---\ndescription: Tinjau kode\n---\nTinjau berkas ini: $ARGUMENTS", encoding="utf-8")
        registry = CommandRegistry(h.app)
        self.assertIn("review", [n for n, _ in h.app.command_names()])
        await registry.dispatch("/review src/app.py")
        first = h.app.agent.messages[0]["content"][0]["text"]
        self.assertEqual(first, "Tinjau berkas ini: src/app.py")

    async def test_builtin_commands_run(self):
        h = self.harness([text_turn("ok")])
        registry = CommandRegistry(h.app)
        for line in ("/help", "/tools", "/permissions", "/config", "/mode", "/model", "/cost", "/todo", "/memory", "/resume"):
            await registry.dispatch(line)
        await registry.dispatch("/mode plan")
        self.assertEqual(h.app.policy.mode, "plan")
        await registry.dispatch("/model sonnet")
        self.assertEqual(h.app.settings.model, "global.anthropic.claude-sonnet-5-5")
        await registry.dispatch("/tidakada")
        self.assertIn("Perintah tidak dikenal", h.out)
        self.assertIn("global.openai.gpt-6-luna", h.out)

    async def test_exit_raises(self):
        from maba_code.commands import ExitRequested

        h = self.harness([text_turn("ok")])
        registry = CommandRegistry(h.app)
        with self.assertRaises(ExitRequested):
            await registry.dispatch("/exit")


if __name__ == "__main__":
    unittest.main()
