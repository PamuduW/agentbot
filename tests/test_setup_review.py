"""`agentbot setup-review` runs the chosen agent CLI read-only, then asks."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from src import setup_review
from src.paths import AgentbotPaths

REPO = Path(__file__).resolve().parents[1]
FAKE_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"

# One fake serves as every CLI. It records each call (argv, cwd, the recursion
# marker) as a JSON line, answers the sign-in check, and behaves as FAKE_MODE
# says for the real run.
FAKE_CLI = textwrap.dedent(
    f"""\
    #!{sys.executable}
    import json, os, sys, time
    log = os.environ["FAKE_LOG"]
    args = sys.argv[1:]
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({{
            "name": os.path.basename(sys.argv[0]),
            "argv": sys.argv,
            "cwd": os.getcwd(),
            "marker": os.environ.get("AGENTBOT_SETUP_REVIEW", ""),
        }}) + "\\n")
    if args[:2] in (["auth", "status"], ["login", "status"]) or args == ["status"]:
        if os.environ.get("FAKE_AUTH") == "bad":
            print("Not logged in")
            sys.exit(1)
        print(json.dumps({{"loggedIn": True}}) if args[0] == "auth" else "Logged in")
        sys.exit(0)
    if args == ["--version"]:
        print("9.9.9 (fake)")
        sys.exit(0)
    if not {{"-p", "exec"}} & set(args):
        sys.exit(0)  # an interactive update session: nothing to report
    mode = os.environ.get("FAKE_MODE", "ok")
    if mode == "limit":
        print("error: Individual quota reached. RESOURCE_EXHAUSTED (code 429)", file=sys.stderr)
        sys.exit(3)
    if mode == "auth":
        print("Error: not logged in. Please log in.", file=sys.stderr)
        sys.exit(1)
    if mode == "crash":
        print("segfault-ish", file=sys.stderr)
        sys.exit(7)
    if mode == "sleep":
        time.sleep(60)
    if mode == "empty":
        sys.exit(0)
    print("# Report\\n\\n1. **Summary**: fine.")
    if mode == "secret":
        print("Found {FAKE_TOKEN} in a file.")
        print('- "apiKey": "abc123"')
        print("auth_token = hunter2hunter2")
        print("model = gpt-6")
    """
)


def _install(home: Path, relative: str) -> Path:
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FAKE_CLI, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


LOCATIONS = {
    "claude": ".local/bin/claude",
    "codex": ".local/bin/codex",
    "cursor": ".local/bin/cursor-agent",
    "agy": ".local/bin/agy",
}


class SetupReviewTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.paths = AgentbotPaths(
            root=REPO,
            codex_home=self.home / ".codex",
            claude_home=self.home / ".claude",
            cursor_home=self.home / ".cursor",
            config_home=self.home / ".config" / "agentbot",
            agents_home=self.home / ".agents",
        )
        self.log = self.tmp / "calls.jsonl"
        # A decoy on PATH for every name: resolution must never reach it.
        decoys = self.tmp / "path-bin"
        for name in ("claude", "codex", "cursor", "cursor-agent", "agent", "agy", "npx"):
            _install(decoys, name)
        env = {
            "FAKE_LOG": str(self.log),
            "PATH": f"{decoys}:{os.environ.get('PATH', '')}",
            "GITHUB_TOKEN": FAKE_TOKEN,
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(setup_review.RECURSION_ENV, None)
        os.environ.pop("FAKE_MODE", None)
        os.environ.pop("FAKE_AUTH", None)
        sources = mock.patch.object(setup_review, "prepare_sources", return_value=[])
        self.prepare_sources = sources.start()
        self.addCleanup(sources.stop)
        self.output: list[str] = []
        self.answers: list[str] = []

    def install(self, key: str) -> Path:
        return _install(self.home, LOCATIONS[key])

    def calls(self) -> list[dict[str, object]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def review_calls(self) -> list[dict[str, object]]:
        return [
            call
            for call in self.calls()
            if call["argv"][1:] not in (["auth", "status"], ["login", "status"], ["status"], ["--version"])  # type: ignore[index]
        ]

    def run_review(self, interactive: bool = False, **options: object) -> int:
        def ask(question: str) -> str:
            self.output.append(question)
            return self.answers.pop(0) if self.answers else ""

        return setup_review.run(
            self.paths,
            setup_review.ReviewOptions(**options),  # type: ignore[arg-type]
            home=self.home,
            interactive=interactive,
            ask=ask,
            out=self.output.append,
            now=lambda: datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc),
            caller_dir=self.tmp,
        )

    def run_dir(self) -> Path:
        (folder,) = (self.paths.config_home / "setup-reviews").iterdir()
        return folder

    @property
    def text(self) -> str:
        return "\n".join(self.output)


class EveryCliTests(SetupReviewTestCase):
    def test_each_cli_runs_read_only_with_the_exact_prompt_last(self) -> None:
        read_only = {
            "claude": ["-p", "--permission-mode", "plan"],
            "codex": ["exec", "--sandbox", "read-only"],
            "cursor": ["-p", "--mode", "plan"],
            "agy": ["--mode", "plan"],
        }
        for key in setup_review.CLIS:
            with self.subTest(cli=key):
                self.log.unlink(missing_ok=True)
                for folder in (self.paths.config_home / "setup-reviews").glob("*"):
                    subprocess.run(["chmod", "-R", "u+w", str(folder)], check=True)
                    subprocess.run(["rm", "-rf", str(folder)], check=True)
                executable = self.install(key)

                self.assertEqual(0, self.run_review(cli=key), self.text)

                (call,) = self.review_calls()
                argv = call["argv"]
                self.assertEqual(str(executable), argv[0])
                for flag in read_only[key]:
                    self.assertIn(flag, argv)
                run_dir = self.run_dir()
                prompt = (run_dir / "prompt.md").read_text()
                self.assertEqual(prompt, argv[-1])
                self.assertIn(setup_review.PROMPT_VERSION, prompt)
                if key == "agy":
                    self.assertEqual("-p", argv[-2])
                else:
                    self.assertEqual("--", argv[-2])
                self.assertEqual(str(run_dir), call["cwd"])
                self.assertEqual(run_dir.name, call["marker"])
                for spec in setup_review.CLIS.values():
                    self.assertFalse(set(argv[:-1]) & spec.bypass_flags)
                self.assertNotIn(FAKE_TOKEN, json.dumps(argv))
                self.assertEqual(key, json.loads(setup_review.state_file(self.paths).read_text())["cli"])
                report = (run_dir / "report.md").read_text()
                self.assertIn("**Summary**: fine.", report)
                self.assertIn(f"run {run_dir.name}", report)
                provenance = json.loads((run_dir / "run.json").read_text())
                self.assertEqual(key, provenance["cli"])
                self.assertEqual(str(executable), provenance["executable"])
                self.assertEqual("9.9.9 (fake)", provenance["cli_version"])
                self.assertEqual(setup_review.PROMPT_VERSION, provenance["prompt_version"])
                self.assertEqual("<prompt.md>", provenance["argv"][-1])
                self.assertEqual("ok", provenance["status"])
                self.assertEqual(0o700, stat.S_IMODE(run_dir.stat().st_mode))
                self.assertEqual(0o600, stat.S_IMODE((run_dir / "report.md").stat().st_mode))

    def test_unavailable_cli_is_reported_not_substituted(self) -> None:
        self.install("codex")

        self.assertEqual(setup_review.EXIT_USAGE, self.run_review(cli="claude"))

        self.assertIn("Claude Code is not available", self.text)
        self.assertIn("codex", self.text)
        self.assertEqual([], self.calls())  # neither the decoy nor codex ran

    def test_path_is_never_used_to_find_a_cli(self) -> None:
        # Only the PATH decoys exist; `cursor` there is what WSL sees of the
        # Windows editor.
        resolved = setup_review.resolve_all(self.home)

        self.assertFalse(any(item.available for item in resolved.values()))
        self.assertEqual(setup_review.EXIT_USAGE, self.run_review(cli="cursor"))
        self.assertEqual([], self.calls())

    def test_a_link_into_windows_is_refused(self) -> None:
        link = self.home / LOCATIONS["agy"]
        link.parent.mkdir(parents=True)
        link.symlink_to(self.tmp / "target")
        (self.tmp / "target").write_text("")
        with mock.patch.object(Path, "resolve", return_value=Path("/mnt/c/Program Files/agy.exe")):
            resolved = setup_review.resolve_cli(setup_review.CLIS["agy"], self.home)

        self.assertFalse(resolved.available)
        self.assertIn("points into Windows", resolved.reason)

    def test_cursor_falls_back_to_the_agent_link_dotfiles_also_installs(self) -> None:
        agent = _install(self.home, ".local/bin/agent")

        resolved = setup_review.resolve_cli(setup_review.CLIS["cursor"], self.home)

        self.assertEqual(agent.resolve(), resolved.executable)


class ChoiceTests(SetupReviewTestCase):
    def test_last_cli_is_reused_without_asking(self) -> None:
        self.install("codex")
        self.install("claude")
        setup_review.remember_cli(self.paths, "codex")

        self.assertEqual(0, self.run_review(interactive=True, update=False))

        self.assertIn("Using Codex (last used", self.text)
        self.assertTrue(self.review_calls()[0]["argv"][0].endswith("codex"))

    def test_remembered_but_missing_cli_is_not_swapped_silently(self) -> None:
        self.install("claude")
        setup_review.remember_cli(self.paths, "codex")

        self.assertEqual(setup_review.EXIT_USAGE, self.run_review(interactive=False))

        self.assertIn("Codex was used last but is not available", self.text)
        self.assertEqual([], self.calls())

    def test_a_terminal_asks_and_offers_only_installed_clis(self) -> None:
        self.install("agy")
        self.answers = ["agy", "n"]

        self.assertEqual(0, self.run_review(interactive=True))

        self.assertTrue(any("[agy]" in line for line in self.output), self.text)
        self.assertTrue(self.review_calls()[0]["argv"][0].endswith("agy"))


class FailureTests(SetupReviewTestCase):
    def test_signed_out_cli_is_reported_before_anything_runs(self) -> None:
        self.install("codex")
        os.environ["FAKE_AUTH"] = "bad"

        self.assertEqual(setup_review.EXIT_ACCESS, self.run_review(cli="codex"))

        self.assertIn("run `codex login`", self.text)
        self.assertEqual([], self.review_calls())

    def test_usage_limit_is_surfaced(self) -> None:
        self.install("agy")
        os.environ["FAKE_MODE"] = "limit"

        self.assertEqual(setup_review.EXIT_ACCESS, self.run_review(cli="agy"))

        self.assertIn("usage or plan limit", self.text)
        self.assertIn("quota", (self.run_dir() / "cli-stderr.txt").read_text())

    def test_auth_failure_during_the_run_names_the_login(self) -> None:
        self.install("cursor")
        os.environ["FAKE_MODE"] = "auth"

        self.assertEqual(setup_review.EXIT_ACCESS, self.run_review(cli="cursor"))

        self.assertIn("cursor-agent login", self.text)

    def test_other_failures_and_empty_reports_fail(self) -> None:
        self.install("claude")
        os.environ["FAKE_MODE"] = "crash"
        self.assertEqual(setup_review.EXIT_FAILED, self.run_review(cli="claude"))
        self.assertIn("exited 7", self.text)

    def test_empty_output_is_not_a_report(self) -> None:
        self.install("claude")
        os.environ["FAKE_MODE"] = "empty"

        self.assertEqual(setup_review.EXIT_FAILED, self.run_review(cli="claude"))
        self.assertIn("printed no report", self.text)

    def test_timeout_stops_the_whole_process_group(self) -> None:
        script = self.tmp / "slow.sh"
        script.write_text(f"#!/bin/sh\nsleep 60 &\necho $! > {self.tmp}/child.pid\nwait\n")
        script.chmod(0o755)
        started = time.monotonic()

        outcome = setup_review.run_review_process(
            [str(script)], cwd=self.tmp, env=os.environ, timeout_seconds=1
        )

        self.assertEqual("timeout", outcome.status)
        self.assertLess(time.monotonic() - started, 15)
        child = int((self.tmp / "child.pid").read_text())
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_timeout_is_reported_with_its_own_exit_code(self) -> None:
        self.install("codex")
        with mock.patch.object(
            setup_review,
            "run_review_process",
            return_value=setup_review.RunOutcome("timeout", None, "partial", ""),
        ):
            self.assertEqual(setup_review.EXIT_TIMEOUT, self.run_review(cli="codex"))
        self.assertIn("did not finish within 45 min", self.text)
        self.assertIn("partial", (self.run_dir() / "report.md").read_text())

    def test_ctrl_c_cancels_and_stops_the_cli(self) -> None:
        process = mock.Mock(pid=4242)
        process.communicate.side_effect = [KeyboardInterrupt, ("out", "err")]
        with (
            mock.patch.object(setup_review.subprocess, "Popen", return_value=process),
            mock.patch.object(setup_review.os, "killpg") as killpg,
        ):
            outcome = setup_review.run_review_process(
                ["x"], cwd=self.tmp, env={}, timeout_seconds=10
            )

        self.assertEqual("cancelled", outcome.status)
        killpg.assert_called_once()
        self.assertEqual(4242, killpg.call_args.args[0])

    def test_a_review_cannot_start_another_review(self) -> None:
        self.install("claude")
        os.environ[setup_review.RECURSION_ENV] = "outer-run"

        self.assertEqual(setup_review.EXIT_USAGE, self.run_review(cli="claude"))

        self.assertIn("already inside setup-review run outer-run", self.text)
        self.assertEqual([], self.calls())


class SafetyTests(SetupReviewTestCase):
    def test_no_shell_interpolation_of_paths_in_the_prompt(self) -> None:
        self.install("claude")
        hostile = self.tmp / "ws $(touch PWNED) `touch PWNED2`"
        for name in ("agentbot", "dotfiles"):
            (hostile / name).mkdir(parents=True)

        self.assertEqual(0, self.run_review(cli="claude", workspace=hostile))

        prompt = self.review_calls()[0]["argv"][-1]
        self.assertIn("$(touch PWNED)", prompt)
        self.assertFalse(list(self.tmp.rglob("PWNED*")))

    def test_bypass_flags_are_refused(self) -> None:
        for key, spec in setup_review.CLIS.items():
            for flag in spec.bypass_flags:
                with self.subTest(cli=key, flag=flag), self.assertRaises(ValueError):
                    setup_review.assert_no_bypass(spec, ["exe", flag, "prompt"])
        # Text in the prompt is data, never a flag.
        setup_review.assert_no_bypass(
            setup_review.CLIS["claude"], ["exe", "--", "--dangerously-skip-permissions"]
        )

    def test_secrets_are_masked_and_credential_settings_refused(self) -> None:
        self.install("codex")
        os.environ["FAKE_MODE"] = "secret"

        self.assertEqual(0, self.run_review(cli="codex", scope="cli-config"))

        report = (self.run_dir() / "report.md").read_text()
        self.assertNotIn(FAKE_TOKEN, report)
        self.assertNotIn("abc123", report)
        self.assertNotIn("hunter2", report)
        self.assertIn("[redacted:SECRET_GITHUB_CLASSIC_TOKEN]", report)
        self.assertEqual(2, report.count("[refused: credential-shaped setting"))
        self.assertIn("model = gpt-6", report)
        self.assertIn("masked in the report", self.text)
        provenance = json.loads((self.run_dir() / "run.json").read_text())
        self.assertEqual(2, provenance["redactions"]["REFUSED_CREDENTIAL_SETTING"])

    def test_review_phase_never_installs_or_prunes_skills(self) -> None:
        self.install("claude")
        with (
            mock.patch("src.skills_installer.install_all", side_effect=AssertionError),
            mock.patch("src.skills_installer.install_source", side_effect=AssertionError),
            mock.patch("src.skills_installer.update_all", side_effect=AssertionError),
            mock.patch("src.skill_prune.apply_prune", side_effect=AssertionError),
        ):
            self.assertEqual(0, self.run_review(cli="claude"))
        self.assertFalse([call for call in self.calls() if call["name"] == "npx"])


class UpdatePhaseTests(SetupReviewTestCase):
    def test_no_session_starts_without_a_yes(self) -> None:
        self.install("claude")
        for answer in ("", "n", "no", "maybe"):
            with self.subTest(answer=answer):
                self.log.unlink(missing_ok=True)
                for folder in (self.paths.config_home / "setup-reviews").glob("*"):
                    subprocess.run(["rm", "-rf", str(folder)], check=True)
                self.answers = [answer]
                self.assertEqual(0, self.run_review(interactive=True, cli="claude"))
                self.assertEqual(1, len(self.review_calls()))
                self.assertIn("Nothing was changed", self.text)

    def test_without_a_terminal_the_update_phase_never_starts(self) -> None:
        self.install("claude")

        self.assertEqual(0, self.run_review(interactive=False, cli="claude"))

        self.assertEqual(1, len(self.review_calls()))
        self.assertFalse([line for line in self.output if "[y/N]" in line])

    def test_no_update_never_asks(self) -> None:
        self.install("claude")

        self.assertEqual(0, self.run_review(interactive=True, cli="claude", update=False))

        self.assertFalse([line for line in self.output if "[y/N]" in line])

    def test_yes_starts_an_interactive_session_in_normal_permission_mode(self) -> None:
        for key in setup_review.CLIS:
            with self.subTest(cli=key):
                self.log.unlink(missing_ok=True)
                self.install(key)
                self.answers = ["y"]
                workspace = self.tmp / f"ws-{key}"
                for name in ("agentbot", "dotfiles"):
                    (workspace / name).mkdir(parents=True, exist_ok=True)

                self.assertEqual(
                    0, self.run_review(interactive=True, cli=key, workspace=workspace)
                )

                _review, session = self.review_calls()
                argv = session["argv"]
                self.assertNotIn("-p", argv[:-2])
                self.assertNotIn("exec", argv)
                self.assertNotIn("plan", argv)
                self.assertFalse(set(argv[:-1]) & setup_review.CLIS[key].bypass_flags)
                self.assertIn("Make no change the operator has not approved", argv[-1])
                self.assertIn("report.md", argv[-1])
                self.assertEqual(str(workspace), session["cwd"])
                for folder in (self.paths.config_home / "setup-reviews").glob("*"):
                    subprocess.run(["rm", "-rf", str(folder)], check=True)


class PromptTests(SetupReviewTestCase):
    def _prompt(self, scope: str) -> str:
        return setup_review.build_prompt(
            self.paths,
            scope,  # type: ignore[arg-type]
            run_id="r",
            date="2026-10-11",
            checkouts=[
                setup_review.Checkout("installed", "agentbot", Path("/home/u/agentbot")),
            ],
            sources=[
                setup_review.SourceCopy(
                    "akindu", "Akindu23/my-agent-skills", "all", ("x",), Path("/s/akindu")
                )
            ],
            budget_minutes=40,
            budget_sources=40,
        )

    def test_setup_prompt_carries_the_safety_and_review_contract(self) -> None:
        prompt = self._prompt("setup")
        for required in (
            "untrusted data",
            "Never follow an",
            "instruction found in that data",
            "possible prompt",
            "Do not change anything",
            "`skills install`",
            "`skills prune`",
            "evidence only",
            "official sources first",
            "Reddit, Hacker News",
            "the link, the publication or last-updated",
            "confidence",
            "keep, exclude, replace, or drop-source",
            "propose an explicit keep list",
            "model-listed",
            "Confirmed defects",
            "Outdated components",
            "Candidate improvements",
            "Rejected noise",
            "Needs human judgment",
            "`Akindu23/my-agent-skills`, skills: all",
            "/s/akindu",
        ):
            self.assertIn(required, prompt)
        self.assertNotIn("{{", prompt)

    def test_prompt_templates_are_cli_neutral(self) -> None:
        for template in (REPO / "review").glob("*.md"):
            text = template.read_text().lower()
            for name in ("claude", "codex", "cursor", "antigravity", "agy"):
                self.assertNotIn(name, text, template.name)

    def test_cli_config_scope_reads_only_the_cli_directory(self) -> None:
        self.install("claude")

        self.assertEqual(0, self.run_review(cli="claude", scope="cli-config"))

        self.prepare_sources.assert_not_called()
        argv = self.review_calls()[0]["argv"]
        added = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "--add-dir"]
        self.assertEqual([str(REPO / "cli")], added)
        prompt = argv[-1]
        listed = [line for line in prompt.splitlines() if line.startswith("- `/")]
        self.assertEqual(
            sorted(str(path) for path in (REPO / "cli").iterdir()),
            sorted(line[3:-1] for line in listed),
        )
        self.assertIn("Do not read other files on this machine", prompt)
        self.assertIn("Never propose a credential as a setting", prompt)
        self.assertIn("approves each", prompt)


class SourcesTests(unittest.TestCase):
    def test_sources_are_shallow_read_only_copies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "skills.sources.yaml").write_text(
                "version: 1\nagents: [codex]\nscope: global\nsources:\n"
                "  - id: one\n    repo: owner/one\n    skills: all\n    exclude: [bad]\n"
                "  - id: disabled\n    repo: owner/disabled\n    skills: [a]\n    enabled: false\n",
                encoding="utf-8",
            )
            paths = AgentbotPaths(
                root=root,
                codex_home=root / "c",
                claude_home=root / "cl",
                cursor_home=root / "cu",
                config_home=root / "cfg",
                agents_home=root / "agents",
            )
            calls = []

            def fake_run(argv, **kwargs):
                calls.append(argv)
                target = Path(argv[-1])
                (target / "skill").mkdir(parents=True)
                (target / "skill" / "SKILL.md").write_text("x")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with mock.patch.object(setup_review.subprocess, "run", side_effect=fake_run):
                copies = setup_review.prepare_sources(paths, root / "sources")

            self.assertEqual(1, len(calls))
            self.assertIn("--depth=1", calls[0])
            (copy,) = copies
            self.assertEqual(("one", "all", ("bad",)), (copy.id, copy.selection, copy.exclude))
            skill = root / "sources" / "one" / "skill" / "SKILL.md"
            self.assertFalse(skill.stat().st_mode & 0o222)
            subprocess.run(["chmod", "-R", "u+w", str(root / "sources")], check=True)


if __name__ == "__main__":
    unittest.main()
