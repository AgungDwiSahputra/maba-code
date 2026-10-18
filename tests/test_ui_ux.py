"""Uji pembaruan UI/UX: status real-time, pesan berlevel, dialog izin, ringkasan giliran, toolbar, banner, /help, /status."""

from __future__ import annotations

import asyncio
import io
import time
import unittest
from pathlib import Path

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from maba_code.commands import BUILTINS, HELP_GROUPS, CommandRegistry
from maba_code.config import Settings
from maba_code.ui import preview
from maba_code.ui.activity import Activity, extract_intent
from maba_code.ui.prompt import create_prompt_session
from maba_code.ui.terminal import TerminalUI
from maba_code.ui.theme import ASCII, UNICODE, fmt_duration, meter, notice_level
from maba_code.workspace import ToolNote
from tests.scripted import Harness, text_turn, tool_turn

DIFF = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-lama\n+baru\n"


def make_ui(width: int = 100, symbols=ASCII) -> tuple[TerminalUI, io.StringIO]:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=width, color_system=None, highlight=False)
    return TerminalUI(console, symbols=symbols), buf


class UxCase(unittest.IsolatedAsyncioTestCase):
    def harness(self, *args, **kwargs) -> Harness:
        h = Harness(*args, **kwargs)
        self.addCleanup(h.close)
        return h


