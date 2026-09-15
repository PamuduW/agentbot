import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class ReconcileApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "global").mkdir()
        (self.root / "base").mkdir()
        (self.root / "global" / "AGENTS.md").write_text("# global\n", encoding="utf-8")
        (self.root / "base" / "AGENTS.md").write_text(
            "| `gone` | old |\n| `keep` | keep |\n", encoding="utf-8"
        )
        (self.root / "AGENTS.md").write_text(
            "| `gone` | old |\n| `keep` | keep |\n", encoding="utf-8"
        )
        (self.root / "skills.sources.yaml").write_text(
            "version: 1\nagents: [codex]\nscope: global\nsources:\n"
            "  - id: explicit\n    repo: owner/repo\n    skills: [gone, keep]\n"
            "  - id: wildcard\n    repo: owner/all\n    skills: all\n",
            encoding="utf-8",
        )
        self.home = self.root / "home"
        self.agents = self.home / ".agents" / "skills"
        self.codex = self.home / ".codex"
        self.claude = self.home / ".claude"
        self.agents.mkdir(parents=True)
        self._skill("gone")
        self._skill("keep")
        self._skill("manual")
        self.lock = self.home / ".agents" / ".skill-lock.json"
        self.lock.write_text(
            json.dumps(
                {
                    "version": 3,
                    "skills": {
                        "gone": {"source": "owner/repo"},
                        "keep": {"source": "owner/repo"},
                        "manual": {"source": "manual/repo"},
                        "old": {"source": "owner/all"},
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _skill(self, name: str) -> Path:
        path = self.agents / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
        return path

    def _paths(self):
        from src.paths import AgentbotPaths

        paths = AgentbotPaths(
            root=self.root,
            codex_home=self.codex,
            claude_home=self.claude,
            cursor_home=self.root / "cursor",
            config_home=self.root / ".config" / "agentbot",
            agents_home=self.root / ".agents",
        )
        patches = (
            mock.patch.object(
                type(paths),
                "agents_skills_home",
                new_callable=lambda: property(lambda _self: self.agents),
            ),
            mock.patch.object(
                type(paths),
                "global_skill_lock",
                new_callable=lambda: property(lambda _self: self.lock),
            ),
        )
        return paths, patches

    def test_wildcard_apply_mirrors_owned_catalog_and_preserves_manual(self) -> None:
        from src.skill_reconcile import apply_reconcile_plan, build_reconcile_plan
        from src.skills_sources import load_skills_sources

        checkout = self.root / "checkout"
        self._skill_from(checkout, "new")
        self._skill_from(checkout, "keep-all")
        config = load_skills_sources(self.root / "skills.sources.yaml")
        plan = build_reconcile_plan(
            config,
            discovered={"explicit": ("gone", "keep"), "wildcard": ("new", "keep-all")},
            lock=json.loads(self.lock.read_text(encoding="utf-8")),
        )
        paths, patches = self._paths()
        with patches[0], patches[1]:
            result = apply_reconcile_plan(
                paths, config, plan, checkouts={"wildcard": checkout}, confirm=True
            )
        self.assertEqual("applied", result.status)
        self.assertTrue((self.agents / "new").is_dir())
        self.assertFalse((self.agents / "old").exists())
        self.assertTrue((self.agents / "manual").exists())
        lock = json.loads(self.lock.read_text(encoding="utf-8"))["skills"]
        self.assertEqual("owner/all", lock["new"]["source"])
        self.assertNotIn("old", lock)

    def test_wildcard_apply_removes_excluded_skill_without_readding_it(self) -> None:
        """Break caught: reconcile removes an excluded skill and copies it back from checkout."""
        from src.skill_reconcile import apply_reconcile_plan, build_reconcile_plan
        from src.skills_sources import load_skills_sources

        self._skill("alpha")
        lock = json.loads(self.lock.read_text(encoding="utf-8"))
        lock["skills"]["alpha"] = {"source": "owner/all"}
        self.lock.write_text(json.dumps(lock), encoding="utf-8")
        manifest = self.root / "skills.sources.yaml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + "    exclude:\n      - alpha\n",
            encoding="utf-8",
        )
        checkout = self.root / "excluded-checkout"
        self._skill_from(checkout, "alpha")
        config = load_skills_sources(manifest)
        plan = build_reconcile_plan(
            config,
            discovered={"explicit": ("gone", "keep"), "wildcard": ("alpha",)},
            lock=lock,
        )

        self.assertEqual((), plan.wildcard_additions)
        self.assertEqual(("alpha", "old"), plan.wildcard_removals)
        paths, patches = self._paths()
        with patches[0], patches[1]:
            result = apply_reconcile_plan(
                paths,
                config,
                plan,
                checkouts={"wildcard": checkout},
                confirm=True,
            )

        self.assertEqual(("alpha", "old"), result.removed_skills)
        self.assertEqual((), result.added_skills)
        self.assertFalse((self.agents / "alpha").exists())
        self.assertNotIn(
            "alpha",
            json.loads(self.lock.read_text(encoding="utf-8"))["skills"],
        )

    def _skill_from(self, checkout: Path, name: str) -> None:
        path = checkout / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")

    def test_explicit_removal_updates_manifest_and_canonical_tables(self) -> None:
        from src.skill_reconcile import apply_reconcile_plan, build_reconcile_plan
        from src.skills_sources import load_skills_sources

        config = load_skills_sources(self.root / "skills.sources.yaml")
        plan = build_reconcile_plan(
            config,
            discovered={"explicit": ("keep",), "wildcard": ()},
            lock=json.loads(self.lock.read_text(encoding="utf-8")),
        )
        paths, patches = self._paths()
        with patches[0], patches[1]:
            result = apply_reconcile_plan(paths, config, plan, confirm=True)
        self.assertEqual("applied", result.status)
        self.assertFalse((self.agents / "gone").exists())
        self.assertNotIn("`gone`", (self.root / "base" / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertNotIn("gone", (self.root / "skills.sources.yaml").read_text(encoding="utf-8"))

    def test_explicit_removal_succeeds_without_canonical_skill_tables(self) -> None:
        from src.skill_reconcile import apply_reconcile_plan, build_reconcile_plan
        from src.skills_sources import load_skills_sources

        (self.root / "base" / "AGENTS.md").write_text("# canonical baseline\n", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("# repository policy\n", encoding="utf-8")
        config = load_skills_sources(self.root / "skills.sources.yaml")
        plan = build_reconcile_plan(
            config,
            discovered={"explicit": ("keep",), "wildcard": ()},
            lock=json.loads(self.lock.read_text(encoding="utf-8")),
        )
        paths, patches = self._paths()
        with patches[0], patches[1]:
            result = apply_reconcile_plan(paths, config, plan, confirm=True)

        self.assertEqual("applied", result.status)
        self.assertFalse((self.agents / "gone").exists())
        self.assertNotIn("gone", (self.root / "skills.sources.yaml").read_text(encoding="utf-8"))

    def test_explicit_removal_succeeds_when_canonical_table_lacks_skill(self) -> None:
        from src.skill_reconcile import apply_reconcile_plan, build_reconcile_plan
        from src.skills_sources import load_skills_sources

        (self.root / "base" / "AGENTS.md").write_text(
            "| `gone` | old |\n| `keep` | keep |\n", encoding="utf-8"
        )
        (self.root / "AGENTS.md").write_text("| `keep` | keep |\n", encoding="utf-8")
        config = load_skills_sources(self.root / "skills.sources.yaml")
        plan = build_reconcile_plan(
            config,
            discovered={"explicit": ("keep",), "wildcard": ()},
            lock=json.loads(self.lock.read_text(encoding="utf-8")),
        )
        paths, patches = self._paths()
        with patches[0], patches[1]:
            result = apply_reconcile_plan(paths, config, plan, confirm=True)

        self.assertEqual("applied", result.status)
        self.assertFalse((self.agents / "gone").exists())
        self.assertNotIn("`gone`", (self.root / "base" / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertNotIn("gone", (self.root / "skills.sources.yaml").read_text(encoding="utf-8"))

    def test_unconfirmed_plan_is_preview_only(self) -> None:
        from src.skill_reconcile import apply_reconcile_plan, build_reconcile_plan
        from src.skills_sources import load_skills_sources

        config = load_skills_sources(self.root / "skills.sources.yaml")
        plan = build_reconcile_plan(
            config, discovered={"explicit": ("gone",), "wildcard": ("new",)}, lock={"skills": {}}
        )
        paths, patches = self._paths()
        with patches[0], patches[1]:
            result = apply_reconcile_plan(paths, config, plan)
        self.assertEqual("confirmation_required", result.status)
        self.assertTrue((self.agents / "gone").exists())

    def test_noop_dry_run_reports_no_changed_paths(self) -> None:
        from src.skill_reconcile import apply_reconcile_plan, build_reconcile_plan
        from src.skills_sources import load_skills_sources

        config = load_skills_sources(self.root / "skills.sources.yaml")
        lock = json.loads(self.lock.read_text(encoding="utf-8"))
        plan = build_reconcile_plan(
            config,
            discovered={"explicit": ("gone", "keep"), "wildcard": ("old",)},
            lock=lock,
        )
        paths, patches = self._paths()
        with patches[0], patches[1]:
            result = apply_reconcile_plan(paths, config, plan, confirm=True, dry_run=True)

        self.assertEqual("preview", result.status)
        self.assertEqual((), result.changed_paths)

    def test_noop_apply_preserves_global_lock_bytes_and_mtime(self) -> None:
        from src.skill_reconcile import apply_reconcile_plan, build_reconcile_plan
        from src.skills_sources import load_skills_sources

        original = '{"version":3,"skills":{"gone":{"source":"owner/repo"},"keep":{"source":"owner/repo"},"manual":{"source":"manual/repo"},"old":{"source":"owner/all"}}}\n'
        self.lock.write_text(original, encoding="utf-8")
        os.utime(self.lock, (1_000_000_000, 1_000_000_000))
        before_mtime = self.lock.stat().st_mtime_ns
        config = load_skills_sources(self.root / "skills.sources.yaml")
        plan = build_reconcile_plan(
            config,
            discovered={"explicit": ("gone", "keep"), "wildcard": ("old",)},
            lock=json.loads(original),
        )
        paths, patches = self._paths()
        with patches[0], patches[1]:
            result = apply_reconcile_plan(paths, config, plan, confirm=True)

        self.assertEqual("applied", result.status)
        self.assertEqual((), result.changed_paths)
        self.assertEqual(original, self.lock.read_text(encoding="utf-8"))
        self.assertEqual(before_mtime, self.lock.stat().st_mtime_ns)

    def test_explicit_removal_tolerates_missing_canonical_row(self) -> None:
        from src.skill_reconcile import apply_reconcile_plan, build_reconcile_plan
        from src.skills_sources import load_skills_sources

        (self.root / "AGENTS.md").write_text("| `keep` | keep |\n", encoding="utf-8")
        config = load_skills_sources(self.root / "skills.sources.yaml")
        plan = build_reconcile_plan(
            config,
            discovered={"explicit": ("keep",), "wildcard": ()},
            lock=json.loads(self.lock.read_text(encoding="utf-8")),
        )
        paths, patches = self._paths()
        before = (self.root / "skills.sources.yaml").read_text(encoding="utf-8")
        with patches[0], patches[1]:
            result = apply_reconcile_plan(paths, config, plan, confirm=True)
        self.assertEqual("applied", result.status)
        self.assertFalse((self.agents / "gone").exists())
        self.assertEqual(
            "| `keep` | keep |\n", (self.root / "AGENTS.md").read_text(encoding="utf-8")
        )
        self.assertNotEqual(before, (self.root / "skills.sources.yaml").read_text(encoding="utf-8"))

    def test_failed_reconciliation_keeps_backup_outside_repository(self) -> None:
        from src.skill_reconcile import apply_reconcile_plan, build_reconcile_plan
        from src.skills_sources import load_skills_sources

        config = load_skills_sources(self.root / "skills.sources.yaml")
        plan = build_reconcile_plan(
            config,
            discovered={"explicit": ("keep",), "wildcard": ()},
            lock=json.loads(self.lock.read_text(encoding="utf-8")),
        )
        paths, patches = self._paths()

        def fail_validation() -> None:
            raise RuntimeError("test rollback")

        with patches[0], patches[1]:
            result = apply_reconcile_plan(
                paths, config, plan, confirm=True, validate=fail_validation
            )

        self.assertEqual("failed", result.status)
        self.assertIsNotNone(result.backup_path)
        self.assertNotIn(self.root, result.backup_path.parents)

    def test_failed_reconciliation_restores_valid_and_dangling_skill_links(self) -> None:
        from src.skill_reconcile import apply_reconcile_plan, build_reconcile_plan
        from src.skills_sources import load_skills_sources

        claude_skills = self.claude / "skills"
        claude_skills.mkdir(parents=True)
        valid_target = self.agents / "keep"
        dangling_target = self.agents / "future"
        (claude_skills / "keep").symlink_to(valid_target)
        (claude_skills / "future").symlink_to(dangling_target)
        config = load_skills_sources(self.root / "skills.sources.yaml")
        plan = build_reconcile_plan(
            config,
            discovered={"explicit": ("gone", "keep"), "wildcard": ("old",)},
            lock=json.loads(self.lock.read_text(encoding="utf-8")),
        )
        paths, patches = self._paths()
        backup = self.root / "reconcile-backup"
        backup.mkdir()

        def fail_validation() -> None:
            raise RuntimeError("test rollback")

        with (
            patches[0],
            patches[1],
            mock.patch("src.skill_reconcile.tempfile.mkdtemp", return_value=str(backup)),
        ):
            try:
                result = apply_reconcile_plan(
                    paths,
                    config,
                    plan,
                    confirm=True,
                    validate=fail_validation,
                    extra_affected=(claude_skills,),
                )
            except Exception as error:  # pragma: no cover - regression diagnostic
                self.fail(f"reconciliation snapshot raised instead of rolling back: {error}")

        self.assertEqual("failed", result.status)
        self.assertEqual("test rollback", result.message)
        self.assertTrue((claude_skills / "keep").is_symlink())
        self.assertEqual(valid_target, (claude_skills / "keep").readlink())
        self.assertTrue((claude_skills / "future").is_symlink())
        self.assertEqual(dangling_target, (claude_skills / "future").readlink())
        self.assertFalse((claude_skills / "future").exists())


if __name__ == "__main__":
    unittest.main()
