from __future__ import annotations

import sys
import unittest
from pathlib import Path

from src.shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
from catalog_promotion import PromotionError, verify_promotion


class PromotionTests(unittest.TestCase):
    def test_all_success_requires_exact_heads_and_catalog_only_diff(self) -> None:
        state = {
            "expected_heads": {
                "agentbot": "a" * 40,
                "dotfiles": "b" * 40,
                "shared-main": "c" * 40,
                "shared-development": "c" * 40,
            },
            "current_heads": {
                "agentbot": "a" * 40,
                "dotfiles": "b" * 40,
                "shared-main": "c" * 40,
                "shared-development": "c" * 40,
            },
            "candidate_commit": "d" * 40,
            "run_head": "d" * 40,
            "expected_workflow": "c" * 40,
            "run_workflow": "c" * 40,
            "required_jobs": {
                "qualify / linux-amd64",
                "qualify / linux-arm64",
                "qualify / wsl2-amd64",
            },
            "jobs": [
                {"name": "qualify / " + target, "conclusion": "success"}
                for target in ("linux-amd64", "linux-arm64", "wsl2-amd64")
            ],
            "changed_files": {
                "catalog/software.json": "100644",
                "catalog/software.tsv": "100644",
                "catalog/locks/python.txt": "100644",
            },
            "auto_promote": True,
        }
        verify_promotion(**state)
        changes = [
            {"auto_promote": False},
            {"current_heads": {**state["current_heads"], "agentbot": "0" * 40}},
            {"current_heads": {**state["current_heads"], "shared-development": "0" * 40}},
            {"run_head": "0" * 40},
            {"run_workflow": "0" * 40},
            {"jobs": state["jobs"][:-1]},
            {"jobs": [*state["jobs"], state["jobs"][0]]},
            {"changed_files": {".github/workflows/unsafe.yml": "100644"}},
            {"changed_files": {"catalog/software.json": "120000"}},
            {"changed_files": {}},
        ]
        for status in ("failure", "cancelled", "timed_out", "skipped", None):
            changes.append({"jobs": [{**job, "conclusion": status} for job in state["jobs"]]})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(PromotionError):
                verify_promotion(**{**state, **change})

    def test_candidate_cannot_change_coverage_or_updater_policy(self) -> None:
        import copy

        from catalog_promotion import verify_candidate_policy

        baseline = {
            "schema_version": 1,
            "generation": "old",
            "sources": {},
            "required_targets": ["linux-amd64", "wsl2-amd64"],
            "auto_promote": True,
            "components": {
                "tool": {
                    "owner": "dotfiles",
                    "kind": "software",
                    "source": "publisher/tool",
                    "discovery": {},
                    "requires": [],
                    "blockers": [],
                    "targets": {"any": {"version": "1.2.3"}},
                }
            },
        }
        candidate = copy.deepcopy(baseline)
        candidate["generation"] = "new"
        candidate["components"]["tool"]["targets"]["any"]["version"] = "1.2.4"
        verify_candidate_policy(baseline, candidate)
        mutations = (
            lambda data: data.update(required_targets=["linux-amd64"]),
            lambda data: data.update(auto_promote=False),
            lambda data: data["components"]["tool"].update(
                discovery={"auto_update_env": "UNVERIFIED"}
            ),
            lambda data: data["components"]["tool"].update(blockers=["exception"]),
            lambda data: data["components"]["tool"].update(targets={}),
        )
        for mutate in mutations:
            changed = copy.deepcopy(candidate)
            mutate(changed)
            with self.subTest(mutate=mutate), self.assertRaises(PromotionError):
                verify_candidate_policy(baseline, changed)
