"""`agentbot setup-review`: sync the checkouts, review read-only, file the report."""

from __future__ import annotations

import json
import os
import re
import shutil
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
    if mode == "personal":
        print(os.environ["FAKE_PERSONAL"])
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



GIT = shutil.which("git", path="/usr/bin:/bin") or "/usr/bin/git"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [GIT, *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


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
        # Real Git first on PATH (not an operator's wrapper), then a decoy of
        # every agent CLI name: resolution must never reach the decoys.
        real = self.tmp / "real-bin"
        real.mkdir()
        (real / "git").symlink_to(GIT)
        decoys = self.tmp / "path-bin"
        for name in ("claude", "codex", "cursor", "cursor-agent", "agent", "agy", "npx"):
            _install(decoys, name)
        env = {
            "FAKE_LOG": str(self.log),
            "PATH": f"{real}:{decoys}:/usr/bin:/bin",
            "GITHUB_TOKEN": FAKE_TOKEN,
            "GIT_AUTHOR_NAME": "Op",
            "GIT_AUTHOR_EMAIL": "op@example.invalid",
            "GIT_COMMITTER_NAME": "Op",
            "GIT_COMMITTER_EMAIL": "op@example.invalid",
            "GIT_CONFIG_GLOBAL": str(self.tmp / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in (setup_review.RECURSION_ENV, "FAKE_MODE", "FAKE_AUTH", "DOTFILES_DIR"):
            os.environ.pop(key, None)
        self.remotes = self.tmp / "remotes"
        for name in ("dotfiles", "agentbot", "dotfiles-shared"):
            self._make_live_clone(name)
        sources = mock.patch.object(setup_review, "prepare_sources", return_value=[])
        self.prepare_sources = sources.start()
        self.addCleanup(sources.stop)
        vault = mock.patch.object(setup_review, "vault_status", return_value="State synced")
        vault.start()
        self.addCleanup(vault.stop)
        facts = mock.patch.object(setup_review, "collect_facts", return_value="FACTS-TABLE")
        facts.start()
        self.addCleanup(facts.stop)
        self.identity = setup_review.Identity(home=self.home, user="opuser", host="ophost")
        self.output: list[str] = []
        self.answers: list[str] = []

    def _make_live_clone(self, name: str) -> None:
        bare = self.remotes / f"{name}.git"
        bare.mkdir(parents=True)
        git(bare, "init", "--quiet", "--bare", "-b", "main")
        seed = self.tmp / "seed" / name
        seed.mkdir(parents=True)
        git(seed, "init", "--quiet", "-b", "main")
        (seed / "README.md").write_text(f"{name}\n")
        git(seed, "add", "README.md")
        git(seed, "commit", "--quiet", "-m", "init")
        git(seed, "remote", "add", "origin", str(bare))
        git(seed, "push", "--quiet", "origin", "main", "main:development")
        git(self.home, "clone", "--quiet", "-b", "main", str(bare), name)

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
        options.setdefault("confirmed", True)

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
            now=lambda: datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc),
            identity=self.identity,
        )

    def run_dir(self) -> Path:
        (folder,) = (self.paths.config_home / "setup-reviews").iterdir()
        return folder

    def filed(self) -> dict[str, str]:
        """The reviews/ folder as it is on the remote development branch."""
        bare = self.remotes / "dotfiles.git"
        names = git(bare, "ls-tree", "--name-only", "development", "reviews/").splitlines()
        return {
            Path(name).name: git(bare, "show", f"development:{name}") for name in names
        }

    @property
    def text(self) -> str:
        return "\n".join(self.output)


class EveryCliTests(SetupReviewTestCase):
    def test_each_cli_runs_read_only_from_dotfiles_and_files_the_report(self) -> None:
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
                    shutil.rmtree(folder)
                executable = self.install(key)

                self.assertEqual(0, self.run_review(cli=key), self.text)

                (call,) = self.review_calls()
                argv = call["argv"]
                self.assertEqual(str(executable), argv[0])
                for flag in read_only[key]:
                    self.assertIn(flag, argv)
                for spec in setup_review.CLIS.values():
                    self.assertFalse(set(argv[:-1]) & spec.bypass_flags)
                self.assertNotIn(FAKE_TOKEN, json.dumps(argv))
                self.assertEqual(str(self.home / "dotfiles"), call["cwd"])
                run_dir = self.run_dir()
                self.assertEqual((run_dir / "prompt.md").read_text(), argv[-1])
                self.assertEqual(run_dir.name, call["marker"])
                provenance = json.loads((run_dir / "run.json").read_text())
                self.assertEqual(key, provenance["cli"])
                self.assertEqual(setup_review.PROMPT_VERSION, provenance["prompt_version"])
                self.assertEqual("<prompt.md>", provenance["argv"][-1])
                self.assertEqual(0o700, stat.S_IMODE(run_dir.stat().st_mode))

        filed = self.filed()
        reviews = sorted(name for name in filed if name.startswith("review_2"))
        self.assertTrue(reviews and all(name.endswith("_full.md") for name in reviews))
        self.assertIn("**Summary**: fine.", filed[reviews[-1]])
        log = filed["review_log.md"]
        for name in reviews:
            self.assertIn(f"- {name} - created", log)

    def test_codex_search_comes_before_exec(self) -> None:
        # `codex exec --search` exits 2 ("unexpected argument"); found in the
        # first live Codex run after the merge.
        self.install("codex")

        self.assertEqual(0, self.run_review(cli="codex"))

        argv = self.review_calls()[0]["argv"]
        self.assertEqual(["--search", "exec"], argv[1:3])

    def test_unavailable_cli_is_reported_not_substituted(self) -> None:
        self.install("codex")

        self.assertEqual(setup_review.EXIT_USAGE, self.run_review(cli="claude"))

        self.assertIn("Claude Code is not available", self.text)
        self.assertEqual([], self.calls())

    def test_path_is_never_used_to_find_a_cli(self) -> None:
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
    def test_the_chosen_cli_is_confirmed_before_anything_runs(self) -> None:
        self.install("codex")
        for answer in ("", "n", "maybe"):
            with self.subTest(answer=answer):
                self.answers = [answer]
                self.assertEqual(
                    0, self.run_review(interactive=True, cli="codex", confirmed=False)
                )
                self.assertIn("Run a full review with Codex? [y/N]", self.text)
                self.assertEqual([], self.review_calls())

        self.answers = ["y"]
        self.assertEqual(0, self.run_review(interactive=True, cli="codex", confirmed=False))
        self.assertEqual(1, len(self.review_calls()))

    def test_without_a_terminal_it_needs_yes(self) -> None:
        self.install("codex")

        self.assertEqual(
            setup_review.EXIT_USAGE, self.run_review(cli="codex", confirmed=False)
        )

        self.assertIn("pass --yes", self.text)
        self.assertEqual([], self.calls())

    def test_last_cli_is_reused(self) -> None:
        self.install("codex")
        self.install("claude")
        setup_review.remember_cli(self.paths, "codex")

        self.assertEqual(0, self.run_review(interactive=True))

        self.assertIn("Using Codex (last used", self.text)
        self.assertTrue(self.review_calls()[0]["argv"][0].endswith("codex"))

    def test_remembered_but_missing_cli_is_not_swapped_silently(self) -> None:
        self.install("claude")
        setup_review.remember_cli(self.paths, "codex")

        self.assertEqual(setup_review.EXIT_USAGE, self.run_review(interactive=False))

        self.assertIn("Codex was used last but is not available", self.text)
        self.assertEqual([], self.calls())


class CheckoutSyncTests(SetupReviewTestCase):
    def _push_upstream(self, name: str) -> None:
        other = self.tmp / "other" / name
        git(self.tmp, "clone", "--quiet", str(self.remotes / f"{name}.git"), str(other))
        (other / "NEW.md").write_text("new\n")
        git(other, "add", "NEW.md")
        git(other, "commit", "--quiet", "-m", "upstream change")
        git(other, "push", "--quiet", "origin", "main")

    def test_a_clone_that_is_behind_is_fast_forwarded_first(self) -> None:
        self.install("claude")
        self._push_upstream("agentbot")

        self.assertEqual(0, self.run_review(cli="claude"), self.text)

        self.assertTrue((self.home / "agentbot" / "NEW.md").is_file())
        self.assertIn("agentbot         pulled: pulled 1 commit(s)", self.text)

    def test_a_dirty_clone_stops_the_review_and_is_left_alone(self) -> None:
        self.install("claude")
        (self.home / "dotfiles-shared" / "WIP.md").write_text("mine\n")

        self.assertEqual(setup_review.EXIT_PREFLIGHT, self.run_review(cli="claude"))

        self.assertIn("dotfiles-shared  dirty", self.text)
        self.assertEqual([], self.review_calls())
        self.assertTrue((self.home / "dotfiles-shared" / "WIP.md").is_file())

    def test_a_clone_with_unpushed_commits_stops_the_review(self) -> None:
        self.install("claude")
        clone = self.home / "dotfiles"
        (clone / "LOCAL.md").write_text("local\n")
        git(clone, "add", "LOCAL.md")
        git(clone, "commit", "--quiet", "-m", "local")

        self.assertEqual(setup_review.EXIT_PREFLIGHT, self.run_review(cli="claude"))

        self.assertIn("dotfiles         ahead", self.text)
        self.assertEqual([], self.review_calls())


class FilingTests(SetupReviewTestCase):
    def test_the_live_clone_never_changes_branch_or_gets_a_file(self) -> None:
        self.install("claude")
        clone = self.home / "dotfiles"

        self.assertEqual(0, self.run_review(cli="claude"))

        self.assertEqual("main", git(clone, "rev-parse", "--abbrev-ref", "HEAD"))
        self.assertEqual("", git(clone, "status", "--porcelain"))
        self.assertFalse((clone / "reviews").exists())
        self.assertEqual(1, len(git(clone, "worktree", "list").splitlines()))
        self.assertNotIn("reviews", git(self.remotes / "dotfiles.git", "ls-tree", "--name-only", "main"))

    def test_the_log_gains_one_line_per_review(self) -> None:
        self.install("claude")

        self.assertEqual(0, self.run_review(cli="claude"))
        with mock.patch.object(
            setup_review, "review_file_name", return_value="review_20261012_120000_dotfiles.md"
        ):
            self.assertEqual(0, self.run_review(cli="claude", scope="dotfiles"))

        log = self.filed()["review_log.md"]
        self.assertTrue(log.startswith("# Setup reviews"))
        lines = [line for line in log.splitlines() if line.startswith("- review_")]
        self.assertEqual(2, len(lines))
        self.assertEqual("- review_20261012_120000_dotfiles.md - created", lines[-1])

    def test_a_refused_push_keeps_the_local_copy_and_leaves_the_clone_clean(self) -> None:
        # Another person using these dotfiles cannot push to the owner's repo.
        self.install("claude")
        hook = self.remotes / "dotfiles.git" / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho denied >&2\nexit 1\n")
        hook.chmod(0o755)

        self.assertEqual(0, self.run_review(cli="claude"))

        self.assertIn("not filed in Dotfiles (push-failed", self.text)
        self.assertIn("**Summary**: fine.", (self.run_dir() / "report.md").read_text())
        clone = self.home / "dotfiles"
        self.assertEqual("", git(clone, "status", "--porcelain"))
        self.assertEqual(1, len(git(clone, "worktree", "list").splitlines()))

    def test_personal_details_and_secrets_are_replaced_before_filing(self) -> None:
        self.install("codex")
        os.environ["FAKE_MODE"] = "personal"
        os.environ["FAKE_PERSONAL"] = (
            f"See {self.home}/dotfiles/install.sh and /home/opuser/x on ophost; "
            f"mail opuser@example.com; token {FAKE_TOKEN}; clone git@github.com:o/r; "
            "user opuserx stays"
        )

        self.assertEqual(0, self.run_review(cli="codex"))

        (name,) = [n for n in self.filed() if n.startswith("review_2")]
        filed = self.filed()[name]
        for leaked in (str(self.home), "opuser@", "ophost", FAKE_TOKEN, " opuser "):
            self.assertNotIn(leaked, filed)
        self.assertIn("/home/{user}/dotfiles/install.sh", filed)
        self.assertIn("/home/{user}/x on {host}", filed)
        self.assertIn("{email}", filed)
        self.assertIn("git@github.com:o/r", filed)
        self.assertIn("opuserx", filed)
        self.assertEqual(filed, (self.run_dir() / "report.md").read_text().rstrip("\n"))


class FailureTests(SetupReviewTestCase):
    def test_signed_out_cli_is_reported_before_anything_runs(self) -> None:
        self.install("codex")
        os.environ["FAKE_AUTH"] = "bad"

        self.assertEqual(setup_review.EXIT_ACCESS, self.run_review(cli="codex"))

        self.assertIn("run `codex login`", self.text)
        self.assertEqual([], self.review_calls())

    def test_usage_limit_is_surfaced_and_nothing_is_filed(self) -> None:
        self.install("agy")
        os.environ["FAKE_MODE"] = "limit"

        self.assertEqual(setup_review.EXIT_ACCESS, self.run_review(cli="agy"))

        self.assertIn("usage or plan limit", self.text)
        self.assertEqual({}, self.filed())

    def test_auth_failure_during_the_run_names_the_login(self) -> None:
        self.install("cursor")
        os.environ["FAKE_MODE"] = "auth"

        self.assertEqual(setup_review.EXIT_ACCESS, self.run_review(cli="cursor"))

        self.assertIn("cursor-agent login", self.text)

    def test_other_failures_fail(self) -> None:
        self.install("claude")
        os.environ["FAKE_MODE"] = "crash"

        self.assertEqual(setup_review.EXIT_FAILED, self.run_review(cli="claude"))
        self.assertIn("exited 7", self.text)

    def test_empty_output_is_not_a_report(self) -> None:
        self.install("claude")
        os.environ["FAKE_MODE"] = "empty"

        self.assertEqual(setup_review.EXIT_FAILED, self.run_review(cli="claude"))
        self.assertIn("printed no report", self.text)
        self.assertEqual({}, self.filed())

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
        self.assertEqual(4242, killpg.call_args.args[0])

    def test_a_review_cannot_start_another_review(self) -> None:
        self.install("claude")
        os.environ[setup_review.RECURSION_ENV] = "outer-run"

        self.assertEqual(setup_review.EXIT_USAGE, self.run_review(cli="claude"))

        self.assertIn("already inside setup-review run outer-run", self.text)
        self.assertEqual([], self.calls())


class SafetyTests(SetupReviewTestCase):
    def test_bypass_flags_are_refused(self) -> None:
        for key, spec in setup_review.CLIS.items():
            for flag in spec.bypass_flags:
                with self.subTest(cli=key, flag=flag), self.assertRaises(ValueError):
                    setup_review.assert_no_bypass(spec, ["exe", flag, "prompt"])
        setup_review.assert_no_bypass(
            setup_review.CLIS["claude"], ["exe", "--", "--dangerously-skip-permissions"]
        )

    def test_credential_settings_are_refused_in_the_report(self) -> None:
        self.install("codex")
        os.environ["FAKE_MODE"] = "secret"

        self.assertEqual(0, self.run_review(cli="codex", scope="cli-config"))

        report = (self.run_dir() / "report.md").read_text()
        self.assertNotIn(FAKE_TOKEN, report)
        self.assertNotIn("abc123", report)
        self.assertNotIn("hunter2", report)
        self.assertEqual(2, report.count("[refused: credential-shaped setting"))
        self.assertIn("model = gpt-6", report)

    def test_the_review_never_installs_or_prunes_skills(self) -> None:
        self.install("claude")
        with (
            mock.patch("src.skills_installer.install_all", side_effect=AssertionError),
            mock.patch("src.skills_installer.install_source", side_effect=AssertionError),
            mock.patch("src.skills_installer.update_all", side_effect=AssertionError),
            mock.patch("src.skill_prune.apply_prune", side_effect=AssertionError),
        ):
            self.assertEqual(0, self.run_review(cli="claude"))
        self.assertFalse([call for call in self.calls() if call["name"] == "npx"])


class ScopeAndPromptTests(SetupReviewTestCase):
    def _prompt(self, scope: str) -> str:
        return setup_review.build_prompt(
            self.paths,
            scope,  # type: ignore[arg-type]
            run_id="r",
            date="2026-10-12",
            checkouts=[setup_review.Checkout("dotfiles", Path("/home/u/dotfiles"), head="abc")],
            sources=[
                setup_review.SourceCopy(
                    "akindu", "Akindu23/my-agent-skills", "all", ("x",), Path("/s/akindu")
                )
            ],
            budget_minutes=40,
            budget_sources=40,
            vault="State synced",
            facts="FACTS-TABLE",
            previous="- review_x.md (created)",
        )

    def test_the_prompt_carries_the_safety_and_review_contract(self) -> None:
        prompt = self._prompt("full")
        for required in (
            "untrusted data",
            "possible prompt",
            "Do not change anything",
            "`skills prune`",
            "do not start subagents",
            "Keep your default model and reasoning settings",
            "/home/{user}/",
            "Official sources first",
            "Reddit, Hacker News",
            "confidence",
            "keep, exclude, replace, or drop-source",
            "propose an explicit keep list",
            "## Across repositories",
            "`## Skill-source review`",
            "dotfiles, agentbot, dotfiles-shared",
            "State synced",
            "/s/akindu",
            "FACTS-TABLE",
            "- review_x.md (created)",
            "Still open from earlier reviews",
            "### Checklist: dotfiles",
            "### Checklist: agentbot",
            "### Checklist: dotfiles-shared",
            "### Checklist: across repositories",
            "**critical**",
            "`path:line`",
            "**suspected**",
            "Do not report",
            "## Not reached",
        ):
            self.assertIn(required, prompt)
        self.assertNotIn("{{", prompt)

    def test_a_single_component_scope_gets_only_its_own_checklist(self) -> None:
        names = {
            "dotfiles": "dotfiles",
            "agentbot": "agentbot",
            "shared": "dotfiles-shared",
        }
        for scope, title in names.items():
            with self.subTest(scope=scope):
                prompt = self._prompt(scope)
                self.assertNotIn("Skill-source review", prompt)
                self.assertIn(f"### Checklist: {title}\n", prompt)
                self.assertEqual(1, prompt.count("### Checklist:"))
        self.assertIn("Skill-source review", self._prompt("skills"))

    def test_skill_sources_are_copied_only_when_reviewed(self) -> None:
        self.install("claude")
        for scope, copied in (("dotfiles", False), ("skills", True), ("full", True)):
            with self.subTest(scope=scope):
                self.prepare_sources.reset_mock()
                self.assertEqual(0, self.run_review(cli="claude", scope=scope))
                self.assertEqual(copied, self.prepare_sources.called)

    def test_cli_config_scope_reads_only_the_cli_directory(self) -> None:
        self.install("claude")

        self.assertEqual(0, self.run_review(cli="claude", scope="cli-config"))

        self.prepare_sources.assert_not_called()
        argv = self.review_calls()[0]["argv"]
        added = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "--add-dir"]
        self.assertEqual([str(REPO / "cli")], added)
        self.assertIn("Do not read other files on this machine", argv[-1])
        self.assertIn("do not start subagents", argv[-1])

    def test_prompt_templates_use_no_cli_specific_tool(self) -> None:
        # The prompt reviews the four CLIs' configuration, so it names them;
        # it must not depend on one CLI's own tools to do the review.
        for template in (REPO / "review").rglob("*.md"):
            text = template.read_text()
            for tool in ("WebFetch", "WebSearch", "web_search", "apply_patch", "Task tool", "/agents"):
                self.assertNotIn(tool, text, template.name)

    def test_every_checklist_path_exists_in_the_workspace(self) -> None:
        # A checklist that names a file the repository no longer has sends
        # the agent looking for nothing. Checked against the sibling
        # checkouts when the tests run inside the workspace.
        workspace = REPO.parent
        roots = {
            "dotfiles.md": workspace / "dotfiles",
            "agentbot.md": REPO,
            "shared.md": workspace / "dotfiles-shared",
        }
        for name, root in roots.items():
            if not root.is_dir():
                self.skipTest(f"{root} is not checked out beside agentbot")
            text = (REPO / "review" / "checklists" / name).read_text()
            for path in re.findall(r"`((?:[\w.-]+/)+[\w.-]+(?:\.\w+)?/?)`", text):
                if "*" in path or "<" in path:
                    continue
                with self.subTest(checklist=name, path=path):
                    self.assertTrue((root / path).exists(), path)


