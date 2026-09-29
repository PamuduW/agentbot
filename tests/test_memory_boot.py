"""`agentbot boot` also gives the repository project memory: one command sets it all up."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from src import memory_projects
from src.workspace_service import WorkspaceResult
from tests.support import run_cli_main
from tests.test_memory import old_schema_vault
from tests.test_memory_project_writes import WritesTestCase
from tests.test_memory_projects import repo
from tests.test_memory_sync import git


class BootMemoryTests(WritesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.beta = repo(self.tmp / "code/beta", "git@github.com:me/beta.git")

    def boot(self, target: Path, *extra: str, vault: Path | None = None, status="applied"):
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBOT_MEMORY")}
        env.update(
            HOME=str(self.tmp / "home"),
            NO_COLOR="1",
            XDG_CONFIG_HOME=str(self.config.parent),
            AGENTBOT_CALLER_PWD=str(self.tmp),
        )
        if vault is not None:
            env["AGENTBOT_MEMORY_DIR"] = str(vault)
        lifecycle = MagicMock()
        lifecycle.apply_workspace.return_value = WorkspaceResult(target, status, (), "rendered")
        with patch.dict(os.environ, env, clear=True), patch("src.cli.Lifecycle") as made:
            made.return_value = lifecycle
            rc, stdout, _ = run_cli_main(
                ["agentbot", "--root", str(self.tmp / "agentbot"), "boot", *extra, str(target)]
            )
        return rc, stdout

    def registry(self, root: Path) -> list[str]:
        data = json.loads((root / ".meta/projects.json").read_text())
        return sorted(entry["folder"] for entry in data["projects"])

    def test_boot_registers_an_unregistered_repository_and_pushes_it(self) -> None:
        rc, stdout = self.boot(self.beta, vault=self.a)
        self.assertEqual(0, rc, stdout)
        self.assertIn("Project memory", stdout)
        self.assertIn("beta registered", stdout)
        self.assertIn("beta", self.registry(self.a))
        self.assertIn("projects/beta/project.md", git(self.origin, "ls-tree", "-r", "main"))
        self.assertEqual([], memory_projects.resolve(self.a, self.beta, self.config).problems)

    def test_a_second_boot_reports_the_existing_project(self) -> None:
        self.boot(self.beta, vault=self.a)
        head = git(self.a, "rev-parse", "HEAD")
        rc, stdout = self.boot(self.beta, vault=self.a)
        self.assertEqual(0, rc)
        self.assertIn("already registered", stdout)
        self.assertEqual(head, git(self.a, "rev-parse", "HEAD"))

    def test_boot_sees_a_registration_from_another_machine(self) -> None:
        self.boot(self.beta, vault=self.a)
        rc, stdout = self.boot(self.beta, vault=self.b)
        self.assertEqual(0, rc)
        self.assertIn("already registered", stdout)
        self.assertEqual(1, self.registry(self.b).count("beta"))

    def test_memory_is_skipped_without_failing_the_boot(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        old = old_schema_vault(self.tmp / "old")
        cases = [
            ("not a repository", plain, (), self.a, "not a Git repository"),
            ("no vault", self.beta, (), None, "no memory vault configured"),
            ("older schema", self.beta, (), old.root, "schema 3"),
        ]
        for label, target, extra, vault, expected in cases:
            with self.subTest(label):
                head = git(self.a, "rev-parse", "HEAD")
                rc, stdout = self.boot(target, *extra, vault=vault)
                self.assertEqual(0, rc, stdout)
                self.assertIn(expected, stdout)
                self.assertEqual(head, git(self.a, "rev-parse", "HEAD"))

    def test_no_memory_and_a_failed_boot_leave_the_vault_alone(self) -> None:
        head = git(self.a, "rev-parse", "HEAD")
        rc, stdout = self.boot(self.beta, "--no-memory", vault=self.a)
        self.assertEqual(0, rc)
        self.assertNotIn("Project memory", stdout)
        rc, stdout = self.boot(self.beta, vault=self.a, status="conflict")
        self.assertEqual(1, rc)
        self.assertNotIn("Project memory", stdout)
        self.assertEqual(head, git(self.a, "rev-parse", "HEAD"))
        self.assertNotIn("beta", self.registry(self.a))

    def test_a_registry_collision_is_reported_not_written(self) -> None:
        path = self.a / ".meta/projects.json"
        data = json.loads(path.read_text())
        for number, folder in enumerate(("beta", "twin")):
            data["projects"].append(
                {
                    "id": f"c0c0c0c0-0000-4000-8000-00000000000{number}",
                    "origin": "github.com/me/beta",
                    "folder": folder,
                }
            )
        path.write_text(json.dumps(data))
        rc, stdout = self.boot(self.beta, vault=self.a)
        self.assertEqual(0, rc)
        self.assertIn("matches 2 projects", stdout)
        self.assertNotIn("projects/beta/project.md", git(self.origin, "ls-tree", "-r", "main"))
