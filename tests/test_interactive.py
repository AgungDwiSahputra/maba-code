"""Uji lapisan interaktif prompt_toolkit memakai pipe input (tanpa TTY nyata)."""

from __future__ import annotations

import asyncio
import unittest

from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from maba_code.commands import CommandRegistry
from maba_code.repl import run_repl
from maba_code.ui.prompt import FileIndex, MabaCompleter, create_prompt_session
from tests.scripted import Harness, text_turn, tool_turn


class InteractiveCase(unittest.IsolatedAsyncioTestCase):
    def harness(self, *args, **kwargs) -> Harness:
        h = Harness(*args, **kwargs)
        self.addCleanup(h.close)
        return h


class PromptKeyTests(InteractiveCase):
    async def test_enter_alt_enter_and_backslash_continuation(self):
        h = self.harness([text_turn("x")])
        CommandRegistry(h.app)
        with create_pipe_input() as inp:
            session = create_prompt_session(h.app, "> ", input=inp, output=DummyOutput())
            inp.send_text("halo dunia\r")
            self.assertEqual(await session.prompt_async(), "halo dunia")
            inp.send_text("baris1\x1b\rbaris2\r")  # Alt+Enter = baris baru
            self.assertEqual(await session.prompt_async(), "baris1\nbaris2")
            inp.send_text("satu\\\rdua\r")  # backslash + Enter = lanjut baris
            self.assertEqual(await session.prompt_async(), "satu\ndua")

    async def test_slash_and_at_file_completion(self):
        h = self.harness([text_turn("x")])
        CommandRegistry(h.app)
        h.write("src/modul_utama.py", "x = 1\n")
        h.write(".env", "S=1\n")
        loop = asyncio.get_running_loop()
        with create_pipe_input() as inp:
            session = create_prompt_session(h.app, "> ", input=inp, output=DummyOutput())
            # Completion berjalan asinkron: beri jeda antara Tab dan Enter seperti pengguna sungguhan.
            inp.send_text("/hel\t")
            loop.call_later(0.4, inp.send_text, "\r")
            self.assertEqual(await session.prompt_async(), "/help")
            inp.send_text("lihat @modul\t")
            loop.call_later(0.4, inp.send_text, "\r")
            self.assertEqual(await session.prompt_async(), "lihat @src/modul_utama.py ", "Tab memilih file + spasi")
        completions = [c.text for c in session.completer.get_completions(Document("@"), None)]
        self.assertIn("src/modul_utama.py", completions)
        self.assertNotIn(".env", completions, "file rahasia tidak boleh muncul di autocomplete")

    async def test_menu_opens_while_typing_with_highlight(self):
        h = self.harness([text_turn("x")])
        CommandRegistry(h.app)
        h.write("src/modul_utama.py", "x = 1\n")
        loop = asyncio.get_running_loop()
        seen: dict[str, list] = {}
        with create_pipe_input() as inp:
            session = create_prompt_session(h.app, "> ", input=inp, output=DummyOutput())
            self.assertTrue(session.default_buffer.complete_while_typing(), "menu harus muncul saat mengetik, tanpa Tab")

            def snapshot_then_enter(key: str) -> None:
                state = session.default_buffer.complete_state
                seen[key] = list(state.completions) if state else []
                inp.send_text("\r")

            inp.send_text("/")
            loop.call_later(0.8, snapshot_then_enter, "slash")
            self.assertEqual(await session.prompt_async(), "/help", "Enter menjalankan item teratas")
            names = [c.text for c in seen["slash"]]
            for expected in ("help", "model", "agents", "skills", "tasks", "loop", "mcp", "exit"):
                self.assertIn(expected, names)
            self.assertEqual(seen["slash"][0].display_meta_text, "Tampilkan bantuan")

            inp.send_text("lihat @modul")
            loop.call_later(0.8, snapshot_then_enter, "file")
            loop.call_later(1.2, inp.send_text, "\r")
            self.assertEqual(await session.prompt_async(), "lihat @src/modul_utama.py ", "Enter pertama memilih file, kedua mengirim")
            self.assertEqual(seen["file"][0].text, "src/modul_utama.py")

    async def test_completion_display_highlights_match_and_lists_folders(self):
        h = self.harness([text_turn("x")])
        CommandRegistry(h.app)
        h.write("src/modul_utama.py", "x = 1\n")
        session_completer = MabaCompleter(h.app.command_names, FileIndex(h.cwd))

        def items(text: str):
            return list(session_completer.get_completions(Document(text), None))

        mo = items("/mo")
        self.assertEqual([c.text for c in mo][:2], ["model", "mode"])
        self.assertIn(("class:completion-match", "/mo"), list(mo[0].display))
        mem = items("/mor")  # memory: potongan di tengah nama
        self.assertIn("memory", [c.text for c in mem])
        self.assertIn(("class:completion-match", "mor"), list(next(c for c in mem if c.text == "memory").display))
        folder = next(c for c in items("@sr") if c.text == "src/")
        self.assertEqual(folder.display_meta_text, "folder")
        self.assertIn(("class:completion-match", "sr"), list(folder.display))
        self.assertIn(("class:completion-match", "modul"), list(items("@modul")[0].display))
        self.assertEqual(items("kirim ke a@b.com"), [], "email bukan @mention")
        self.assertEqual(items("/model luna"), [], "setelah spasi menu perintah ditutup")

    async def test_enter_rules_for_partial_slash_exact_file_and_email(self):
        h = self.harness([text_turn("x")])
        CommandRegistry(h.app)
        h.write("src/modul_utama.py", "x = 1\n")
        loop = asyncio.get_running_loop()
        with create_pipe_input() as inp:
            session = create_prompt_session(h.app, "> ", input=inp, output=DummyOutput())
            inp.send_text("/mo\r")
            self.assertEqual(await session.prompt_async(), "/model", "Enter pada '/mo' menjalankan item teratas")
            inp.send_text("baca @src/modul_utama.py\r")
            self.assertEqual(await session.prompt_async(), "baca @src/modul_utama.py", "path yang sudah lengkap langsung dikirim")
            inp.send_text("kirim ke a@b.com\r")
            self.assertEqual(await session.prompt_async(), "kirim ke a@b.com")
            inp.send_text("/tidakada\r")
            self.assertEqual(await session.prompt_async(), "/tidakada", "perintah tak dikenal tetap dikirim (lalu diberi pesan)")
            inp.send_text("@sr\t")
            loop.call_later(0.6, inp.send_text, "modul\t")
            loop.call_later(1.2, inp.send_text, "\r")
            self.assertEqual(await session.prompt_async(), "@src/modul_utama.py ", "Tab pada folder lalu masuk ke isinya")

    async def test_shift_tab_cycles_mode(self):
        h = self.harness([text_turn("x")])
        with create_pipe_input() as inp:
            session = create_prompt_session(h.app, "> ", input=inp, output=DummyOutput())
            inp.send_text("\x1b[Z\r")  # Shift+Tab lalu Enter
            self.assertEqual(await session.prompt_async(), "")
        self.assertEqual(h.app.policy.mode, "acceptEdits")

    async def test_toolbar_shows_mode_and_model(self):
        h = self.harness([text_turn("x")])
        text = "".join(t for _, t in h.app.toolbar_fragments())
        self.assertIn("standar", text)
        self.assertIn("Shift+Tab", text)
        h.app.set_mode("bypass")
        self.assertIn("BYPASS", "".join(t for _, t in h.app.toolbar_fragments()))