class EarlierReviewTests(SetupReviewTestCase):
    def test_none_filed_yet(self) -> None:
        self.assertEqual("None filed yet.", setup_review.previous_reviews(self.home / "dotfiles"))

    def test_the_log_and_the_newest_unread_review_are_given(self) -> None:
        self.install("claude")
        with mock.patch.object(setup_review, "review_file_name", return_value="review_a_full.md"):
            self.assertEqual(0, self.run_review(cli="claude"))
        with mock.patch.object(setup_review, "review_file_name", return_value="review_b_full.md"):
            self.assertEqual(0, self.run_review(cli="claude"))
        clone = self.home / "dotfiles"
        git(clone, "fetch", "--quiet", "origin")

        text = setup_review.previous_reviews(clone)

        self.assertIn("- review_a_full.md (created)", text)
        self.assertIn("The newest one not yet marked reviewed, `review_b_full.md`", text)
        self.assertIn("**Summary**: fine.", text)

    def test_the_second_review_is_told_about_the_first(self) -> None:
        self.install("claude")
        self.assertEqual(0, self.run_review(cli="claude"))
        self.log.unlink()
        for folder in (self.paths.config_home / "setup-reviews").glob("*"):
            shutil.rmtree(folder)

        self.assertEqual(0, self.run_review(cli="claude"))

        prompt = self.review_calls()[0]["argv"][-1]
        self.assertIn("(created)", prompt)
        self.assertIn("The newest one not yet marked reviewed", prompt)


class FactsTests(unittest.TestCase):
    def test_table_rows_keep_rows_and_drop_rules_and_colour(self) -> None:
        text = (
            "  === Update report ===\n"
            "  \x1b[1mcomponent\x1b[0m | installed | action\n"
            "  ----------+-----------+-------\n"
            "  Boost CLI | v0.15.4   | \x1b[32mlatest unchecked\x1b[0m\n"
            "  1 verified update available.\n"
        )

        rows = setup_review._table_rows(text)

        self.assertEqual(
            "  component | installed | action\n  Boost CLI | v0.15.4   | latest unchecked",
            rows,
        )


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