class NoticeLevelTests(unittest.TestCase):
    def test_color_styles_map_to_icons(self):
        ui, buf = make_ui()
        for style in ("green", "yellow", "red", "cyan", "dim"):
            ui.notice(f"pesan {style}", style)
        self.assertEqual(
            buf.getvalue().splitlines(),
            ["  v pesan green", "  ! pesan yellow", "  x pesan red", "  i pesan cyan", "  pesan dim"],
        )

    def test_level_names_work_directly(self):
        self.assertEqual(notice_level("warn"), "warn")
        self.assertEqual(notice_level("yellow"), "warn")
        self.assertEqual(notice_level("bold magenta"), "custom")

    def test_old_bang_prefix_becomes_a_warning_icon(self):
        ui, buf = make_ui()
        ui.notice("! kunci diabaikan", "dim")
        self.assertEqual(buf.getvalue().strip(), "! kunci diabaikan")

    def test_unicode_symbols_are_used_when_available(self):
        ui, buf = make_ui(symbols=UNICODE)
        ui.notice("beres", "green")
        self.assertIn("✓ beres", buf.getvalue())


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.t = [1000.0]
        self.act = Activity(clock=lambda: self.t[0])
        self.act.begin_turn()

    def text(self) -> str:
        return self.act.render().plain

    def test_think_then_write_shows_phase_words_and_elapsed(self):
        self.act.set_phase("think", "memahami permintaan Anda")
        self.t[0] += 2
        self.assertEqual(self.text(), "Berpikir: memahami permintaan Anda  2s  (Esc untuk batal)")
        self.act.set_phase("write")
        self.act.add_text("satu dua tiga")
        self.assertIn("Menulis jawaban", self.text())
        self.assertIn("3 kata", self.text())

    def test_words_are_not_double_counted_across_chunks(self):
        for chunk in ("hel", "lo wor", "ld", " lagi"):
            self.act.add_text(chunk)
        self.assertEqual(self.act.words, 3)

    def test_new_message_after_tool_result_restarts_word_count(self):
        self.act.set_phase("write")
        self.act.add_text("a b c")
        self.act.tool_started("read_file", {"path": "x.py"})
        self.act.tool_finished("read_file", {"path": "x.py"}, True, None)
        self.assertEqual(self.act.words, 0)

    def test_tool_headline_uses_progressive_label_and_step_number(self):
        self.act.tool_started("read_file", {"path": "calc.py"})
        self.act.tool_started("run_command", {"command": "pytest"}, running=1)
        text = self.text()
        self.assertTrue(text.startswith("Menjalankan pytest"), text)
        self.assertIn("langkah 2", text)

    def test_parallel_tools_are_summarised(self):
        self.act.tool_started("read_file", {"path": "a"}, running=1)
        self.act.tool_started("read_file", {"path": "b"}, running=2)
        self.assertIn("2 langkah berjalan bersamaan", self.text())
        self.act.tool_finished("read_file", {"path": "a"}, True, None, running=1)
        self.assertIn("menunggu 1 langkah lain", self.text())
        self.act.tool_finished("read_file", {"path": "b"}, True, None, running=0)
        self.assertTrue(self.text().startswith("Berpikir: membaca hasil langkah 2"))

    def test_slow_tool_shows_how_long_this_step_runs(self):
        self.act.tool_started("run_command", {"command": "build"})
        self.assertNotIn("langkah ini", self.text())
        self.t[0] += 12
        self.assertIn("langkah ini 12s", self.text())

    def test_waiting_for_the_user_is_not_counted_as_work(self):
        self.t[0] += 10
        self.act.begin_wait()
        self.t[0] += 30
        self.act.end_wait()
        self.t[0] += 5
        self.assertAlmostEqual(self.act.waited, 30)
        self.assertAlmostEqual(self.act.work_elapsed(), 15)
        self.assertIn("15s", self.text())

    def test_esc_hint_only_when_cancellable(self):
        self.act.set_phase("compact", cancellable=False)
        self.assertNotIn("Esc", self.text())
        self.act.set_phase("think")
        self.assertIn("Esc untuk batal", self.text())

    def test_no_elapsed_outside_a_turn(self):
        self.act.end_turn()
        self.act.set_phase("custom", "Loop 1/3 · berikutnya 00:05", True)
        self.assertEqual(self.text(), "Loop 1/3 · berikutnya 00:05  (Esc untuk batal)")

    def test_subagent_steps_are_counted_and_reported_once(self):
        self.act.sub_step("explore", "Membaca a.py")
        self.act.sub_step("explore", "Mencari teks x")
        self.assertIn("explore › Mencari teks x (2 langkah)", self.text())
        self.assertEqual(self.act.take_sub_steps("explore"), 2)
        self.assertEqual(self.act.take_sub_steps("explore"), 0)
        self.assertEqual(self.act.log.sub_total["explore"], 2, "total untuk ringkasan akhir tetap ada")

    def test_changes_are_recorded_for_edits_new_files_and_subagent_results(self):
        self.act.tool_finished("edit_file", {"path": "a.py"}, True, ToolNote("+1 −1", DIFF, "diff"))
        self.assertEqual(self.act.log.changes["a.py"], [1, 1])
        content = "\n".join(f"baris {i}" for i in range(100)) + "\n"
        preview_text = "\n".join(f"+baris {i}" for i in range(40)) + "\n+... (60 baris lagi)"
        self.act.tool_finished("write_file", {"path": "b.py", "content": content}, True, ToolNote("dibuat · 100 baris", preview_text, "diff"))
        self.assertEqual(self.act.log.changes["b.py"], [100, 0], "jumlah baris baru dihitung dari isi, bukan pratinjau terpotong")
        self.act.tool_finished("edit_file", {"path": "gagal.py"}, False, ToolNote("x", DIFF, "diff"))
        self.assertNotIn("gagal.py", self.act.log.changes)
        self.act.sub_tool_finished("run_command", {"command": "ls"}, True, ToolNote("exit 0"))
        self.assertEqual(self.act.log.commands, 1)

    def test_begin_turn_resets_the_log(self):
        self.act.tool_started("run_command", {"command": "x"})
        self.act.begin_turn()
        self.assertEqual((self.act.log.steps, self.act.log.commands), (0, 0))

    def test_status_line_uses_whole_seconds_but_the_summary_keeps_decimals(self):
        self.t[0] += 3.7
        self.assertIn("  3s", self.text())
        self.t[0] += 70
        self.assertIn("1m 13s", self.text())

    def test_duration_and_meter_formats(self):
        self.assertEqual([fmt_duration(s) for s in (0.84, 9.99, 14.2, 59.9, 125)], ["0.8s", "10.0s", "14s", "59s", "2m 05s"])
        self.assertEqual(meter(32, 5, UNICODE), "▰▰▱▱▱")
        self.assertEqual(meter(100, 4, ASCII), "####")
        self.assertEqual(meter(None, 3, ASCII), "---")


