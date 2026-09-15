"""Classification and removal for `agentbot skills prune`.

Every case here is a state the real skill store has actually been in.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.paths import AgentbotPaths
from src.skill_prune import apply_prune, plan_prune
from src.skills_sources import load_skills_sources


class PruneTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.home = self.root / "home"
        self.paths = AgentbotPaths(
            root=self.root,
            codex_home=self.home / ".codex",
            claude_home=self.home / ".claude",
            cursor_home=self.home / ".cursor",
            config_home=self.home / ".config" / "agentbot",
            agents_home=self.home / ".agents",
        )
        self.store = self.paths.agents_skills_home
        self.store.mkdir(parents=True)

    def tearDown(self) -> None:
        self._temp.cleanup()

    # --- fixtures -----------------------------------------------------
    def _skill(self, name: str) -> Path:
        directory = self.store / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
        return directory

    def _lock(self, entries: dict[str, str]) -> None:
        payload = {
            "version": 3,
            "skills": {name: {"source": repo} for name, repo in entries.items()},
        }
        self.paths.global_skill_lock.parent.mkdir(parents=True, exist_ok=True)
        self.paths.global_skill_lock.write_text(json.dumps(payload), encoding="utf-8")

    def _manifest(self, body: str):
        path = self.paths.skills_sources_file
        path.write_text(body, encoding="utf-8")
        return load_skills_sources(path)

    def _bridge(self, name: str) -> tuple[Path, Path]:
        claude = self.paths.claude_skills_home
        codex = self.paths.codex_home / "skills"
        claude.mkdir(parents=True, exist_ok=True)
        codex.mkdir(parents=True, exist_ok=True)
        claude_link = claude / name
        codex_link = codex / name
        claude_link.symlink_to(self.store / name)
        codex_link.symlink_to(self.store / name)
        return claude_link, codex_link

    BASE = (
        "version: 1\nagents: [claude-code]\nscope: global\nsources:\n"
        "  - id: owner\n    repo: owner/repo\n    skills: all\n"
    )

    # --- classification ------------------------------------------------
    def test_a_skill_from_an_active_source_is_left_alone(self):
        self._skill("keeper")
        self._lock({"keeper": "owner/repo"})
        report = plan_prune(self.paths, self._manifest(self.BASE))
        self.assertEqual((), report.removable)

    def test_an_excluded_wildcard_skill_is_removable(self):
        self._skill("alpha")
        self._skill("keeper")
        self._lock({"alpha": "owner/repo", "keeper": "owner/repo"})
        config = self._manifest(self.BASE + "    exclude:\n      - alpha\n")
        report = plan_prune(self.paths, config)
        self.assertEqual(["alpha"], [item.name for item in report.removable])
        self.assertEqual("excluded", report.removable[0].reason)

    def test_a_pin_to_an_inactive_source_is_orphaned(self):
        self._skill("leftover")
        self._lock({"leftover": "gone/repo"})
        report = plan_prune(self.paths, self._manifest(self.BASE))
        self.assertEqual(["leftover"], [item.name for item in report.removable])
        self.assertEqual("orphaned", report.removable[0].reason)

    def test_a_lock_pin_without_a_directory_is_a_stale_pin(self):
        self._lock({"ghost": "owner/repo"})
        report = plan_prune(self.paths, self._manifest(self.BASE))
        self.assertEqual(["ghost"], [item.name for item in report.removable])
        self.assertEqual("stale-pin", report.removable[0].reason)

    def test_a_directory_without_a_lock_entry_is_manual_and_not_removable(self):
        self._skill("graphify")
        self._lock({})
        report = plan_prune(self.paths, self._manifest(self.BASE))
        self.assertEqual((), report.removable)
        self.assertEqual(["graphify"], [item.name for item in report.manual])

    def test_official_graphify_is_not_a_manual_prune_candidate(self):
        graphify = self._skill("graphify")
        (graphify / ".graphify_version").write_text("1.2.3\n", encoding="utf-8")
        self._lock({})

        report = plan_prune(self.paths, self._manifest(self.BASE))

        self.assertEqual((), report.manual)

    def test_a_disabled_source_orphans_its_skills(self):
        self._skill("dropped")
        self._lock({"dropped": "owner/repo"})
        config = self._manifest(self.BASE + "    enabled: false\n")
        report = plan_prune(self.paths, config)
        self.assertEqual(["dropped"], [item.name for item in report.removable])

    def test_malformed_lock_blocks_planning_without_manual_candidates(self):
        """Break caught: malformed managed state is presented as user-owned."""
        self._skill("managed-by-wildcard")
        self.paths.global_skill_lock.parent.mkdir(parents=True, exist_ok=True)
        self.paths.global_skill_lock.write_text("{invalid", encoding="utf-8")

        report = plan_prune(self.paths, self._manifest(self.BASE))

        self.assertEqual((), report.candidates)
        self.assertIn("invalid global skill lock", report.blocked_reason or "")

    def test_invalid_lock_entry_blocks_planning_without_manual_candidates(self):
        """Break caught: a malformed pin is dropped and its skill becomes manual."""
        self._skill("managed-by-wildcard")
        self.paths.global_skill_lock.parent.mkdir(parents=True, exist_ok=True)
        self.paths.global_skill_lock.write_text(
            json.dumps({"version": 3, "skills": {"managed-by-wildcard": "invalid"}}),
            encoding="utf-8",
        )

        report = plan_prune(self.paths, self._manifest(self.BASE))

        self.assertEqual((), report.candidates)
        self.assertIn("invalid global skill lock", report.blocked_reason or "")

    def test_unreadable_lock_blocks_planning_without_manual_candidates(self):
        """Break caught: a transient lock read error becomes an empty lock."""
        self._skill("managed-by-wildcard")
        self._lock({"managed-by-wildcard": "owner/repo"})
        config = self._manifest(self.BASE)
        real_read_text = Path.read_text

        def fail_lock_read(path: Path, *args, **kwargs) -> str:
            if path == self.paths.global_skill_lock:
                raise PermissionError("lock is unreadable")
            return real_read_text(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", autospec=True, side_effect=fail_lock_read):
            report = plan_prune(self.paths, config)

        self.assertEqual((), report.candidates)
        self.assertIn("lock is unreadable", report.blocked_reason or "")

    def test_invalid_lock_container_types_block_planning(self):
        """Break caught: invalid root or skills containers become an empty lock."""
        self._skill("managed-by-wildcard")
        self.paths.global_skill_lock.parent.mkdir(parents=True, exist_ok=True)
        config = self._manifest(self.BASE)

        for payload in ([], {"version": 3, "skills": []}):
            with self.subTest(payload=payload):
                self.paths.global_skill_lock.write_text(json.dumps(payload), encoding="utf-8")
                report = plan_prune(self.paths, config)
                self.assertEqual((), report.candidates)
                self.assertIn("invalid global skill lock", report.blocked_reason or "")

    def test_a_blocked_plan_cannot_be_applied(self):
        """Break caught: an invalid-lock plan is converted into a successful no-op."""
        skill = self._skill("managed-by-wildcard")
        self.paths.global_skill_lock.parent.mkdir(parents=True, exist_ok=True)
        self.paths.global_skill_lock.write_text("{invalid", encoding="utf-8")
        report = plan_prune(self.paths, self._manifest(self.BASE))

        with self.assertRaisesRegex(ValueError, "invalid global skill lock"):
            apply_prune(self.paths, report)

        self.assertTrue(skill.is_dir())
        self.assertEqual("{invalid", self.paths.global_skill_lock.read_text(encoding="utf-8"))

    # --- removal --------------------------------------------------------
    def test_apply_removes_directory_lock_entry_and_both_bridges(self):
        self._skill("alpha")
        self._skill("keeper")
        self._lock({"alpha": "owner/repo", "keeper": "owner/repo"})
        claude_link, codex_link = self._bridge("alpha")
        keeper_claude, _ = self._bridge("keeper")
        config = self._manifest(self.BASE + "    exclude:\n      - alpha\n")

        result = apply_prune(self.paths, plan_prune(self.paths, config))

        self.assertEqual(("alpha",), result.removed)
        self.assertFalse((self.store / "alpha").exists())
        self.assertFalse(claude_link.is_symlink())
        self.assertFalse(codex_link.is_symlink())
        # the untouched skill survives, links and all
        self.assertTrue((self.store / "keeper").is_dir())
        self.assertTrue(keeper_claude.is_symlink())
        lock = json.loads(self.paths.global_skill_lock.read_text(encoding="utf-8"))
        self.assertNotIn("alpha", lock["skills"])
        self.assertIn("keeper", lock["skills"])

    def test_lock_replace_failure_restores_skill_and_bridges(self):
        """Break caught: a failed lock commit leaves the skill store pruned."""
        skill = self._skill("alpha")
        self._lock({"alpha": "owner/repo"})
        original_lock = self.paths.global_skill_lock.read_bytes()
        claude_link, codex_link = self._bridge("alpha")
        config = self._manifest(self.BASE + "    exclude:\n      - alpha\n")

        with (
            mock.patch("os.replace", side_effect=OSError("replace failed")),
            self.assertRaisesRegex(OSError, "replace failed"),
        ):
            apply_prune(self.paths, plan_prune(self.paths, config))

        self.assertTrue(skill.is_dir())
        self.assertTrue(claude_link.is_symlink())
        self.assertTrue(codex_link.is_symlink())
        self.assertEqual(original_lock, self.paths.global_skill_lock.read_bytes())

    def test_directory_staging_failure_rolls_back_without_temporary_paths(self):
        """Break caught: staging failure leaks backups after restoring bridges."""
        skill = self._skill("alpha")
        self._lock({"alpha": "owner/repo"})
        original_lock = self.paths.global_skill_lock.read_bytes()
        claude_link, codex_link = self._bridge("alpha")
        config = self._manifest(self.BASE + "    exclude:\n      - alpha\n")
        real_rename = Path.rename

        def fail_skill_stage(path: Path, target: Path) -> Path:
            if path == skill:
                raise OSError("directory staging failed")
            return real_rename(path, target)

        with (
            mock.patch.object(Path, "rename", autospec=True, side_effect=fail_skill_stage),
            self.assertRaisesRegex(OSError, "directory staging failed"),
        ):
            apply_prune(self.paths, plan_prune(self.paths, config))

        self.assertTrue(skill.is_dir())
        self.assertTrue(claude_link.is_symlink())
        self.assertTrue(codex_link.is_symlink())
        self.assertEqual(original_lock, self.paths.global_skill_lock.read_bytes())
        self.assertEqual([], list(self.home.rglob(".agentbot-prune-*")))

    def test_invalid_lock_at_apply_time_aborts_before_staging(self):
        """Break caught: a lock changed after planning is treated as absent."""
        skill = self._skill("alpha")
        self._lock({"alpha": "owner/repo"})
        claude_link, codex_link = self._bridge("alpha")
        config = self._manifest(self.BASE + "    exclude:\n      - alpha\n")
        report = plan_prune(self.paths, config)
        self.paths.global_skill_lock.write_text("{invalid", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "invalid skill lock"):
            apply_prune(self.paths, report)

        self.assertTrue(skill.is_dir())
        self.assertTrue(claude_link.is_symlink())
        self.assertTrue(codex_link.is_symlink())
        self.assertEqual("{invalid", self.paths.global_skill_lock.read_text(encoding="utf-8"))
        self.assertEqual([], list(self.home.rglob(".agentbot-prune-*")))

    def test_missing_lock_at_apply_time_aborts_for_a_locked_candidate(self):
        """Break caught: a vanished lock is accepted after a locked prune plan."""
        skill = self._skill("alpha")
        self._lock({"alpha": "owner/repo"})
        claude_link, codex_link = self._bridge("alpha")
        config = self._manifest(self.BASE + "    exclude:\n      - alpha\n")
        report = plan_prune(self.paths, config)
        self.paths.global_skill_lock.unlink()

        with self.assertRaisesRegex(ValueError, "skill lock disappeared"):
            apply_prune(self.paths, report)

        self.assertTrue(skill.is_dir())
        self.assertTrue(claude_link.is_symlink())
        self.assertTrue(codex_link.is_symlink())
        self.assertEqual([], list(self.home.rglob(".agentbot-prune-*")))

    def test_lock_temporary_write_failure_does_not_stage_any_paths(self):
        """Break caught: lock preflight failure starts mutating the skill store."""
        skill = self._skill("alpha")
        self._lock({"alpha": "owner/repo"})
        original_lock = self.paths.global_skill_lock.read_bytes()
        claude_link, codex_link = self._bridge("alpha")
        config = self._manifest(self.BASE + "    exclude:\n      - alpha\n")

        with (
            mock.patch("os.fsync", side_effect=OSError("temporary write failed")),
            self.assertRaisesRegex(OSError, "temporary write failed"),
        ):
            apply_prune(self.paths, plan_prune(self.paths, config))

        self.assertTrue(skill.is_dir())
        self.assertTrue(claude_link.is_symlink())
        self.assertTrue(codex_link.is_symlink())
        self.assertEqual(original_lock, self.paths.global_skill_lock.read_bytes())
        self.assertEqual([], list(self.paths.global_skill_lock.parent.glob(".*.json.*")))

    def test_bridge_staging_failure_restores_an_already_staged_bridge(self):
        """Break caught: one bridge-stage failure strands the other bridge."""
        skill = self._skill("alpha")
        self._lock({"alpha": "owner/repo"})
        original_lock = self.paths.global_skill_lock.read_bytes()
        claude_link, codex_link = self._bridge("alpha")
        config = self._manifest(self.BASE + "    exclude:\n      - alpha\n")
        real_rename = Path.rename

        def fail_codex_bridge(path: Path, target: Path) -> Path:
            if path == codex_link:
                raise OSError("bridge staging failed")
            return real_rename(path, target)

        with (
            mock.patch.object(Path, "rename", autospec=True, side_effect=fail_codex_bridge),
            self.assertRaisesRegex(OSError, "bridge staging failed"),
        ):
            apply_prune(self.paths, plan_prune(self.paths, config))

        self.assertTrue(skill.is_dir())
        self.assertTrue(claude_link.is_symlink())
        self.assertTrue(codex_link.is_symlink())
        self.assertEqual(original_lock, self.paths.global_skill_lock.read_bytes())
        self.assertEqual([], list(self.home.rglob(".agentbot-prune-*")))

    def test_apply_leaves_manual_skills_unless_asked(self):
        self._skill("graphify")
        self._lock({})
        config = self._manifest(self.BASE)

        result = apply_prune(self.paths, plan_prune(self.paths, config))
        self.assertEqual((), result.removed)
        self.assertTrue((self.store / "graphify").is_dir())

        result = apply_prune(self.paths, plan_prune(self.paths, config), include_manual=True)
        self.assertEqual(("graphify",), result.removed)
        self.assertFalse((self.store / "graphify").exists())

    def test_apply_removes_only_named_manual_skills(self):
        self._skill("remove-me")
        self._skill("keep-me")
        remove_claude, remove_codex = self._bridge("remove-me")
        keep_claude, keep_codex = self._bridge("keep-me")
        self._lock({})
        config = self._manifest(self.BASE)

        result = apply_prune(
            self.paths,
            plan_prune(self.paths, config),
            manual_names=("remove-me",),
        )

        self.assertEqual(("remove-me",), result.removed)
        self.assertFalse((self.store / "remove-me").exists())
        self.assertFalse(remove_claude.is_symlink())
        self.assertFalse(remove_codex.is_symlink())
        self.assertTrue((self.store / "keep-me").is_dir())
        self.assertTrue(keep_claude.is_symlink())
        self.assertTrue(keep_codex.is_symlink())

    def test_apply_removes_only_selected_candidates_across_reasons(self):
        """Break caught: selective pruning can remove only manual directories."""
        self._skill("manual")
        self._skill("orphaned")
        self._skill("keeper")
        self._lock({"orphaned": "gone/repo", "keeper": "owner/repo"})
        manual_claude, manual_codex = self._bridge("manual")
        orphaned_claude, orphaned_codex = self._bridge("orphaned")
        config = self._manifest(self.BASE)

        result = apply_prune(
            self.paths,
            plan_prune(self.paths, config),
            candidate_names=("manual", "orphaned"),
        )

        self.assertEqual(("manual", "orphaned"), result.removed)
        self.assertFalse(manual_claude.is_symlink())
        self.assertFalse(manual_codex.is_symlink())
        self.assertFalse(orphaned_claude.is_symlink())
        self.assertFalse(orphaned_codex.is_symlink())
        self.assertTrue((self.store / "keeper").is_dir())

    def test_apply_rejects_a_selected_managed_skill(self):
        """Break caught: a forged checkbox name removes an active managed skill."""
        self._skill("managed")
        self._lock({"managed": "owner/repo"})
        report = plan_prune(self.paths, self._manifest(self.BASE))

        with self.assertRaisesRegex(ValueError, "not prune candidates: managed"):
            apply_prune(self.paths, report, candidate_names=("managed",))

        self.assertTrue((self.store / "managed").is_dir())

    def test_apply_rejects_a_requested_name_that_is_not_manual(self):
        self._skill("managed")
        self._lock({"managed": "owner/repo"})
        report = plan_prune(self.paths, self._manifest(self.BASE))

        with self.assertRaisesRegex(ValueError, "not removable manual skills: managed"):
            apply_prune(self.paths, report, manual_names=("managed",))

        self.assertTrue((self.store / "managed").is_dir())

    def test_a_bridge_link_pointing_outside_our_store_is_never_removed(self):
        # A link the user or another installer owns must survive, even when the
        # skill name matches something we are pruning.
        self._skill("alpha")
        self._lock({"alpha": "owner/repo"})
        foreign = self.root / "elsewhere" / "alpha"
        foreign.mkdir(parents=True)
        claude = self.paths.claude_skills_home
        claude.mkdir(parents=True, exist_ok=True)
        link = claude / "alpha"
        link.symlink_to(foreign)
        config = self._manifest(self.BASE + "    exclude:\n      - alpha\n")

        apply_prune(self.paths, plan_prune(self.paths, config))

        self.assertTrue(link.is_symlink())
        self.assertEqual(foreign, link.resolve())

    def test_planning_writes_nothing(self):
        self._skill("alpha")
        self._lock({"alpha": "owner/repo"})
        config = self._manifest(self.BASE + "    exclude:\n      - alpha\n")
        before = self.paths.global_skill_lock.read_text(encoding="utf-8")

        plan_prune(self.paths, config)

        self.assertTrue((self.store / "alpha").is_dir())
        self.assertEqual(before, self.paths.global_skill_lock.read_text(encoding="utf-8"))


class ManifestExcludeTests(unittest.TestCase):
    def test_exclude_must_name_something_the_source_installs(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "skills.sources.yaml"
            path.write_text(
                "version: 1\nagents: [claude-code]\nscope: global\nsources:\n"
                "  - id: owner\n    repo: owner/repo\n    skills:\n      - real\n"
                "    exclude:\n      - typo\n",
                encoding="utf-8",
            )
            with self.assertRaises(Exception) as caught:
                load_skills_sources(path)
            self.assertIn("does not install", str(caught.exception))


class EnforceExclusionsTests(PruneTests):
    """`exclude:` must mean "never present", not "prunable later"."""

    def test_install_time_enforcement_removes_excluded_skills(self):
        from src.skill_prune import enforce_exclusions

        self._skill("alpha")
        self._skill("keeper")
        self._lock({"alpha": "owner/repo", "keeper": "owner/repo"})
        claude_link, codex_link = self._bridge("alpha")
        config = self._manifest(self.BASE + "    exclude:\n      - alpha\n")

        removed = enforce_exclusions(self.paths, config)

        self.assertEqual(("alpha",), removed)
        self.assertFalse((self.store / "alpha").exists())
        self.assertFalse(claude_link.is_symlink())
        self.assertFalse(codex_link.is_symlink())
        self.assertTrue((self.store / "keeper").is_dir())

    def test_enforcement_leaves_orphans_and_manual_skills_alone(self):
        # Only `excluded` is enforced at install time. Removing an orphan or a
        # user-placed skill mid-install would be a surprise; that stays an
        # explicit `skills prune`.
        from src.skill_prune import enforce_exclusions

        self._skill("leftover")
        self._skill("graphify")
        self._lock({"leftover": "gone/repo"})
        config = self._manifest(self.BASE)

        self.assertEqual((), enforce_exclusions(self.paths, config))
        self.assertTrue((self.store / "leftover").is_dir())
        self.assertTrue((self.store / "graphify").is_dir())

    def test_enforcement_is_a_no_op_when_nothing_is_excluded(self):
        from src.skill_prune import enforce_exclusions

        self._skill("keeper")
        self._lock({"keeper": "owner/repo"})
        config = self._manifest(self.BASE)

        self.assertEqual((), enforce_exclusions(self.paths, config))
        self.assertTrue((self.store / "keeper").is_dir())

    def test_reinstalling_does_not_resurrect_an_excluded_skill(self):
        # The treadmill: install re-adds and re-pins, so enforcement has to run
        # every time, not once.
        from src.skill_prune import enforce_exclusions

        config = self._manifest(self.BASE + "    exclude:\n      - alpha\n")
        for _ in range(2):
            self._skill("alpha")  # stand in for `skills add` re-adding it
            self._lock({"alpha": "owner/repo"})
            enforce_exclusions(self.paths, config)
            self.assertFalse((self.store / "alpha").exists())
            lock = json.loads(self.paths.global_skill_lock.read_text(encoding="utf-8"))
            self.assertNotIn("alpha", lock["skills"])


if __name__ == "__main__":
    unittest.main()