class DialogKeyTests(InteractiveCase):
    async def test_single_key_choices(self):
        h = self.harness([text_turn("x")])
        mapping = {"y": "allow", "enter": "allow", "n": "deny", "escape": "deny", "e": "comment", "a": "allow_session"}
        with create_pipe_input() as inp:
            h.ui.pt_input, h.ui.pt_output = inp, DummyOutput()
            for keys, expected in (("y", "allow"), ("n", "deny"), ("a", "allow_session"), ("e", "comment"), ("\r", "allow"), ("zzxn", "deny"), ("\x03", "deny")):
                inp.send_text(keys)
                self.assertEqual(await h.ui._pt_choose(mapping, "? "), expected, repr(keys))

    async def test_real_dialog_denies_write_end_to_end(self):
        h = self.harness([tool_turn([("w1", "write_file", {"path": "x.py", "content": "1"})]), text_turn("ok")])
        with create_pipe_input() as inp:
            h.ui.pt_input, h.ui.pt_output = inp, DummyOutput()
            h.ui._choose = h.ui._pt_choose  # pakai dialog prompt_toolkit sungguhan, bukan jawaban terskrip
            inp.send_text("n")
            await h.app.run_prompt("buat x.py")
        self.assertFalse((h.cwd / "x.py").exists())
        self.assertIn("Ditolak", h.out)
        self.assertEqual(h.result_for("w1")[0], "error")

    async def test_real_dialog_allows_write_end_to_end(self):
        h = self.harness([tool_turn([("w1", "write_file", {"path": "x.py", "content": "print(1)\n"})]), text_turn("ok")])
        with create_pipe_input() as inp:
            h.ui.pt_input, h.ui.pt_output = inp, DummyOutput()
            h.ui._choose = h.ui._pt_choose
            inp.send_text("y")
            await h.app.run_prompt("buat x.py")
        self.assertEqual((h.cwd / "x.py").read_text(), "print(1)\n")
        self.assertIn("Diizinkan", h.out)


class ReplLoopTests(InteractiveCase):
    async def test_repl_handles_commands_chat_shell_and_exit(self):
        h = self.harness([text_turn("Halo juga dari model!")])
        registry = CommandRegistry(h.app)
        with create_pipe_input() as inp:
            session = create_prompt_session(h.app, "> ", input=inp, output=DummyOutput())
            inp.send_text("/mode plan\r")
            inp.send_text("halo\r")
            inp.send_text("!echo dari-shell\r")
            inp.send_text("/exit\r")
            await run_repl(h.app, registry, session=session)
        self.assertEqual(h.app.policy.mode, "plan")
        self.assertIn("Halo juga dari model!", h.out)
        self.assertIn("dari-shell", h.out)
        self.assertIn("Sampai jumpa", h.out)
        self.assertIn("global.openai.gpt-6-luna", h.out)  # banner memuat model default


if __name__ == "__main__":
    unittest.main()