class IntentTests(unittest.TestCase):
    def test_takes_the_last_sentence(self):
        self.assertEqual(extract_intent("Saya baca dulu file-nya. Lalu saya akan menjalankan tes."), "Lalu saya akan menjalankan tes.")

    def test_joins_a_too_short_last_sentence_with_the_previous_one(self):
        self.assertEqual(extract_intent("Saya akan memperbaiki bug itu. Ya."), "Saya akan memperbaiki bug itu. Ya.")

    def test_strips_markdown_and_truncates(self):
        self.assertEqual(extract_intent("Menjalankan **`pytest`** sekarang:"), "Menjalankan pytest sekarang:")
        self.assertTrue(extract_intent("kata " * 100).endswith("…"))
        self.assertLessEqual(len(extract_intent("kata " * 100)), 180)

    def test_empty_text_gives_empty_intent(self):
        self.assertEqual(extract_intent("  \n "), "")


class PermissionDialogTests(UxCase):
    async def test_dangerous_command_is_flagged_and_enter_does_not_approve(self):
        h = self.harness([tool_turn([("c1", "run_command", {"command": "python build.py"})]), text_turn("ok")], answers=["deny"])
        await h.app.run_prompt("bangun")
        self.assertIn("Izin diperlukan - BERBAHAYA", h.out)
        self.assertIn("Dampak: Perintah berjalan di komputer Anda", h.out)
        self.assertIn("Enter tidak menyetujui", h.out)
        self.assertIn("Mode aktif: standar", h.out)
        self.assertNotIn("enter", h.asked[0])
        self.assertEqual(h.asked[0]["y"], "allow")

    async def test_file_write_is_not_strict_and_enter_approves(self):
        h = self.harness([tool_turn([("w1", "write_file", {"path": "x.py", "content": "print(1)\n"})]), text_turn("ok")], answers=["allow"])
        await h.app.run_prompt("buat x.py")
        self.assertIn("Izin diperlukan - TULIS", h.out)
        self.assertIn("Membuat file baru.", h.out)
        self.assertIn("Enter = Ya.", h.out)
        self.assertEqual(h.asked[0]["enter"], "allow")

    async def test_dialog_quotes_what_the_agent_said_before_the_call(self):
        h = self.harness(
            [tool_turn([("w1", "write_file", {"path": "x.py", "content": "1\n"})], preface="Saya akan membuat berkas x.py sekarang."), text_turn("ok")],
            answers=["deny"],
        )
        await h.app.run_prompt("buat")
        self.assertIn('Agen menjelaskan: "Saya akan membuat berkas x.py sekarang."', h.out)

    async def test_answers_tell_the_user_what_happens_next(self):
        h = self.harness([tool_turn([("w1", "write_file", {"path": "x.py", "content": "1\n"})]), text_turn("ok")], answers=["deny"])
        await h.app.run_prompt("buat")
        self.assertIn("Ditolak", h.out)
        self.assertIn("agen diberi tahu", h.out)
        h2 = self.harness([tool_turn([("w1", "write_file", {"path": "y.py", "content": "1\n"})]), text_turn("ok")], answers=["allow"])
        await h2.app.run_prompt("buat")
        self.assertIn("Diizinkan — agen melanjutkan", h2.out)

    async def test_overwrite_outside_the_project_is_called_out(self):
        h = self.harness([text_turn("x")])
        outside = h.cwd.parent / "luar.txt"
        outside.write_text("lama\n", encoding="utf-8")
        h.app.ws.mark_read(outside)
        request = h.app.hook.build_request("write_file", {"path": str(outside), "content": "baru\n"}, "menulis file")
        self.assertIn("DI LUAR folder kerja", request.impact)
        self.assertEqual(request.mode, "default")

    async def test_badge_and_strictness_per_tool_class(self):
        ui, _ = make_ui()
        from maba_code.permissions import PermissionRequest

        cases = {
            "edit_file": ("TULIS", False), "run_command": ("BERBAHAYA", True), "web_fetch": ("INTERNET", True),
            "mcp__gh__issue": ("MCP", True), "mcp": ("BERBAHAYA", True), "tool_aneh": ("TOOL", True),
        }
        for tool, (badge, strict) in cases.items():
            text, _style, is_strict = ui._badge(PermissionRequest(tool, "t", "x", "", "text"))
            self.assertEqual((text, is_strict), (badge, strict), tool)
        text, _style, is_strict = ui._badge(PermissionRequest("edit_file", "t", "x", danger=True))
        self.assertEqual((text, is_strict), ("BERBAHAYA", True), "danger=True menaikkan kelas biasa")

    async def test_real_prompt_ignores_enter_in_a_dangerous_dialog_until_y(self):
        """Lewat prompt_toolkit sungguhan: Enter tidak menyetujui perintah shell, 'y' baru menyetujui."""
        h = self.harness([tool_turn([("c1", "run_command", {"command": "python -c \"print(42)\""})]), text_turn("ok")])
        with create_pipe_input() as inp:
            h.ui.pt_input, h.ui.pt_output = inp, DummyOutput()
            h.ui._choose = h.ui._pt_choose
            inp.send_text("\r\r\ry")
            await h.app.run_prompt("jalankan")
        status, text = h.result_for("c1")
        self.assertEqual(status, "success", text)
        self.assertIn("42", text)
        self.assertIn("Diizinkan — agen melanjutkan", h.out)

    async def test_real_prompt_enter_still_approves_a_file_edit(self):
        h = self.harness([tool_turn([("w1", "write_file", {"path": "x.py", "content": "1\n"})]), text_turn("ok")])
        with create_pipe_input() as inp:
            h.ui.pt_input, h.ui.pt_output = inp, DummyOutput()
            h.ui._choose = h.ui._pt_choose
            inp.send_text("\r")
            await h.app.run_prompt("buat")
        self.assertTrue((h.cwd / "x.py").exists())

    async def test_plan_dialog_explains_each_option(self):
        h = self.harness([text_turn("x")], answers=["keep"])
        await h.ui.ask_plan("1. Ubah a.py")
        self.assertIn("Apakah rencana ini boleh dijalankan?", h.out)
        self.assertIn("agen mulai bekerja; edit file tanpa bertanya", h.out)
        self.assertIn("Rencana dari agen", h.out)


