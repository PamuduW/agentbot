"""Ticket M3: project identity across machines."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import memory_projects as projects
from src import memory_setup
from src import memory_sync as sync
from src.memory import MemoryVaultError
from tests.support import run_cli_main
from tests.test_memory_sync import GIT, Fleet, git


def repo(
    path: Path, origin: str | None = None, *, extra_remotes: dict[str, str] | None = None
) -> Path:
    subprocess.run([*GIT, "init", "-q", "-b", "main", str(path)], check=True)
    if origin:
        git(path, "remote", "add", "origin", origin)
    for name, url in (extra_remotes or {}).items():
        git(path, "remote", "add", name, url)
    return path


class IdentityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.fleet = Fleet(self.tmp / "fleet")
        self.vault = self.fleet.a
        self.config = self.tmp / "config-a"


class NormalizationTests(unittest.TestCase):
    def test_equivalent_remote_forms_normalize_to_one_origin(self) -> None:
        same = [
            "git@github.com:PamuduW/FreePlayground.git",
            "ssh://git@github.com/PamuduW/freeplayground",
            "ssh://git@github.com:22/pamuduw/freeplayground.git",
            "https://github.com/PamuduW/freeplayground.git",
            "https://github.com/pamuduw/freeplayground/",
            "https://token@github.com/pamuduw/freeplayground.git",
        ]
        self.assertEqual(
            {"github.com/pamuduw/freeplayground"}, {sync.normalize_origin(u) for u in same}
        )

    def test_unknown_forges_keep_path_case(self) -> None:
        self.assertEqual(
            "git.example.org/Team/Repo",
            sync.normalize_origin("https://git.example.org/Team/Repo.git"),
        )
        self.assertNotEqual(
            sync.normalize_origin("https://git.example.org/Team/Repo"),
            sync.normalize_origin("https://git.example.org/team/repo"),
        )

    def test_a_port_that_is_not_the_default_is_part_of_the_identity(self) -> None:
        # Review 1, M-R13: two services on one host are two repositories.
        self.assertEqual(
            "git.example.org:2222/team/repo",
            sync.normalize_origin("ssh://git@git.example.org:2222/team/repo.git"),
        )
        self.assertNotEqual(
            sync.normalize_origin("https://git.example.org:8443/team/repo"),
            sync.normalize_origin("https://git.example.org:9443/team/repo"),
        )
        self.assertEqual(
            "git.example.org/team/repo",
            sync.normalize_origin("https://git.example.org:443/team/repo"),
        )

    def test_an_ssh_host_alias_resolves_to_its_real_host(self) -> None:
        def ssh_config(host: str) -> tuple[str, int | None]:
            return ("github.com", 22) if host == "github-work" else (host, None)

        with patch.object(sync, "_ssh_endpoint", side_effect=ssh_config):
            self.assertEqual(
                sync.normalize_origin("git@github.com:Me/Repo.git"),
                sync.normalize_origin("git@github-work:me/repo.git"),
            )

    def test_an_alias_that_looks_like_an_option_is_never_passed_to_ssh(self) -> None:
        with patch.object(sync.subprocess, "run") as run:
            self.assertEqual(("-oProxyCommand=x", None), sync._ssh_endpoint("-oProxyCommand=x"))
        run.assert_not_called()

    def test_credentials_never_survive(self) -> None:
        self.assertEqual(
            "gitlab.com/o/r", sync.normalize_origin("https://user:secret@gitlab.com/o/r.git?x=1")
        )


class ResolutionTests(IdentityTestCase):
    def test_ssh_and_https_checkouts_resolve_to_the_same_project(self) -> None:
        ssh = repo(self.tmp / "pcA/freeplayground", "git@github.com:PamuduW/freeplayground.git")
        entry = projects.register(self.vault, ssh, self.config)
        sync.sync(self.vault)
        sync.sync(self.fleet.b)
        https = repo(
            self.tmp / "pcB/code/freeplayground", "https://github.com/pamuduw/freeplayground"
        )
        (https / "src").mkdir()
        resolved = projects.resolve(self.fleet.b, https / "src", self.tmp / "config-b")
        self.assertEqual("resolved", resolved.state)
        self.assertEqual(
            (entry["id"], "freeplayground"), (resolved.entry["id"], resolved.entry["folder"])
        )

    def test_worktrees_resolve_like_their_repository(self) -> None:
        main = repo(self.tmp / "main", "https://github.com/fixture/hot")
        subprocess.run(
            [*GIT, "-C", str(main), "commit", "-q", "--allow-empty", "-m", "c"],
            check=True,
            capture_output=True,
        )
        git(main, "worktree", "add", "-q", str(self.tmp / "wt"), "-b", "side")
        a = projects.resolve(self.vault, main, self.config)
        b = projects.resolve(self.vault, self.tmp / "wt", self.config)
        self.assertEqual(("resolved", "hot"), (a.state, a.entry["folder"]))
        self.assertEqual(a.entry, b.entry)

    def test_a_fork_is_a_separate_project(self) -> None:
        fork = repo(
            self.tmp / "fork",
            "https://github.com/someone-else/hot",
            extra_remotes={"upstream": "https://github.com/fixture/hot"},
        )
        self.assertEqual("unregistered", projects.resolve(self.vault, fork, self.config).state)
        projects.link(self.vault, "https://github.com/someone-else/hot", "hot")
        resolved = projects.resolve(self.vault, fork, self.config)
        self.assertEqual(("resolved", "hot"), (resolved.state, resolved.entry["folder"]))

    def test_a_rename_is_linked_not_guessed(self) -> None:
        renamed = repo(self.tmp / "renamed", "https://github.com/fixture/hot-v2")
        self.assertEqual("unregistered", projects.resolve(self.vault, renamed, self.config).state)
        hot = projects.link(self.vault, "https://github.com/fixture/hot-v2", "hot")
        again = projects.resolve(self.vault, renamed, self.config)
        self.assertEqual(hot["id"], again.entry["id"])

    def test_outside_a_repository_there_is_no_project(self) -> None:
        plain = self.tmp / "not-a-repo"
        plain.mkdir()
        self.assertEqual("no-repository", projects.resolve(self.vault, plain, self.config).state)
        with self.assertRaises(MemoryVaultError):
            projects.require_project(self.vault, plain, self.config)


class RegistrationTests(IdentityTestCase):
    def test_register_names_the_folder_after_the_repository(self) -> None:
        r = repo(self.tmp / "My Cool.Repo", "https://github.com/me/My.Cool-Repo.git")
        entry = projects.register(self.vault, r, self.config)
        self.assertEqual("my-cool-repo", entry["folder"])
        self.assertEqual("resolved", projects.resolve(self.vault, r, self.config).state)
        with self.assertRaises(MemoryVaultError):
            projects.register(self.vault, r, self.config)

    def test_a_folder_name_clash_gets_a_suffix(self) -> None:
        other = repo(self.tmp / "hot", "https://gitlab.com/elsewhere/hot")
        entry = projects.register(self.vault, other, self.config)
        self.assertEqual("hot-2", entry["folder"])

    def test_registrations_on_two_machines_merge(self) -> None:
        one = repo(self.tmp / "one", "https://github.com/me/one")
        two = repo(self.tmp / "two", "https://github.com/me/two")
        projects.register(self.fleet.a, one, self.config)
        projects.register(self.fleet.b, two, self.tmp / "config-b")
        self.fleet.settle()
        folders = {
            e["folder"]
            for e in json.loads((self.fleet.a / ".meta/projects.json").read_text())["projects"]
        }
        self.assertTrue({"one", "two"} <= folders)


class LocalRepositoryTests(IdentityTestCase):
    def test_a_no_remote_repository_needs_an_explicit_attach_elsewhere(self) -> None:
        local_a = repo(self.tmp / "pcA/scratch")
        entry = projects.register(self.vault, local_a, self.config)
        registry = json.loads((self.vault / ".meta/projects.json").read_text())
        origin = next(e["origin"] for e in registry["projects"] if e["id"] == entry["id"])
        self.assertTrue(origin.startswith("local/") and origin.endswith("-scratch"))
        self.assertEqual("resolved", projects.resolve(self.vault, local_a, self.config).state)
        self.assertEqual(0o600, os.stat(self.config / memory_setup.BINDINGS_NAME).st_mode & 0o777)
        self.fleet.settle()
        # Machine B has an unrelated-looking local checkout with the same name.
        local_b = repo(self.tmp / "pcB/scratch")
        config_b = self.tmp / "config-b"
        self.assertEqual("unregistered", projects.resolve(self.fleet.b, local_b, config_b).state)
        projects.attach(self.fleet.b, local_b, config_b, entry["folder"])
        resolved = projects.resolve(self.fleet.b, local_b, config_b)
        self.assertEqual(entry["id"], resolved.entry["id"])
        # The binding is private per machine, never in the vault.
        self.assertNotIn(str(local_b), (self.fleet.b / ".meta/projects.json").read_text())

    def test_attach_is_only_for_local_projects_and_known_ids(self) -> None:
        local = repo(self.tmp / "loose")
        with self.assertRaises(MemoryVaultError):
            projects.attach(self.vault, local, self.config, "hot")  # hot has a real origin
        with self.assertRaises(MemoryVaultError):
            projects.attach(self.vault, local, self.config, "no-such-project")
        remote = repo(self.tmp / "remote", "https://github.com/me/remote")
        with self.assertRaises(MemoryVaultError):
            projects.attach(self.vault, remote, self.config, "hot")


class CollisionTests(IdentityTestCase):
    def poison(self, entries: list[dict]) -> None:
        path = self.vault / ".meta/projects.json"
        data = json.loads(path.read_text())
        data["projects"].extend(entries)
        path.write_text(json.dumps(data))

    def test_two_projects_claiming_one_origin_block_writes(self) -> None:
        self.poison([{"id": "dup-1", "origin": "github.com/fixture/hot", "folder": "hot-copy"}])
        r = repo(self.tmp / "hot", "https://github.com/fixture/hot")
        result = projects.resolve(self.vault, r, self.config)
        self.assertEqual("collision", result.state)
        with self.assertRaises(MemoryVaultError) as raised:
            projects.require_project(self.vault, r, self.config)
        self.assertIn("collision", str(raised.exception))

    def test_a_shared_folder_or_alias_is_a_collision(self) -> None:
        self.poison(
            [
                {"id": "x-1", "origin": "github.com/x/new", "folder": "alpha"},
                {
                    "id": "x-2",
                    "origin": "github.com/x/other",
                    "aliases": ["github.com/fixture/beta"],
                    "folder": "zeta",
                },
            ]
        )
        problems = projects.registry_problems(
            json.loads((self.vault / ".meta/projects.json").read_text())
        )
        self.assertIn("x-1", problems)
        self.assertIn("x-2", problems)
        beta = repo(self.tmp / "beta", "https://github.com/fixture/beta")
        self.assertEqual("collision", projects.resolve(self.vault, beta, self.config).state)

    def test_a_contested_registration_from_another_machine_is_a_sync_conflict(self) -> None:
        a = repo(self.tmp / "a/same", "https://github.com/me/same")
        b = repo(self.tmp / "b/same", "git@github.com:me/same.git")
        projects.register(self.fleet.a, a, self.config)
        projects.register(self.fleet.b, b, self.tmp / "config-b")
        sync.sync(self.fleet.a)
        result = sync.sync(self.fleet.b)
        # Registration is two operations (registry entry and project.md); both are
        # contested, and both are preserved rather than half applied.
        self.assertEqual({"register", "put"}, {c["kind"] for c in result.conflicts})
        self.fleet.settle()
        self.assertEqual("resolved", projects.resolve(self.fleet.b, b, self.tmp / "config-b").state)


class ProjectCliTests(IdentityTestCase):
    def test_cli_shows_the_resolution_for_the_caller_directory(self) -> None:
        r = repo(self.tmp / "hot-checkout", "git@github.com:fixture/hot.git")
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBOT_MEMORY")}
        env.update(
            HOME=str(self.tmp / "home"),
            NO_COLOR="1",
            AGENTBOT_MEMORY_DIR=str(self.vault),
            AGENTBOT_CALLER_PWD=str(r),
            XDG_CONFIG_HOME=str(self.tmp / "xdg"),
        )
        with patch.dict(os.environ, env, clear=True):
            rc, stdout, _ = run_cli_main(
                ["agentbot", "--root", str(self.tmp / "agentbot"), "memory", "project", "--json"]
            )
        self.assertEqual(0, rc, stdout)
        payload = json.loads(stdout)
        self.assertEqual(
            ("resolved", "github.com/fixture/hot", "hot"),
            (payload["state"], payload["origin"], payload["project"]["folder"]),
        )


if __name__ == "__main__":
    unittest.main()
