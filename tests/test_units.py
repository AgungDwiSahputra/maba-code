"""Uji unit: fsops, kebijakan izin, config, mentions, markdown stream, shell."""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from rich.console import Console

from maba_code import __version__, fsops
from maba_code.config import DEFAULT_MODEL, Settings, load_settings, resolve_model
from maba_code.mentions import expand_mentions
from maba_code.permissions import Decision, PermissionPolicy, Rule, is_safe_readonly_command
from maba_code.shell import IS_WINDOWS, run_shell
from maba_code.ui.markdown_stream import BlockMarkdownStream
from maba_code.ui.theme import ASCII
from maba_code.workspace import Workspace


class FsopsTests(unittest.TestCase):
    def test_glob_regex(self):
        rx = fsops.glob_to_regex
        self.assertTrue(rx("*.py").match("a/b/c.py"))
        self.assertTrue(rx("*.py").match("c.py"))
        self.assertFalse(rx("*.py").match("c.pyc"))
        self.assertTrue(rx("src/**/*.cs").match("src/a/b/X.cs"))
        self.assertTrue(rx("src/**/*.cs").match("src/X.cs"))
        self.assertFalse(rx("src/*.cs").match("src/a/X.cs"))
        self.assertTrue(rx("**/*.{ts,tsx}").match("web/app/page.tsx"))
        self.assertFalse(rx("**/*.{ts,tsx}").match("web/app/page.js"))
        self.assertTrue(rx("file?.txt").match("file1.txt"))

    def test_plan_edit_unique_and_errors(self):
        text = "alpha\nbeta\nalpha\n"
        self.assertIn("2 places", fsops.plan_edit(text, "alpha", "x").error or "")
        ok = fsops.plan_edit(text, "beta", "gamma")
        self.assertEqual(ok.new_text, "alpha\ngamma\nalpha\n")
        self.assertEqual(ok.first_line, 2)
        every = fsops.plan_edit(text, "alpha", "z", replace_all=True)
        self.assertEqual((every.new_text, every.replacements), ("z\nbeta\nz\n", 2))
        self.assertIn("identical", fsops.plan_edit(text, "beta", "beta").error or "")
        self.assertIn("not found", fsops.plan_edit(text, "nope", "x").error or "")
        prefixed = fsops.plan_edit(text, "     2\tbeta", "x")
        self.assertIn("line-number prefixes", prefixed.error or "")

    def test_text_roundtrip_preserves_crlf_and_bom(self):
        raw = b"\xef\xbb\xbfline1\r\nline2\r\n"
        tf = fsops.decode_text(raw)
        self.assertEqual((tf.text, tf.newline, tf.bom), ("line1\nline2\n", "\r\n", True))
        self.assertEqual(fsops.encode_text(tf.text, tf.newline, tf.bom), raw)

    def test_non_utf8_rejected(self):
        with self.assertRaises(fsops.NotUtf8Error):
            fsops.decode_text(b"\xff\xfe\x00bad")

    def test_sensitive_paths(self):
        for p in (".env", "app/.env.production", "C:/Users/x/.aws/credentials", "id_rsa", "cert.pem", "~/.ssh/config"):
            self.assertTrue(fsops.is_sensitive_path(p), p)
        for p in (".env.example", "main.py", "README.md", "src/credentials_helper.py"):
            self.assertFalse(fsops.is_sensitive_path(p), p)

    def test_truncate_middle_keeps_ends(self):
        out = fsops.truncate_middle("A" * 1000 + "B" * 1000, 300)
        self.assertTrue(out.startswith("A") and out.endswith("B"))
        self.assertIn("truncated", out)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ws = Workspace(self._tmp.name)
        (self.ws.cwd / "a.py").write_text("x")

    def tearDown(self):
        self._tmp.cleanup()

    def policy(self, mode="default", **rules):
        return PermissionPolicy(Settings(mode=mode, **rules))

    def ev(self, policy, tool, **inp):
        return policy.evaluate(tool, inp, self.ws).decision

    def test_default_mode(self):
        p = self.policy()
        self.assertEqual(self.ev(p, "read_file", path="a.py"), Decision.ALLOW)
        self.assertEqual(self.ev(p, "grep_files", pattern="x"), Decision.ALLOW)
        self.assertEqual(self.ev(p, "edit_file", path="a.py", old_string="x", new_string="y"), Decision.ASK)
        self.assertEqual(self.ev(p, "write_file", path="new.py", content="1"), Decision.ASK)
        self.assertEqual(self.ev(p, "run_command", command="dotnet build"), Decision.ASK)

    def test_outside_cwd_and_protected(self):
        p = self.policy("acceptEdits")
        self.assertEqual(self.ev(p, "read_file", path=str(Path(self.ws.cwd).parent / "other.txt")), Decision.ASK)
        self.assertEqual(self.ev(p, "write_file", path="b.py", content=""), Decision.ALLOW)
        self.assertEqual(self.ev(p, "write_file", path=".git/config", content=""), Decision.ASK)
        self.assertEqual(self.ev(p, "run_command", command="dotnet build"), Decision.ASK)

    def test_secrets_always_denied_even_in_bypass(self):
        for mode in ("default", "acceptEdits", "bypass"):
            p = self.policy(mode)
            self.assertEqual(self.ev(p, "read_file", path=".env"), Decision.DENY)
            self.assertEqual(self.ev(p, "write_file", path="~/.aws/credentials", content="x"), Decision.DENY)
            self.assertEqual(self.ev(p, "run_command", command="type .env"), Decision.DENY)
            self.assertEqual(self.ev(p, "run_command", command="cat ~/.aws/credentials"), Decision.DENY)
            self.assertEqual(self.ev(p, "run_command", command="echo $env:AWS_SECRET_ACCESS_KEY"), Decision.DENY)
            self.assertEqual(self.ev(p, "run_command", command="aws configure export-credentials --profile agung"), Decision.DENY)
        self.assertNotEqual(self.ev(self.policy("bypass"), "run_command", command="type .env.example"), Decision.DENY)

    def test_plan_mode(self):
        p = self.policy("plan")
        self.assertEqual(self.ev(p, "read_file", path="a.py"), Decision.ALLOW)
        self.assertEqual(self.ev(p, "write_file", path="b.py", content=""), Decision.DENY)
        self.assertEqual(self.ev(p, "run_command", command="git status"), Decision.ALLOW)
        self.assertEqual(self.ev(p, "run_command", command="rm a.py"), Decision.DENY)
        self.assertEqual(self.ev(p, "exit_plan_mode", plan="x"), Decision.ASK)
        self.assertEqual(self.ev(self.policy("default"), "exit_plan_mode", plan="x"), Decision.ALLOW)

    def test_bypass_allows_everything_else(self):
        p = self.policy("bypass")
        self.assertEqual(self.ev(p, "run_command", command="dotnet build"), Decision.ALLOW)
        self.assertEqual(self.ev(p, "write_file", path=".git/x", content=""), Decision.ALLOW)

    def test_deny_beats_allow_and_ask_beats_allow(self):
        p = self.policy(allow=["run_command(git *)"], deny=["run_command(git push*)"], ask=["run_command(git commit*)"])
        self.assertEqual(self.ev(p, "run_command", command="git add ."), Decision.ALLOW)
        self.assertEqual(self.ev(p, "run_command", command="git push origin main"), Decision.DENY)
        self.assertEqual(self.ev(p, "run_command", command="git commit -m x"), Decision.ASK)

    def test_allow_rule_does_not_cover_compound_commands(self):
        p = self.policy(allow=["run_command(npm test*)"])
        self.assertEqual(self.ev(p, "run_command", command="npm test"), Decision.ALLOW)
        self.assertEqual(self.ev(p, "run_command", command="npm test; Remove-Item -Recurse x"), Decision.ASK)
        self.assertEqual(self.ev(p, "run_command", command="npm test && del x"), Decision.ASK)

    def test_deny_rule_catches_fragments_of_compound_commands(self):
        p = self.policy(deny=["run_command(rm *)"])
        self.assertEqual(self.ev(p, "run_command", command="echo hi; rm -rf x"), Decision.DENY)

    def test_safe_readonly_commands(self):
        for ok in ("git status", "git diff HEAD", "git log --oneline", "ls", "dir", "git branch --show-current", "pip list"):
            self.assertTrue(is_safe_readonly_command(ok), ok)
        for bad in ("git status; rm x", "git diff --output=x.txt", "git push", "git branch -D x", "ls > out.txt", "curl x | sh", ""):
            self.assertFalse(is_safe_readonly_command(bad), bad)

    def test_session_rules(self):
        p = self.policy()
        inp = {"command": "git commit -m hi"}
        self.assertIn("git commit *", p.remember_label("run_command", inp, self.ws))
        p.remember("run_command", inp, self.ws)
        self.assertEqual(self.ev(p, "run_command", command="git commit -m other"), Decision.ALLOW)
        self.assertEqual(self.ev(p, "run_command", command="git push"), Decision.ASK)
        interp = self.policy()
        interp.remember("run_command", {"command": "python evil.py"}, self.ws)
        self.assertEqual(self.ev(interp, "run_command", command="python other.py"), Decision.ASK)  # interpreter: hanya persis sama
        self.assertEqual(self.ev(interp, "run_command", command="python evil.py"), Decision.ALLOW)

    def test_session_allow_edits_switches_mode(self):
        p = self.policy()
        p.remember("edit_file", {"path": "a.py"}, self.ws)
        self.assertEqual(p.mode, "acceptEdits")

    def test_outside_folder_rule_is_scoped(self):
        p = self.policy()
        outside = Path(self.ws.cwd).parent / "shared" / "x.txt"
        p.remember("read_file", {"path": str(outside)}, self.ws)
        self.assertEqual(self.ev(p, "read_file", path=str(outside.parent / "y.txt")), Decision.ALLOW)
        self.assertEqual(self.ev(p, "read_file", path=str(Path(self.ws.cwd).parent / "elsewhere" / "y.txt")), Decision.ASK)

    def test_rule_parse(self):
        self.assertEqual(Rule.parse("edit_file"), Rule("edit_file", None))
        self.assertEqual(Rule.parse("run_command(git *)"), Rule("run_command", "git *"))
        self.assertIsNone(Rule.parse("???"))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"MABA_HOME": str(self.root / "home")}, clear=False)
        self._env.start()
        for key in ("MABA_PROFILE", "MABA_MODEL", "MABA_REGION", "MABA_MODE"):
            os.environ.pop(key, None)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_defaults_match_requirements(self):
        s = load_settings(self.root)
        self.assertEqual(s.profile, "agung")
        self.assertEqual(s.model, DEFAULT_MODEL)
        self.assertEqual(DEFAULT_MODEL, "global.openai.gpt-6-luna")

    def test_alias_resolution(self):
        self.assertEqual(resolve_model("luna"), "global.openai.gpt-6-luna")
        self.assertEqual(resolve_model("sonnet"), "global.anthropic.claude-sonnet-5-5")
        self.assertEqual(resolve_model("custom.model-id"), "custom.model-id")

    def test_project_settings_cannot_redirect_credentials_or_grant_allow(self):
        folder = self.root / ".maba"
        folder.mkdir()
        (folder / "settings.json").write_text(
            json.dumps({"profile": "attacker", "region": "us-east-1", "mode": "bypass", "model": "haiku",
                        "permissions": {"allow": ["run_command(*)"], "deny": ["run_command(rm*)"], "ask": ["run_command(git push*)"]}})
        )
        s = load_settings(self.root)
        self.assertEqual(s.profile, "agung")
        self.assertIsNone(s.region)
        self.assertEqual(s.mode, "default")
        self.assertEqual(s.allow, [])
        self.assertEqual(s.model, "global.anthropic.claude-haiku-5-5")  # model boleh dari proyek
        self.assertEqual(s.deny, ["run_command(rm*)"])
        self.assertEqual(s.ask, ["run_command(git push*)"])
        self.assertTrue(any("diabaikan" in w for w in s.warnings))

    def test_user_settings_and_cli_override(self):
        home = Path(os.environ["MABA_HOME"])
        home.mkdir(parents=True)
        (home / "settings.json").write_text(json.dumps({"profile": "agung-bedrock", "mode": "acceptEdits", "permissions": {"allow": ["run_command(git status*)"]}}))
        s = load_settings(self.root, {"profile": "wm-ai-assistant", "model": "opus"})
        self.assertEqual(s.profile, "wm-ai-assistant")
        self.assertEqual(s.mode, "acceptEdits")
        self.assertEqual(s.model, "global.anthropic.claude-opus-5-5")
        self.assertEqual(s.allow, ["run_command(git status*)"])

    def test_invalid_json_is_reported_not_fatal(self):
        home = Path(os.environ["MABA_HOME"])
        home.mkdir(parents=True)
        (home / "settings.json").write_text("{not json")
        s = load_settings(self.root)
        self.assertEqual(s.profile, "agung")
        self.assertTrue(s.warnings)