class TurnSummaryTests(UxCase):
    async def test_chat_only_turn_is_a_single_quiet_line(self):
        h = self.harness([text_turn("Halo!")])
        await h.app.run_prompt("hai")
        lines = [ln for ln in h.out.splitlines() if "Selesai dalam" in ln]
        self.assertEqual(len(lines), 1)
        self.assertIn("masuk", lines[0])
        self.assertNotIn("Berikutnya", h.out)
        self.assertNotIn("Diubah:", h.out)

    async def test_tool_turn_lists_changes_commands_and_undo_hint(self):
        h = self.harness(
            [
                tool_turn([("w1", "write_file", {"path": "x.py", "content": "print(1)\n"})]),
                tool_turn([("c1", "run_command", {"command": "python x.py"})]),
                text_turn("Selesai."),
            ],
            answers=["allow", "allow"],
        )
        await h.app.run_prompt("buat dan jalankan x.py")
        self.assertIn("Selesai dalam", h.out)
        self.assertIn("2 langkah", h.out)
        self.assertIn("Diubah: x.py (+1 -0)", h.out)
        self.assertIn("Dijalankan: 1 perintah", h.out)
        self.assertIn("Berikutnya: /undo membatalkan perubahan file", h.out)
        self.assertIn("diringkas otomatis di 75%", h.out)

    async def test_denied_actions_are_counted(self):
        h = self.harness([tool_turn([("w1", "write_file", {"path": "x.py", "content": "1\n"})]), text_turn("ok")], answers=["deny"])
        await h.app.run_prompt("buat")
        self.assertIn("Ditolak: 1 aksi", h.out)

    async def test_cancelled_and_limit_turns_have_their_own_headline_and_next_step(self):
        h = self.harness([tool_turn([("r1", "glob_files", {"pattern": "*.py"})])], max_turns=2)
        await h.app.run_prompt("loop")
        self.assertIn("Batas 2 giliran per perintah tercapai setelah", h.out)
        self.assertIn("ketik 'lanjut' untuk meneruskan", h.out)
        h2 = self.harness([text_turn("kata " * 80, delay=0.03, chunk=5), text_turn("pulih")])
        asyncio.get_running_loop().call_later(0.4, h2.app.cancel)
        await h2.app.run_prompt("panjang")
        self.assertIn("Dihentikan setelah", h2.out)
        self.assertIn("ketik instruksi baru untuk melanjutkan", h2.out)

    async def test_footer_separates_time_spent_waiting_for_the_user(self):
        ui, buf = make_ui()
        ui.begin_turn()
        ui.activity.waited = 31.0
        ui.turn_footer(100, 20, 10, 45.0)
        self.assertIn("Selesai dalam 14s", buf.getvalue())
        self.assertIn("menunggu Anda 31s", buf.getvalue())

    async def test_footer_warns_when_context_is_filling_up(self):
        ui, buf = make_ui()
        ui.begin_turn()
        ui.turn_footer(100, 20, 70, 1.0)
        self.assertIn("konteks 70%", buf.getvalue())
        self.assertIn("/compact untuk meringkas sekarang", buf.getvalue())
        ui2, buf2 = make_ui()
        ui2.begin_turn()
        ui2.turn_footer(100, 20, 10, 1.0)
        self.assertNotIn("/compact", buf2.getvalue())

    async def test_many_changed_files_are_abbreviated(self):
        ui, buf = make_ui()
        ui.begin_turn()
        for name in ("a.py", "b.py", "c.py", "d.py", "e.py"):
            ui.activity.log.changes[name] = [1, 0]
        ui.activity.log.steps = 5
        ui.turn_footer(1, 1, None, 1.0)
        self.assertIn("a.py (+1 -0), b.py (+1 -0), c.py (+1 -0) dan 2 file lain", buf.getvalue())

    async def test_subagent_steps_and_changes_reach_the_main_summary(self):
        h = self.harness(
            [
                tool_turn([("t1", "task", {"subagent_type": "general-purpose", "prompt": "buat catatan.txt"})]),
                tool_turn([("w1", "write_file", {"path": "catatan.txt", "content": "halo\n"})]),
                text_turn("File dibuat."),
                text_turn("Beres."),
            ],
            answers=["allow"],
        )
        await h.app.run_prompt("buat")
        self.assertIn("1 langkah", h.out)
        self.assertIn("Diubah: catatan.txt (+1 -0)", h.out)
        self.assertIn("Sub-agen: general-purpose 1 langkah", h.out)


