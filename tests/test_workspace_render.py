from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.workspace_render import (
    GENERATED_HEADER,
    MANAGED_BEGIN,
    MANAGED_END,
    apply_workspace_render_plan,
    build_workspace_render_plan,
)


class WorkspaceRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.base_agents = (
            "# AGENTS.md\n\n"
            "## Environment\n\n"
            "- Shared environment.\n\n"
            "## Project\n\n"
            "<!-- Fill in project policy. -->\n"
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_missing_files_plan_the_selected_default_outputs(self) -> None:
        plan = build_workspace_render_plan(
            self.base_agents,
            {},
            ("agents", "claude", "cursor"),
        )

        self.assertEqual(
            (
                "AGENTS.md",
                "CLAUDE.md",
                ".cursor/rules/agentbot-policy.mdc",
            ),
            plan.paths(),
        )
        self.assertTrue(all(action.kind == "create" for action in plan.actions))
        self.assertEqual("managed", plan.policy_mode)

    def test_resync_replaces_only_the_managed_region(self) -> None:
        agents = (
            "custom heading\n"
            f"{MANAGED_BEGIN}\nold base\n{MANAGED_END}\n\n"
            "## Project\nKeep this project rule.\n"
        )
        newer_base = self.base_agents.replace("Shared environment.", "new shared base")

        plan = build_workspace_render_plan(newer_base, {"AGENTS.md": agents}, ("agents",))
        action = plan.actions[0]

        self.assertEqual("update", action.kind)
        self.assertIn("new shared base", action.content or "")
        self.assertIn("custom heading\n", action.content or "")
        self.assertIn("Keep this project rule.", action.content or "")
        self.assertNotIn("old base", action.content or "")

    def test_unmarked_custom_agents_file_is_preserved(self) -> None:
        plan = build_workspace_render_plan(
            self.base_agents,
            {"AGENTS.md": "# My policy\n"},
            ("claude",),
        )

        self.assertEqual("custom", plan.policy_mode)
        self.assertEqual("unchanged", plan.action_for("AGENTS.md").kind)
        self.assertEqual("create", plan.action_for("CLAUDE.md").kind)
        self.assertEqual("# My policy\n", plan.action_for("AGENTS.md").content)

    def test_unmarked_custom_agents_file_gets_a_review_template(self) -> None:
        plan = build_workspace_render_plan(
            self.base_agents,
            {"AGENTS.md": "# My policy\n"},
            ("agents",),
        )

        review = plan.action_for("AGENTS_temp.md")

        self.assertEqual("create", review.kind)
        self.assertIn("sha256=", review.content or "")
        self.assertIn("# AGENTS.md", review.content or "")
        self.assertIn("## Project", review.content or "")

    def test_unowned_compatibility_file_gets_a_review_template(self) -> None:
        plan = build_workspace_render_plan(
            self.base_agents,
            {"CLAUDE.md": "# User-owned instructions\n"},
            ("claude",),
        )

        original = plan.action_for("CLAUDE.md")
        review = plan.action_for("CLAUDE_temp.md")

        self.assertEqual("unchanged", original.kind)
        self.assertEqual("# User-owned instructions\n", original.content)
        self.assertEqual("create", review.kind)
        self.assertIn(GENERATED_HEADER, review.content or "")
        self.assertIn("sha256=", review.content or "")

    def test_each_compatibility_target_uses_a_sibling_review_name(self) -> None:
        targets = (
            ("claude", "CLAUDE.md", "CLAUDE_temp.md"),
            (
                "cursor",
                ".cursor/rules/agentbot-policy.mdc",
                ".cursor/rules/agentbot-policy_temp.mdc",
            ),
        )

        for target, relative_path, review_path in targets:
            with self.subTest(target=target):
                plan = build_workspace_render_plan(
                    self.base_agents,
                    {relative_path: "# User-owned instructions\n"},
                    (target,),
                )

                self.assertEqual("unchanged", plan.action_for(relative_path).kind)
                self.assertEqual("create", plan.action_for(review_path).kind)

    def test_every_custom_selection_still_plans_agents(self) -> None:
        plan = build_workspace_render_plan(self.base_agents, {}, ("cursor",))

        self.assertEqual(
            ("AGENTS.md", ".cursor/rules/agentbot-policy.mdc"),
            plan.paths(),
        )

    def test_legacy_phase_one_agents_are_migrated(self) -> None:
        legacy = self.base_agents.replace(
            "<!-- Fill in project policy. -->",
            "Keep this legacy project policy.",
        )

        plan = build_workspace_render_plan(self.base_agents, {"AGENTS.md": legacy}, ("agents",))
        content = plan.action_for("AGENTS.md").content or ""

        self.assertEqual("managed", plan.policy_mode)
        self.assertEqual("update", plan.action_for("AGENTS.md").kind)
        self.assertIn(MANAGED_BEGIN, content)
        self.assertIn("Keep this legacy project policy.", content)

    def test_existing_unowned_compatibility_file_is_preserved(self) -> None:
        plan = build_workspace_render_plan(
            self.base_agents,
            {"CLAUDE.md": "# User-owned instructions\n"},
            ("claude",),
        )

        self.assertEqual("unchanged", plan.action_for("CLAUDE.md").kind)
        self.assertEqual("create", plan.action_for("CLAUDE_temp.md").kind)

    def test_untouched_review_template_is_refreshed_in_place(self) -> None:
        first = build_workspace_render_plan(
            self.base_agents,
            {"AGENTS.md": "# My policy\n"},
            ("agents",),
        )
        old_review = first.action_for("AGENTS_temp.md").content or ""
        newer_base = self.base_agents.replace("Shared environment.", "new shared base")

        second = build_workspace_render_plan(
            newer_base,
            {
                "AGENTS.md": "# My policy\n",
                "AGENTS_temp.md": old_review,
            },
            ("agents",),
        )

        review = second.action_for("AGENTS_temp.md")
        self.assertEqual("update", review.kind)
        self.assertIn("new shared base", review.content or "")
        self.assertNotIn("AGENTS_temp_1.md", second.paths())

    def test_edited_stale_review_template_rolls_to_next_suffix(self) -> None:
        first = build_workspace_render_plan(
            self.base_agents,
            {"AGENTS.md": "# My policy\n"},
            ("agents",),
        )
        old_review = (first.action_for("AGENTS_temp.md").content or "") + "\nMy notes.\n"
        newer_base = self.base_agents.replace("Shared environment.", "new shared base")

        second = build_workspace_render_plan(
            newer_base,
            {
                "AGENTS.md": "# My policy\n",
                "AGENTS_temp.md": old_review,
            },
            ("agents",),
        )

        self.assertNotIn("AGENTS_temp.md", second.paths())
        review = second.action_for("AGENTS_temp_1.md")
        self.assertEqual("create", review.kind)
        self.assertIn("new shared base", review.content or "")

    def test_edited_current_review_template_is_not_duplicated(self) -> None:
        first = build_workspace_render_plan(
            self.base_agents,
            {"AGENTS.md": "# My policy\n"},
            ("agents",),
        )
        edited_review = (first.action_for("AGENTS_temp.md").content or "") + "\nMy notes.\n"

        second = build_workspace_render_plan(
            self.base_agents,
            {
                "AGENTS.md": "# My policy\n",
                "AGENTS_temp.md": edited_review,
            },
            ("agents",),
        )

        self.assertEqual("unchanged", second.action_for("AGENTS_temp.md").kind)
        self.assertNotIn("AGENTS_temp_1.md", second.paths())

    def test_copied_agents_review_template_is_migrated(self) -> None:
        first = build_workspace_render_plan(
            self.base_agents,
            {"AGENTS.md": "# My policy\n"},
            ("agents",),
        )
        copied = first.action_for("AGENTS_temp.md").content or ""

        second = build_workspace_render_plan(
            self.base_agents,
            {"AGENTS.md": copied},
            ("agents",),
        )

        self.assertEqual("managed", second.policy_mode)
        self.assertEqual("update", second.action_for("AGENTS.md").kind)
        self.assertIn(MANAGED_BEGIN, second.action_for("AGENTS.md").content or "")

    def test_malformed_agents_markers_get_a_review_template(self) -> None:
        malformed = f"{MANAGED_BEGIN}\n# incomplete\n"

        plan = build_workspace_render_plan(
            self.base_agents,
            {"AGENTS.md": malformed},
            ("agents",),
        )

        self.assertEqual("unchanged", plan.action_for("AGENTS.md").kind)
        self.assertEqual("create", plan.action_for("AGENTS_temp.md").kind)

    def test_legacy_claude_template_is_migrated_as_agentbot_owned(self) -> None:
        legacy = (
            "IMPORTANT: Read and follow all instructions in AGENTS.md before starting any task.\n\n"
            "@AGENTS.md\n"
        )

        plan = build_workspace_render_plan(self.base_agents, {"CLAUDE.md": legacy}, ("claude",))

        self.assertEqual("update", plan.action_for("CLAUDE.md").kind)
        self.assertIn(GENERATED_HEADER, plan.action_for("CLAUDE.md").content or "")

    def test_second_plan_after_apply_has_no_changes(self) -> None:
        first = build_workspace_render_plan(
            self.base_agents,
            {},
            ("agents", "claude", "cursor"),
        )
        apply_workspace_render_plan(
            self.root,
            first,
        )
        current = {path: (self.root / path).read_text(encoding="utf-8") for path in first.paths()}

        second = build_workspace_render_plan(
            self.base_agents,
            current,
            ("agents", "claude", "cursor"),
        )

        self.assertTrue(all(action.kind == "unchanged" for action in second.actions))

    def test_path_traversal_is_rejected_before_rendering(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported workspace target"):
            build_workspace_render_plan(self.base_agents, {}, ("../AGENTS.md",))

    def test_cursor_rule_points_at_agents_md_instead_of_copying_it(self) -> None:
        # Cursor applies .cursor/rules/, AGENTS.md, and CLAUDE.md together, so a
        # full copy here is the same policy loaded twice per request.
        from src.workspace_render import render_cursor

        rule = render_cursor()

        self.assertIn("AGENTS.md", rule)
        self.assertIn("alwaysApply: true", rule)
        # The policy body must not appear. Sample distinctive lines from the
        # base template rather than trusting a length check.
        for marker in ("## Safety floor", "## Definition of done", "## Repository workflow"):
            self.assertNotIn(marker, rule)
        self.assertNotIn(MANAGED_BEGIN, rule)

    def test_cursor_rule_stays_small(self) -> None:
        # An absolute cap, not a ratio against the fixture: self.base_agents is
        # a short stub, whereas the real policy is several kilobytes. The point
        # is that this file never grows into a second copy of anything.
        from src.workspace_render import render_cursor

        self.assertLess(len(render_cursor()), 1024)

    def test_claude_and_cursor_adapters_both_point_rather_than_duplicate(self) -> None:
        # The two hosts that read multiple surfaces use the same shape.
        from src.workspace_render import render_claude, render_cursor

        for rendered in (render_claude(), render_cursor()):
            self.assertIn("AGENTS.md", rendered)
            self.assertNotIn("## Safety floor", rendered)


if __name__ == "__main__":
    unittest.main()
