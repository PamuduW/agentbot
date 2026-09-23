import hashlib
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


def tree_fingerprint(root: Path) -> tuple[tuple[str, str], ...]:
    if not root.exists():
        return ()
    return tuple(
        (path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    )


class UpdateLifecycleTests(unittest.TestCase):
    def _fixture(self, temporary: str):
        from src.paths import AgentbotPaths

        root = Path(temporary) / "repo"
        home = Path(temporary) / "home"
        root.mkdir()
        home.mkdir()
        (root / "skills.sources.yaml").write_text(
            "version: 1\nagents: [codex]\nscope: global\nsources:\n"
            "  - id: source\n    repo: owner/repo\n    skills: all\n",
            encoding="utf-8",
        )
        lock = home / ".agents" / ".skill-lock.json"
        lock.parent.mkdir(parents=True)
        lock.write_text(
            '{"version": 3, "skills": {"alpha": {"source": "owner/repo"}}}\n',
            encoding="utf-8",
        )
        paths = AgentbotPaths(
            root,
            home / ".codex",
            home / ".claude",
            home / ".cursor",
            home / ".config" / "agentbot",
            agents_home=home / ".agents",
        )
        return root, home, paths

    def test_plan_update_is_read_only_and_uses_remote_catalog(self) -> None:
        from src.graphify import GraphifyStatus
        from src.lifecycle import Lifecycle
        from src.skill_catalog import SourceCatalog
        from src.workspace_service import WorkspaceReport

        with tempfile.TemporaryDirectory() as temporary:
            _root, home, paths = self._fixture(temporary)
            graphify = mock.Mock()
            graphify.status.return_value = GraphifyStatus(
                "not-installed",
                None,
                None,
                home / ".agents/skills/graphify/SKILL.md",
                None,
                "missing",
                "missing",
                "not installed",
            )
            lifecycle = Lifecycle(
                paths,
                graphify=graphify,
                catalog_discoverer=lambda _config: (
                    SourceCatalog("source", "owner/repo", "abc123", ("alpha", "beta")),
                ),
                repository_head=lambda _root: "head123",
                workspace_preview=lambda: WorkspaceReport(()),
            )
            before = tree_fingerprint(Path(temporary))

            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                plan = lifecycle.plan_update()

            self.assertEqual(before, tree_fingerprint(Path(temporary)))
            self.assertEqual(("beta",), plan.reconcile.wildcard_additions)
            self.assertEqual("skip", plan.graphify_action)
            self.assertEqual("head123", plan.snapshot.repository_head)
            self.assertEqual("abc123", plan.source_catalogs[0].revision)
            self.assertIsNotNone(plan.cli_config)

    def test_plan_update_does_not_readd_excluded_wildcard_skills(self) -> None:
        """Break caught: update planning resurrects a skill excluded during install."""
        from src.graphify import GraphifyStatus
        from src.lifecycle import Lifecycle
        from src.skill_catalog import SourceCatalog
        from src.workspace_service import WorkspaceReport

        with tempfile.TemporaryDirectory() as temporary:
            root, home, paths = self._fixture(temporary)
            manifest = root / "skills.sources.yaml"
            manifest.write_text(
                manifest.read_text(encoding="utf-8") + "    exclude:\n      - beta\n",
                encoding="utf-8",
            )
            graphify = mock.Mock()
            graphify.status.return_value = GraphifyStatus(
                "not-installed",
                None,
                None,
                home / ".agents/skills/graphify/SKILL.md",
                None,
                "missing",
                "missing",
                "not installed",
            )
            lifecycle = Lifecycle(
                paths,
                graphify=graphify,
                catalog_discoverer=lambda _config: (
                    SourceCatalog("source", "owner/repo", "abc123", ("alpha", "beta")),
                ),
                repository_head=lambda _root: "head123",
                workspace_preview=lambda: WorkspaceReport(()),
            )

            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                plan = lifecycle.plan_update()

            self.assertEqual((), plan.reconcile.wildcard_additions)
            self.assertEqual((), plan.reconcile.wildcard_removals)

    def test_apply_rejects_stale_local_snapshot_before_any_stage_runs(self) -> None:
        from src.graphify import GraphifyStatus
        from src.lifecycle import Lifecycle
        from src.skill_catalog import SourceCatalog
        from src.workspace_service import WorkspaceReport

        with tempfile.TemporaryDirectory() as temporary:
            _root, home, paths = self._fixture(temporary)
            graphify = mock.Mock()
            graphify.status.return_value = GraphifyStatus(
                "not-installed",
                None,
                None,
                home / ".agents/skills/graphify/SKILL.md",
                None,
                "missing",
                "missing",
                "not installed",
            )
            apply_stage = mock.Mock()
            lifecycle = Lifecycle(
                paths,
                graphify=graphify,
                catalog_discoverer=lambda _config: (
                    SourceCatalog("source", "owner/repo", "abc123", ("alpha",)),
                ),
                repository_head=lambda _root: "head123",
                workspace_preview=lambda: WorkspaceReport(()),
                update_applier=apply_stage,
            )
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                plan = lifecycle.plan_update()
                with paths.skills_sources_file.open("a", encoding="utf-8") as manifest:
                    manifest.write("# changed after preview\n")
                before_apply = tree_fingerprint(Path(temporary))
                outcome = lifecycle.apply_update(plan)

            self.assertEqual("stale-plan", outcome.status)
            self.assertIn("preview again", outcome.message)
            self.assertEqual(before_apply, tree_fingerprint(Path(temporary)))
            apply_stage.assert_not_called()

    def test_apply_uses_verified_catalog_and_runs_each_stage_once(self) -> None:
        from src.graphify import GraphifyStatus
        from src.lifecycle import Lifecycle
        from src.models import DiagnosticsSnapshot
        from src.skill_catalog import SourceCatalog
        from src.skill_reconcile import ReconcileResult
        from src.workspace_service import WorkspaceReport

        with tempfile.TemporaryDirectory() as temporary:
            root, home, paths = self._fixture(temporary)
            (root / "cli").mkdir()
            (root / "cli" / "codex.config.toml").write_text('model = "new"\n')
            paths.codex_home.mkdir()
            codex_config = paths.codex_home / "config.toml"
            codex_config.write_text('model = "old"\n[tui]\ntheme = "dark"\n')
            skill_path = home / ".agents/skills/graphify/SKILL.md"
            skill_path.parent.mkdir(parents=True)
            skill_path.write_text("# graphify\n", encoding="utf-8")
            status = GraphifyStatus(
                "ready",
                Path("/bin/graphify"),
                "1",
                skill_path,
                "1",
                "linked",
                "linked",
                "ready",
            )
            graphify = mock.Mock()
            graphify.status.return_value = status
            graphify.setup.return_value = status
            diagnostics = mock.Mock()
            diagnostics.collect.return_value = DiagnosticsSnapshot(
                (), 1, True, True, True, 1, 1, 0, 1, "ok", ()
            )
            catalog = SourceCatalog("source", "owner/repo", "abc123", ("alpha",))
            events: list[str] = []

            @contextmanager
            def checkouts(_config, _expected):
                events.append("checkout")
                yield {"source": root / "checkout"}

            def reconcile(*_args, **kwargs):
                events.append("reconcile")
                self.assertIn(codex_config, kwargs["extra_affected"])
                kwargs["validate"]()
                return ReconcileResult("applied", (), (), ())

            def install(_paths, _checkouts):
                events.append("install")
                return []

            def apply_surfaces(*, apply: bool, paths=()):
                self.assertTrue(apply)
                events.append("surfaces")
                return WorkspaceReport(())

            lifecycle = Lifecycle(
                paths,
                diagnostics=diagnostics,
                graphify=graphify,
                catalog_discoverer=lambda _config: (catalog,),
                repository_head=lambda _root: "head123",
                workspace_preview=lambda: WorkspaceReport(()),
                checkout_provider=checkouts,
                reconcile_applier=reconcile,
                planned_installer=install,
            )
            lifecycle.resync_workspaces = apply_surfaces

            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                plan = lifecycle.plan_update()
                self.assertIn("model", plan.cli_config.plans["codex"].changes)
                outcome = lifecycle.apply_update(plan)

            self.assertEqual("applied", outcome.status)
            self.assertEqual(["checkout", "reconcile", "install", "surfaces"], events)
            self.assertIn('model = "new"', codex_config.read_text())
            self.assertIn('[tui]\ntheme = "dark"', codex_config.read_text())
            self.assertIsNotNone(outcome.cli_config)
            graphify.setup.assert_called_once_with()
            diagnostics.collect.assert_called_once_with()

    def test_apply_restores_managed_state_when_a_later_stage_fails(self) -> None:
        from src.graphify import GraphifyStatus
        from src.lifecycle import Lifecycle
        from src.skill_catalog import SourceCatalog
        from src.workspace_service import WorkspaceReport

        with tempfile.TemporaryDirectory() as temporary:
            root, home, paths = self._fixture(temporary)
            original = home / ".agents" / "skills" / "alpha" / "SKILL.md"
            original.parent.mkdir(parents=True)
            original.write_text("# original\n", encoding="utf-8")
            catalog = SourceCatalog("source", "owner/repo", "abc123", ("alpha",))
            graphify = mock.Mock()
            graphify.status.return_value = GraphifyStatus(
                "not-installed",
                None,
                None,
                home / ".agents/skills/graphify/SKILL.md",
                None,
                "missing",
                "missing",
                "not installed",
            )

            @contextmanager
            def checkouts(_config, _expected):
                yield {"source": root / "checkout"}

            def fail_install(_paths, _checkouts):
                original.write_text("# changed\n", encoding="utf-8")
                raise RuntimeError("install failed")

            lifecycle = Lifecycle(
                paths,
                graphify=graphify,
                catalog_discoverer=lambda _config: (catalog,),
                repository_head=lambda _root: "head123",
                workspace_preview=lambda: WorkspaceReport(()),
                checkout_provider=checkouts,
                planned_installer=fail_install,
            )

            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                plan = lifecycle.plan_update()
                outcome = lifecycle.apply_update(plan)

            self.assertEqual("failed", outcome.status)
            self.assertEqual("# original\n", original.read_text(encoding="utf-8"))
            self.assertIsNotNone(outcome.reconcile)
            self.assertIsNotNone(outcome.reconcile.backup_path)
            self.assertTrue(outcome.reconcile.backup_path.exists())


if __name__ == "__main__":
    unittest.main()