class GuardrailVisibilityTests(UxCase):
    async def test_warns_before_the_turn_limit_is_hit(self):
        h = self.harness([tool_turn([("r1", "glob_files", {"pattern": "*.py"})])], max_turns=5)
        await h.app.run_prompt("loop")
        self.assertIn("4 dari 5 putaran model", h.out)
        self.assertEqual(h.out.count("putaran model untuk perintah ini"), 1, "peringatan hanya sekali")

    async def test_no_warning_for_tiny_limits(self):
        h = self.harness([tool_turn([("r1", "glob_files", {"pattern": "*.py"})])], max_turns=2)
        await h.app.run_prompt("loop")
        self.assertNotIn("putaran model untuk perintah ini", h.out)

    async def test_auto_compaction_is_announced(self):
        turns = [text_turn(f"jawab {i}") for i in range(12)] + [text_turn("RINGKASAN percakapan sebelumnya")]
        h = self.harness(turns)
        for i in range(12):
            await h.app.run_prompt(f"pertanyaan {i}")
        cm = h.app.agent.conversation_manager
        await asyncio.to_thread(cm.reduce_context, h.app.agent)
        self.assertIn("Konteks mendekati 75%: percakapan lama diringkas otomatis", h.out)
        self.assertRegex(h.out, r"Percakapan diringkas otomatis: \d+ → \d+ pesan")

    async def test_manual_compact_is_not_announced_twice(self):
        turns = [text_turn(f"jawab {i}") for i in range(12)] + [text_turn("RINGKASAN percakapan sebelumnya")]
        h = self.harness(turns)
        for i in range(12):
            await h.app.run_prompt(f"pertanyaan {i}")
        await h.app.compact()
        self.assertNotIn("diringkas otomatis:", h.out)


