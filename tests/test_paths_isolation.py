"""The real user home must never be reachable from injected paths.

Test fixture skills once ended up in the user's real ~/.agents store, pinned in
the real lock, and symlinked into ~/.claude/skills, because two path properties
were computed from Path.home() instead of being injectable. These tests pin the
injection contract so that cannot recur.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.paths import AgentbotPaths, default_paths


def _sandboxed(root: Path) -> AgentbotPaths:
    return AgentbotPaths(
        root=root,
        codex_home=root / ".codex",
        claude_home=root / ".claude",
        cursor_home=root / ".cursor",
        config_home=root / ".config" / "agentbot",
        agents_home=root / ".agents",
    )


class PathConstructionGuardTests(unittest.TestCase):
    """Tests must not build AgentbotPaths positionally.

    Break caught: `AgentbotPaths(root, root / "codex", ...)` looks sandboxed but
    leaves `config_home` and `agents_home` at their real-home defaults, so the
    suite read and wrote the operator's own ~/.agents skill lock. That stayed
    invisible while nothing in the covered path wrote the lock, and surfaced the
    moment `Lifecycle.install()` began migrating renamed source pins.
    """

    def test_no_test_builds_agentbot_paths_without_the_injected_homes(self) -> None:
        import ast

        tests_root = Path(__file__).resolve().parent
        offenders = []
        for module in sorted(tests_root.rglob("*.py")):
            if module.name in {"support.py", Path(__file__).name}:
                continue
            tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = node.func.id if isinstance(node.func, ast.Name) else None
                if name != "AgentbotPaths":
                    continue
                keywords = {keyword.arg for keyword in node.keywords}
                positional = len(node.args)
                # config_home is the fifth positional parameter and agents_home
                # the sixth, so a long positional call is injected too.
                if ("config_home" in keywords or positional >= 5) and (
                    "agents_home" in keywords or positional >= 6
                ):
                    continue
                offenders.append(f"{module.name}:{node.lineno}")
        self.assertEqual(
            [],
            offenders,
            "use tests.support.agentbot_paths(root), or pass agents_home and "
            "config_home explicitly",
        )


class PathIsolationTests(unittest.TestCase):
    def test_every_managed_path_stays_inside_an_injected_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = _sandboxed(root)
            managed = {
                "global_agents": paths.global_agents,
                "skills_sources_file": paths.skills_sources_file,
                "skills_lock_file": paths.skills_lock_file,
                "agents_skills_home": paths.agents_skills_home,
                "claude_skills_home": paths.claude_skills_home,
                "global_skill_lock": paths.global_skill_lock,
                "workspace_profiles_file": paths.workspace_profiles_file,
                "workspace_state_file": paths.workspace_state_file,
                "mcp_catalog_file": paths.mcp_catalog_file,
                "mcp_state_file": paths.mcp_state_file,
                "mcp_backup_home": paths.mcp_backup_home,
            }
            for name, value in managed.items():
                self.assertTrue(
                    str(value).startswith(str(root)),
                    f"{name} escaped the injected root: {value}",
                )

    def test_agents_home_is_injectable_not_derived_from_the_process_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = _sandboxed(root)
            self.assertEqual(root / ".agents" / "skills", paths.agents_skills_home)
            self.assertEqual(root / ".agents" / ".skill-lock.json", paths.global_skill_lock)

    def test_positional_construction_still_binds_config_home_fifth(self):
        # agents_home was added after config_home on purpose: inserting it
        # earlier silently rebound the fifth positional argument.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "cfg"
            paths = AgentbotPaths(root, root / "c", root / "cl", root / "cu", config)
            self.assertEqual(config, paths.config_home)

    def test_default_paths_still_resolves_the_real_home(self):
        # The production default must keep pointing at the user's home; only
        # explicit construction sandboxes it.
        paths = default_paths(Path("/tmp/does-not-matter"))
        self.assertEqual(Path.home() / ".agents", paths.agents_home)


if __name__ == "__main__":
    unittest.main()