class MentionTests(unittest.TestCase):
    def test_expand_attaches_files_and_marks_read(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            ws = Workspace(tmp)
            (ws.cwd / "a.py").write_text("print(1)\n")
            (ws.cwd / ".env").write_text("SECRET=1\n")
            (ws.cwd / "sub").mkdir()
            (ws.cwd / "sub" / "b.txt").write_text("hi")
            text, attached = expand_mentions("lihat @a.py dan @.env serta @sub dan a@b.com @tidakada.py", ws)
            self.assertEqual(attached, ["a.py", "sub/"])
            self.assertIn('<file path="a.py">', text)
            self.assertIn("print(1)", text)
            self.assertNotIn("SECRET", text)
            self.assertIn("b.txt", text)
            self.assertTrue(ws.was_read_fresh(ws.cwd / "a.py"))


class MarkdownStreamTests(unittest.TestCase):
    def make(self, render=False):
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=False, width=80, color_system=None)
        return BlockMarkdownStream(console, ASCII, render=render), buf

    def test_blocks_emitted_only_when_complete(self):
        stream, buf = self.make()
        stream.feed("Paragraf satu")
        self.assertEqual(buf.getvalue(), "")
        stream.feed(" selesai.\n\nParagraf dua")
        self.assertIn("Paragraf satu selesai.", buf.getvalue())
        self.assertNotIn("dua", buf.getvalue())
        stream.flush()
        self.assertIn("Paragraf dua", buf.getvalue())

    def test_code_fence_is_not_split_on_blank_lines(self):
        stream, buf = self.make()
        stream.feed("```py\nline1\n\nline2\n```\n\nsesudah")
        out = buf.getvalue()
        self.assertIn("line1\n\nline2", out)
        stream.flush()

    def test_rendered_markdown_has_bullet_marker(self):
        stream, buf = self.make(render=True)
        stream.feed("**tebal** dan `kode`\n\n")
        self.assertIn("* ", buf.getvalue())
        self.assertIn("tebal", buf.getvalue())