class ToolbarTests(UxCase):
    def text(self, h, **kw) -> str:
        return "".join(t for _, t in h.app.toolbar_fragments(**kw))

    def ctx_style(self, h) -> str:
        return next(s for s, t in h.app.toolbar_fragments() if "konteks" in t)

    async def test_context_meter_changes_color_with_thresholds(self):
        h = self.harness([text_turn("x")])
        h.app.last_ctx_pct = 30
        self.assertEqual(self.ctx_style(h), "class:bottom-toolbar")
        self.assertIn("konteks ##--- 30%", self.text(h))
        h.app.last_ctx_pct = 70
        self.assertEqual(self.ctx_style(h), "class:tb.ctx.warn")
        h.app.last_ctx_pct = 90
        self.assertEqual(self.ctx_style(h), "class:tb.ctx.crit")

    async def test_hint_follows_the_open_completion_menu(self):
        h = self.harness([text_turn("x")])
        self.assertIn("Shift+Tab ganti mode", self.text(h))
        self.assertIn("Enter jalankan", self.text(h, menu="slash"))
        self.assertNotIn("Shift+Tab", self.text(h, menu="slash"))
        self.assertIn("Tab/Enter pilih", self.text(h, menu="file"))

    async def test_mode_tag_explains_the_mode_in_plain_words(self):
        h = self.harness([text_turn("x")])
        self.assertIn("tanya dulu", self.text(h))
        h.app.set_mode("plan")
        self.assertIn("hanya baca", self.text(h))
        h.app.set_mode("bypass")
        self.assertIn("TANPA TANYA", self.text(h))

    async def test_optional_parts_are_dropped_when_the_terminal_is_narrow(self):
        h = self.harness([text_turn("x")])
        h.app.total_in, h.app.total_out = 12400, 3100
        h.app.ws.todos = [{"content": "a", "status": "completed"}, {"content": "b", "status": "pending"}]
        wide = self.text(h)
        self.assertIn("12.4k↑", wide)
        self.assertIn("todo 1/2", wide)
        narrow = self.text(h, width=40)
        self.assertNotIn("↑", narrow)
        self.assertNotIn("todo", narrow)
        self.assertIn("Shift+Tab mode", narrow)

    async def test_shift_tab_shows_what_the_new_mode_means_then_it_expires(self):
        h = self.harness([text_turn("x")])
        self.assertEqual(h.app.cycle_mode(), "acceptEdits")
        self.assertIn("Mode terima-edit: edit file di folder kerja otomatis", self.text(h))
        h.app._flash = (h.app._flash[0], time.monotonic() - 1)
        self.assertNotIn("Mode terima-edit", self.text(h))
        self.assertIn("Shift+Tab ganti mode", self.text(h))

    async def test_background_tasks_and_todos_are_listed(self):
        h = self.harness([text_turn("x")])
        h.app.ws.todos = [{"content": "a", "status": "completed"}, {"content": "b", "status": "in_progress"}, {"content": "c", "status": "pending"}]
        self.assertIn("todo 1/3", self.text(h))

    async def test_toolbar_callable_reports_the_open_menu_and_width_during_a_real_prompt(self):
        h = self.harness([text_turn("x")])
        CommandRegistry(h.app)
        h.write("src/modul_utama.py", "x = 1\n")
        loop = asyncio.get_running_loop()
        calls: list[tuple] = []
        original = h.app.toolbar_fragments

        def spy(menu=None, width=None):
            calls.append((menu, width))
            return original(menu, width)

        h.app.toolbar_fragments = spy
        with create_pipe_input() as inp:
            session = create_prompt_session(h.app, "> ", input=inp, output=DummyOutput())
            inp.send_text("/")
            loop.call_later(0.8, inp.send_text, "\r")
            await session.prompt_async()
            self.assertTrue(session.bottom_toolbar() is not None, "toolbar bisa dihitung di luar aplikasi tanpa error")
        self.assertIn("slash", {menu for menu, _ in calls}, "toolbar tahu menu / sedang terbuka")
        self.assertIn(None, {menu for menu, _ in calls}, "dan kembali normal saat tidak ada menu")
        self.assertTrue(all(isinstance(width, int) for _, width in calls if width is not None))

    async def test_placeholder_guides_the_first_message_then_gets_shorter(self):
        h = self.harness([text_turn("x")])
        with create_pipe_input() as inp:
            session = create_prompt_session(h.app, "> ", input=inp, output=DummyOutput())
            first = "".join(t for _, t in session.placeholder())
            self.assertIn("jelaskan struktur proyek ini", first)
            h.app.total_in = 10
            later = "".join(t for _, t in session.placeholder())
            self.assertIn("Ketik pesan Anda", later)
            self.assertNotIn("jelaskan", later)


