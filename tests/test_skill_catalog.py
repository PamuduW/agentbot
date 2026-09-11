import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _config_with_sources(ids):
    from src.skills_sources import SkillSourceEntry, SkillsSourcesConfig

    return SkillsSourcesConfig(
        version=1,
        agents=["codex"],
        scope="global",
        sources=[SkillSourceEntry(name, f"owner/{name}", ["*"]) for name in ids],
    )


class SkillCatalogTests(unittest.TestCase):
    def test_discovery_uses_frontmatter_name_and_folder_fallback_once(self) -> None:
        from src.skill_catalog import discover_checkout_skills, skill_name_from_file

        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary)
            named = checkout / "nested" / "folder-name" / "SKILL.md"
            named.parent.mkdir(parents=True)
            named.write_text(
                "---\nname: canonical-name\ndescription: test\n---\n# Skill\n",
                encoding="utf-8",
            )
            fallback = checkout / "fallback-name" / "SKILL.md"
            fallback.parent.mkdir()
            fallback.write_text("# Skill\n", encoding="utf-8")

            self.assertEqual("canonical-name", skill_name_from_file(named))
            self.assertEqual(
                ("canonical-name", "fallback-name"),
                discover_checkout_skills(checkout),
            )

    def test_remote_discovery_removes_temporary_checkout_on_success(self) -> None:
        from src.skill_catalog import discover_remote_catalogs
        from src.skills_sources import SkillSourceEntry, SkillsSourcesConfig

        config = SkillsSourcesConfig(
            version=1,
            agents=["codex"],
            scope="global",
            sources=[SkillSourceEntry("source", "owner/repo", ["*"])],
        )
        destinations: list[Path] = []

        def clone(_repo: str, destination: Path) -> None:
            destinations.append(destination)
            skill = destination / "alpha" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("# alpha\n", encoding="utf-8")

        catalogs = discover_remote_catalogs(
            config,
            clone_source=clone,
            revision_reader=lambda _checkout: "revision-1",
        )

        self.assertEqual(("alpha",), catalogs[0].skills)
        self.assertTrue(destinations)
        self.assertTrue(all(not destination.exists() for destination in destinations))

    def test_sources_are_inspected_concurrently_and_returned_in_manifest_order(self) -> None:
        """Twelve sequential shallow clones was thirty seconds before a prompt.

        They wait on the network rather than compete for anything, so they run
        together -- but the plan must not depend on which one finished first.
        """
        import threading
        import time

        from src.skill_catalog import discover_remote_catalogs

        config = _config_with_sources(("alpha", "beta", "gamma"))
        overlapped = threading.Event()
        running = []
        lock = threading.Lock()

        def clone(_repo: str, destination: Path) -> None:
            with lock:
                running.append(destination.name)
                if len(running) > 1:
                    overlapped.set()
            # Slow enough that sequential clones could not overlap by accident.
            time.sleep(0.2)
            skill = destination / destination.name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("# s\n", encoding="utf-8")
            with lock:
                running.remove(destination.name)

        started = time.monotonic()
        catalogs = discover_remote_catalogs(
            config, clone_source=clone, revision_reader=lambda _c: "rev"
        )
        elapsed = time.monotonic() - started

        self.assertTrue(overlapped.is_set(), "the clones did not overlap")
        self.assertLess(elapsed, 0.6, "three 0.2s clones did not run together")
        # Manifest order, not completion order.
        self.assertEqual(["alpha", "beta", "gamma"], [c.source_id for c in catalogs])

    def test_one_worker_still_inspects_every_source(self) -> None:
        """AGENTBOT_CATALOG_WORKERS=1 is the way back to one at a time."""
        import os
        from unittest import mock

        from src.skill_catalog import discover_remote_catalogs

        config = _config_with_sources(("alpha", "beta"))

        def clone(_repo: str, destination: Path) -> None:
            skill = destination / "s"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("# s\n", encoding="utf-8")

        with mock.patch.dict(os.environ, {"AGENTBOT_CATALOG_WORKERS": "1"}):
            catalogs = discover_remote_catalogs(
                config, clone_source=clone, revision_reader=lambda _c: "rev"
            )
        self.assertEqual(["alpha", "beta"], [c.source_id for c in catalogs])

    def test_verified_checkouts_reject_remote_drift_before_yielding(self) -> None:
        from src.skill_catalog import (
            SourceCatalog,
            StaleSourceCatalogError,
            discover_remote_catalogs,
            verified_source_checkouts,
        )
        from src.skills_sources import SkillSourceEntry, SkillsSourcesConfig

        config = SkillsSourcesConfig(
            1,
            ["codex"],
            "global",
            [SkillSourceEntry("source", "owner/repo", ["*"])],
        )
        expected = (SourceCatalog("source", "owner/repo", "old", ("alpha",)),)
        destinations: list[Path] = []

        def clone(_repo: str, destination: Path) -> None:
            destinations.append(destination)
            skill = destination / "alpha" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("# alpha\n", encoding="utf-8")

        with self.assertRaises(StaleSourceCatalogError):
            with verified_source_checkouts(
                config,
                expected,
                clone_source=clone,
                revision_reader=lambda _checkout: "new",
            ):
                self.fail("stale source checkout must not be exposed")

        self.assertTrue(all(not destination.exists() for destination in destinations))

        destinations.clear()

        def fail_clone(_repo: str, destination: Path) -> None:
            destinations.append(destination)
            raise RuntimeError("clone failed")

        with self.assertRaisesRegex(RuntimeError, "clone failed"):
            discover_remote_catalogs(
                config,
                clone_source=fail_clone,
                revision_reader=mock.Mock(),
            )
        self.assertTrue(all(not destination.exists() for destination in destinations))

    def test_default_clone_uses_the_bounded_command_runner(self) -> None:
        from src.command_runner import CommandResult
        from src.skill_catalog import _clone_source

        runner = mock.Mock()
        runner.run.return_value = CommandResult(0)
        with tempfile.TemporaryDirectory() as temporary:
            _clone_source("owner/repo", Path(temporary) / "checkout", runner=runner)

        self.assertEqual(300, runner.run.call_args.kwargs["timeout_seconds"])
        self.assertEqual(
            ["git", "clone", "--depth=1", "https://github.com/owner/repo.git"],
            runner.run.call_args.args[0][:4],
        )


if __name__ == "__main__":
    unittest.main()
