from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

from src.shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
from catalog_github import PREFIX
from software_catalog import CatalogError

from . import test_catalog_writer as writers


class MainGitHub(writers.WritableGitHub):
    def post(self, endpoint, payload):
        result = super().post(endpoint, payload)
        if endpoint == PREFIX + "/pulls" and payload["base"] == "main":
            pull = self.pulls[-1]
            pull["number"] = 20
            pull["html_url"] = "https://github.com/PamuduW/dotfiles-shared/pull/20"
            pull["base"]["sha"] = self.heads["shared-main"]
            result = copy.deepcopy(pull)
        return result


class MainPublicationTests(unittest.TestCase):
    setUp = writers.WriterTests.setUp
    write = writers.WriterTests.write
    example = writers.WriterTests.example
    set_receipt = writers.WriterTests.set_receipt
    fixture = writers.WriterTests.fixture
    submit = writers.WriterTests.submit

    def merged(self):
        original, identity, policy, _ = self.fixture()
        api = MainGitHub(original, policy["expected_heads"])
        submitted = self.submit(api, identity, policy)
        pull = api.pulls[0]
        pull.update(
            state="closed",
            merged=True,
            merged_at="2026-10-28T10:00:00Z",
            merge_commit_sha="e" * 40,
            user={"id": 9000, "type": "Bot"},
        )
        api.responses[f"{PREFIX}/pulls/19"] = lambda: api.pulls[0]
        published = api.responses[f"{PREFIX}/git/commits/{submitted['commit']}"]
        api.responses[f"{PREFIX}/git/commits/{'e' * 40}"] = {
            "sha": "e" * 40,
            "tree": copy.deepcopy(published["tree"]),
            "parents": [
                {"sha": policy["expected_heads"]["shared-development"]},
                {"sha": submitted["commit"]},
            ],
        }
        api.responses[f"{PREFIX}/git/ref/heads/development"]["object"]["sha"] = "e" * 40
        api.responses[
            f"{PREFIX}/compare/{policy['expected_heads']['shared-development']}...{'e' * 40}"
        ] = {
            "status": "ahead",
            "merge_base_commit": {"sha": policy["expected_heads"]["shared-development"]},
        }
        api.responses[f"{PREFIX}/pulls/19/reviews?per_page=100&page=1"] = [
            {
                "id": 1,
                "user": {"id": 1001, "login": "reviewer", "type": "User"},
                "state": "APPROVED",
                "commit_id": submitted["commit"],
                "submitted_at": "2026-10-28T09:00:00Z",
            }
        ]
        api.responses[f"{PREFIX}/collaborators/reviewer/permission"] = {
            "permission": "write",
            "user": {"id": 1001, "login": "reviewer"},
        }
        return api, identity, policy, submitted

    def main_submit(self, api, identity, policy):
        from catalog_main_publication import submit_main_publication

        return submit_main_publication(api, identity, development_pull_request=19, **policy)

    def test_reviewed_merge_submits_identical_head_to_main_without_rewriting_any_ref(self):
        api, identity, policy, submitted = self.merged()
        before = len(api.writes)
        result = self.main_submit(api, identity, policy)
        self.assertEqual("submitted-main", result["status"])
        self.assertFalse(result["published"])
        self.assertEqual(submitted["commit"], result["commit"])
        self.assertEqual(20, result["pull_request"]["number"])
        self.assertEqual([PREFIX + "/pulls"], [endpoint for endpoint, _ in api.writes[before:]])
        self.assertEqual("main", api.writes[-1][1]["base"])
        self.assertEqual(submitted["branch"], api.writes[-1][1]["head"])
        before = len(api.writes)
        self.assertEqual(result, self.main_submit(api, identity, policy))
        self.assertEqual(before, len(api.writes))

    def test_missing_current_human_review_and_changed_merge_hold_without_writes(self):
        modes = (
            "unmerged",
            "wrong-tree",
            "old-review",
            "bot-review",
            "dismissed",
            "moved-development",
            "moved-source",
            "branch-head",
        )
        for mode in modes:
            with self.subTest(mode=mode):
                api, identity, policy, submitted = self.merged()
                reviews = api.responses[f"{PREFIX}/pulls/19/reviews?per_page=100&page=1"]
                if mode == "unmerged":
                    api.pulls[0]["merged"] = False
                elif mode == "wrong-tree":
                    api.responses[f"{PREFIX}/git/commits/{'e' * 40}"]["tree"] = copy.deepcopy(
                        api.responses[f"{PREFIX}/git/commits/{identity.candidate}"]["tree"]
                    )
                elif mode == "old-review":
                    reviews[0]["commit_id"] = identity.candidate
                elif mode == "bot-review":
                    reviews[0]["user"]["type"] = "Bot"
                elif mode == "dismissed":
                    reviews.append({**copy.deepcopy(reviews[0]), "id": 2, "state": "DISMISSED"})
                elif mode == "moved-development":
                    api.responses[f"{PREFIX}/git/ref/heads/development"]["object"]["sha"] = "f" * 40
                elif mode == "moved-source":
                    api.responses["repos/PamuduW/agentbot/git/ref/heads/main"]["object"]["sha"] = (
                        "f" * 40
                    )
                else:
                    api.refs["heads/" + submitted["branch"]] = identity.candidate
                before = len(api.writes)
                with self.assertRaises(CatalogError):
                    self.main_submit(api, identity, policy)
                self.assertEqual(before, len(api.writes))

    def test_source_movement_during_review_revalidation_stops_before_main_pr(self):
        api, identity, policy, _ = self.merged()
        permission = copy.deepcopy(api.responses[f"{PREFIX}/collaborators/reviewer/permission"])
        calls = 0

        def read_permission():
            nonlocal calls
            calls += 1
            if calls == 2:
                api.responses["repos/PamuduW/agentbot/git/ref/heads/main"]["object"]["sha"] = (
                    "f" * 40
                )
            return permission

        api.responses[f"{PREFIX}/collaborators/reviewer/permission"] = read_permission
        before = len(api.writes)
        with self.assertRaises(CatalogError):
            self.main_submit(api, identity, policy)
        self.assertEqual(before, len(api.writes))

    def test_current_review_decisions_and_repository_permissions_are_enforced(self):
        for mode in (
            "COMMENTED",
            "PENDING",
            "CHANGES_REQUESTED",
            "read-only",
            "after-merge",
            "self-review",
        ):
            with self.subTest(mode=mode):
                api, identity, policy, _ = self.merged()
                reviews = api.responses[f"{PREFIX}/pulls/19/reviews?per_page=100&page=1"]
                if mode in {"COMMENTED", "PENDING", "CHANGES_REQUESTED"}:
                    reviews.append({**copy.deepcopy(reviews[0]), "id": 2, "state": mode})
                elif mode == "read-only":
                    api.responses[f"{PREFIX}/collaborators/reviewer/permission"]["permission"] = (
                        "read"
                    )
                elif mode == "after-merge":
                    reviews[0]["submitted_at"] = "2026-10-28T11:00:00Z"
                else:
                    api.pulls[0]["user"]["id"] = 1001
                if mode in {"COMMENTED", "PENDING"}:
                    self.assertEqual(
                        "submitted-main", self.main_submit(api, identity, policy)["status"]
                    )
                else:
                    before = len(api.writes)
                    with self.assertRaises(CatalogError):
                        self.main_submit(api, identity, policy)
                    self.assertEqual(before, len(api.writes))

    def test_cli_main_phase_keeps_dry_run_and_execution_separate(self):
        import contextlib
        import dataclasses
        import importlib.util
        import io
        import json
        from unittest.mock import patch

        script = (
            shared_python_path(Path(__file__).resolve().parents[1]).parents[3]
            / "scripts/catalog/publish.py"
        )
        spec = importlib.util.spec_from_file_location("main_publisher_cli", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for execute in (False, True):
            with self.subTest(execute=execute):
                api, identity, policy, _ = self.merged()
                policy_path = self.directory / "policy.json"
                policy_path.write_text(
                    json.dumps(
                        {"schema_version": 1, "identity": dataclasses.asdict(identity), **policy}
                    )
                )
                args = [str(script), str(policy_path), "--development-pr", "19"]
                if execute:
                    args.append("--execute")
                output = io.StringIO()
                before = len(api.writes)
                with (
                    patch.object(module, "GhApi", return_value=api),
                    patch.object(module, "GhAppWriter", return_value=api) as writer,
                    patch.object(sys, "argv", args),
                    contextlib.redirect_stdout(output),
                ):
                    self.assertEqual(0, module.main())
                result = json.loads(output.getvalue())
                self.assertEqual("submitted-main" if execute else "planned-main", result["status"])
                if not execute:
                    writer.assert_not_called()
                    self.assertEqual(before, len(api.writes))


if __name__ == "__main__":
    unittest.main()