class BannerTests(unittest.TestCase):
    def banner(self, width: int, resumed: bool = False, notes=None) -> list[str]:
        ui, buf = make_ui(width)
        ui.banner(Settings(), Path("D:/Workspace Agung/MABA"), "default", "20261018-101530-ab12", resumed, notes=notes)
        return buf.getvalue().splitlines()

    def test_wide_banner_has_keys_examples_and_readiness_notes(self):
        text = "\n".join(self.banner(110, notes=[("info", "Belum ada MABA.md di proyek ini: ketik /init"), ("warn", "rg tidak ada")]))
        self.assertIn("Shift+Tab ganti mode", text)
        self.assertIn("Contoh: jelaskan struktur proyek ini", text)
        self.assertIn("i Belum ada MABA.md di proyek ini: ketik /init", text)
        self.assertIn("! rg tidak ada", text)
        self.assertNotIn("tidak ditampilkan", text, "catatan kredensial pindah ke /config")

    def test_narrow_banner_never_exceeds_the_terminal_width(self):
        lines = self.banner(50, notes=[("info", "Belum ada MABA.md di proyek ini: ketik /init agar agen memahami proyek Anda.")])
        self.assertLessEqual(max(len(ln) for ln in lines), 50, lines)
        text = "\n".join(lines)
        self.assertIn("MABA CODE", text)
        self.assertIn("Esc batalkan", text)
        self.assertIn("/plan tambahkan fitur login", text)

    def test_resumed_session_skips_the_examples(self):
        self.assertNotIn("Contoh:", "\n".join(self.banner(100, resumed=True)))

    def test_settings_warnings_are_shown_with_a_warning_icon(self):
        ui, buf = make_ui()
        settings = Settings()
        settings.warnings = ["Kunci 'hooks' diabaikan"]
        ui.banner(settings, Path("."), "default", "sid", False)
        self.assertIn("! Kunci 'hooks' diabaikan", buf.getvalue())


class ErrorPanelTests(unittest.TestCase):
    def test_session_errors_reassure_that_the_conversation_is_kept(self):
        ui, buf = make_ui()
        ui.error("Throttling", "Tunggu sebentar.", session=True)
        self.assertIn("Tunggu sebentar.", buf.getvalue())
        self.assertIn("Percakapan tetap tersimpan", buf.getvalue())
        ui2, buf2 = make_ui()
        ui2.error("Profile tidak ditemukan", "Cek aws configure.")
        self.assertNotIn("Percakapan", buf2.getvalue())


class TodoDisplayTests(unittest.TestCase):
    def test_header_shows_progress(self):
        ui, buf = make_ui()
        ui.show_todos([{"content": "a", "status": "completed"}, {"content": "b", "status": "pending"}])
        self.assertIn("Rencana kerja 1/2 selesai", buf.getvalue())
        self.assertIn("####----", buf.getvalue())