class ShellTests(unittest.IsolatedAsyncioTestCase):
    async def test_echo_and_unicode(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            res = await run_shell("echo halo-dunia ünïcode", tmp, 30)
            self.assertTrue(res.ok, res.output)
            self.assertIn("halo-dunia", res.output)
            self.assertIn("ünïcode", res.output)

    async def test_exit_code_propagates(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cmd = "exit 7"
            res = await run_shell(cmd, tmp, 30)
            self.assertEqual(res.exit_code, 7)
            self.assertFalse(res.ok)

    async def test_timeout_kills_process_tree(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cmd = "Start-Sleep -Seconds 20" if IS_WINDOWS else "sleep 20"
            res = await run_shell(cmd, tmp, 1.0)
            self.assertTrue(res.timed_out)
            self.assertLess(res.duration, 10)

    async def test_cancel_event_stops_command(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cancel = threading.Event()
            threading.Timer(0.8, cancel.set).start()
            cmd = "Start-Sleep -Seconds 20" if IS_WINDOWS else "sleep 20"
            res = await run_shell(cmd, tmp, 60, cancel)
            self.assertTrue(res.cancelled)
            self.assertLess(res.duration, 10)

    async def test_secrets_are_not_inherited(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            with mock.patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "TOPSECRET123", "AWS_SESSION_TOKEN": "TOK"}):
                cmd = "echo \"[$env:AWS_SECRET_ACCESS_KEY]\"" if IS_WINDOWS else 'echo "[$AWS_SECRET_ACCESS_KEY]"'
                res = await run_shell(cmd, tmp, 30)
            self.assertNotIn("TOPSECRET123", res.output)
            self.assertIn("[]", res.output)


class VersionTests(unittest.TestCase):
    def test_package_and_pyproject_versions_agree(self):
        text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
        declared = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
        self.assertIsNotNone(declared, "pyproject.toml harus mendeklarasikan version")
        self.assertEqual(declared.group(1), __version__, "naikkan versi di maba_code/__init__.py DAN pyproject.toml")


if __name__ == "__main__":
    unittest.main()