class HelpAndStatusTests(UxCase):
    def registry(self, h: Harness) -> CommandRegistry:
        return CommandRegistry(h.app)

    async def test_every_builtin_is_in_exactly_one_help_group(self):
        grouped = [name for _, names in HELP_GROUPS for name in names]
        self.assertEqual(sorted(grouped), sorted(cmd.name for cmd in BUILTINS), "perintah baru harus didaftarkan ke HELP_GROUPS")
        self.assertEqual(len(grouped), len(set(grouped)))

    async def test_help_is_grouped_and_lists_every_command(self):
        h = self.harness([text_turn("x")])
        await self.registry(h).dispatch("/help")
        for title in ("Mulai cepat", "Percakapan", "Model & izin", "Proyek & ekstensi", "Info & diagnosis"):
            self.assertIn(title, h.out)
        for cmd in BUILTINS:
            self.assertIn(f"/{cmd.name}", h.out)
        self.assertIn("/undo membatalkan perubahan file", h.out)

    async def test_help_lists_custom_commands_in_their_own_section(self):
        h = self.harness([text_turn("x")])
        folder = h.cwd / ".maba" / "commands"
        folder.mkdir(parents=True)
        (folder / "review.md").write_text("---\ndescription: Tinjau kode\n---\nTinjau: $ARGUMENTS", encoding="utf-8")
        await CommandRegistry(h.app).dispatch("/help")
        self.assertIn("Skill & perintah kustom", h.out)
        self.assertIn("/review", h.out)

    async def test_status_on_a_fresh_session(self):
        h = self.harness([text_turn("x")])
        await self.registry(h).dispatch("/status")
        self.assertIn("menunggu perintah Anda", h.out)
        self.assertIn("Mode standar: agen bertanya dulu", h.out)
        self.assertIn("belum terukur", h.out)
        self.assertIn("tidak ada yang bisa di-undo", h.out)
        self.assertIn("task latar", h.out)

    async def test_status_after_work_shows_context_and_changed_files(self):
        h = self.harness(
            [
                tool_turn([("w1", "write_file", {"path": "x.py", "content": "1\n"})]),
                text_turn("ok"),
            ],
            answers=["allow"],
        )
        await h.app.run_prompt("buat x.py")
        h.app.last_ctx_pct = 40
        await self.registry(h).dispatch("/status")
        self.assertIn("40%", h.out)
        self.assertIn("diringkas otomatis di 75%", h.out)
        self.assertIn("x.py", h.out.split("keadaan")[-1])
        self.assertIn("/undo membatalkan", h.out.split("keadaan")[-1])

    async def test_status_is_listed_in_the_slash_menu(self):
        h = self.harness([text_turn("x")])
        registry = self.registry(h)
        self.assertIn("status", [name for name, _ in registry.names()])


class ReadinessTests(UxCase):
    async def test_suggests_init_only_when_project_has_no_maba_md(self):
        h = self.harness([text_turn("x")])
        self.assertTrue(any("/init" in text for _, text in h.app.readiness()))
        h.write("MABA.md", "# aturan\n")
        self.assertFalse(any("/init" in text for _, text in h.app.readiness()))


class PreviewSmokeTests(unittest.TestCase):
    def test_preview_renders_every_component_without_errors(self):
        for width in (50, 100):
            for symbols in (ASCII, UNICODE):
                ui, buf = make_ui(width, symbols)
                preview.render_all(ui)
                preview.render_toolbar(ui)
                text = buf.getvalue()
                for marker in ("Banner awal", "Dialog izin: perintah shell", "Panel error", "Toolbar bawah", "Ringkasan akhir giliran"):
                    self.assertIn(marker, text)

    def test_ascii_components_stay_ascii_except_known_dashes(self):
        ui, buf = make_ui(100, ASCII)
        ui.notice("a", "green")
        ui.begin_turn()
        ui.activity.log.changes["a.py"] = [1, 2]
        ui.activity.log.steps = 1
        ui.turn_footer(10, 5, 30, 1.0)
        ui.show_todos([{"content": "a", "status": "pending"}])
        text = buf.getvalue().replace("—", "-")
        self.assertTrue(text.isascii(), [c for c in text if not c.isascii()])


if __name__ == "__main__":
    unittest.main()
