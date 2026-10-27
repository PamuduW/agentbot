from __future__ import annotations

import base64
import copy
import hashlib
import json
import stat
import sys
import unittest
from pathlib import Path

from src.shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
from software_catalog import load_snapshot

from . import test_software_catalog as fixtures
from .test_catalog_github import archive, identity, jobs, run


def blob_id(raw):
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def protection():
    return {
        "required_status_checks": {
            "strict": True,
            "checks": [{"context": "catalog/publication-ready", "app_id": 42}],
        },
        "required_pull_request_reviews": {
            "required_approving_review_count": 1,
            "dismiss_stale_reviews": True,
            "require_last_push_approval": True,
            "bypass_pull_request_allowances": {"users": [], "teams": [], "apps": []},
        },
        "enforce_admins": {"enabled": True},
        "allow_force_pushes": {"enabled": False},
        "allow_deletions": {"enabled": False},
        "restrictions": {"users": [], "teams": [], "apps": [{"id": 42}]},
    }


class FakeGitHub:
    def __init__(self):
        self.responses = {}
        self.archives = {}
        self.calls = []

    def json(self, endpoint):
        self.calls.append(endpoint)
        result = self.responses[endpoint]
        return copy.deepcopy(result() if callable(result) else result)

    def pages(self, endpoint, key):
        return self.json(endpoint)[key]

    def read(self, endpoint, *, maximum):
        self.calls.append(endpoint)
        raw = self.archives[endpoint]
        if len(raw) > maximum:
            raise AssertionError("test archive exceeds read bound")
        return raw

    def tree(self, files):
        entries = []
        for path, content in sorted(files.items()):
            digest = blob_id(content)
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": digest})
            self.responses[f"repos/PamuduW/dotfiles-shared/git/blobs/{digest}"] = {
                "sha": digest,
                "encoding": "base64",
                "size": len(content),
                "content": base64.b64encode(content).decode(),
            }
        sha = hashlib.sha1(json.dumps(entries, sort_keys=True).encode()).hexdigest()
        self.responses[f"repos/PamuduW/dotfiles-shared/git/trees/{sha}?recursive=1"] = {
            "sha": sha,
            "truncated": False,
            "tree": entries,
        }
        return sha


class PublicationTests(unittest.TestCase):
    setUp = fixtures.SoftwareCatalogTests.setUp
    write = fixtures.SoftwareCatalogTests.write

    def example(self, *, approved=True, auto_promote=True):
        expected = identity()
        heads = {
            "agentbot": "a" * 40,
            "dotfiles": "a" * 40,
            "shared-main": "b" * 40,
            "shared-development": "d" * 40,
        }
        self.data["generation"] = "fixture-1"
        self.data["components"]["fixture"]["targets"]["linux-amd64"]["version"] = "1.2.3"
        self.data["auto_promote"] = auto_promote
        self.write(approve=approved)
        predecessor_digest = load_snapshot(self.directory, require_approved=False).digest
        baseline = {"catalog/" + path.name: path.read_bytes() for path in self.directory.iterdir()}
        self.data["generation"] = "fixture-2"
        self.data["components"]["fixture"]["targets"]["linux-amd64"]["version"] = "1.2.4"
        self.write(approve=False)
        snapshot = load_snapshot(self.directory, require_approved=False)
        candidate = {"catalog/" + path.name: path.read_bytes() for path in self.directory.iterdir()}
        code = {
            "README.md": b"trusted code",
            "catalog/README.md": b"catalog policy documentation",
            ".github/workflows/catalog-candidate.yml": b"trusted wrapper",
        }
        api = FakeGitHub()
        root = "repos/PamuduW/dotfiles-shared"
        base_tree = api.tree({**code, **baseline})
        candidate_tree = api.tree({**code, **candidate})
        for sha, tree, parents in (
            (heads["shared-main"], base_tree, [heads["shared-development"]]),
            (heads["shared-development"], base_tree, ["a" * 40]),
            ("a" * 40, base_tree, []),
            (expected.candidate, candidate_tree, [heads["shared-main"]]),
        ):
            api.responses[f"{root}/git/commits/{sha}"] = {
                "sha": sha,
                "tree": {"sha": tree},
                "parents": [{"sha": parent} for parent in parents],
            }
        for name, repo, branch in (
            ("agentbot", "agentbot", "main"),
            ("dotfiles", "dotfiles", "main"),
            ("shared-main", "dotfiles-shared", "main"),
            ("shared-development", "dotfiles-shared", "development"),
        ):
            api.responses[f"repos/PamuduW/{repo}/git/ref/heads/{branch}"] = {
                "ref": f"refs/heads/{branch}",
                "object": {"type": "commit", "sha": heads[name]},
            }
        for ancestor in (heads["shared-development"], "a" * 40):
            api.responses[f"{root}/compare/{ancestor}...{heads['shared-main']}"] = {
                "status": "ahead",
                "merge_base_commit": {"sha": ancestor},
            }
        for branch in ("main", "development"):
            api.responses[f"{root}/branches/{branch}/protection"] = protection()
        api.responses[f"{root}/rulesets"] = []
        api.responses[root] = {
            "id": expected.repository_id,
            "full_name": "PamuduW/dotfiles-shared",
            "default_branch": "main",
            "owner": {"type": "Organization"},
        }
        api.responses[f"{root}/git/ref/heads/{expected.branch}"] = {
            "ref": "refs/heads/" + expected.branch,
            "object": {"type": "commit", "sha": expected.candidate},
        }
        run_root = f"{root}/actions/runs/{expected.run_id}"
        api.responses[run_root] = run(expected)
        api.responses[f"{run_root}/attempts/1"] = run(expected)
        api.responses[f"{run_root}/attempts/1/jobs"] = {"jobs": jobs(expected)}
        artifact = self.data["components"]["fixture"]["targets"]["linux-amd64"]
        receipt = {
            "catalog_digest": snapshot.digest,
            "sources": dict(snapshot.data["sources"]),
            "target": "linux-amd64",
            "run_id": str(expected.run_id),
            "runner_image": "ubuntu-24.04-fixture",
            "scenarios": dict.fromkeys(("fresh", "upgrade", "repeat", "failure-safety"), "success"),
            "checks": dict.fromkeys(("product-gates", "behavior", "updater-controls"), "success"),
            "observations": {
                "fixture": {
                    "version": "1.2.4",
                    "integrity": artifact["digest"],
                    "owned": True,
                    "managed_path": True,
                }
            },
        }
        envelope = {
            "schema_version": 1,
            "candidate_commit": expected.candidate,
            "workflow_revision": expected.workflow_revision,
            "run_attempt": 1,
            "approved_predecessor": {
                "commit": heads["shared-main"],
                "catalog_digest": predecessor_digest,
            },
            "receipt": receipt,
        }
        self.set_receipt(api, expected, envelope)
        return api, expected, heads, envelope

    def set_receipt(self, api, expected, envelope):
        root = "repos/PamuduW/dotfiles-shared"
        raw = archive([("receipt.json", json.dumps(envelope).encode(), stat.S_IFREG | 0o600)])
        api.archives[f"{root}/actions/artifacts/321/zip"] = raw
        api.responses[f"{root}/actions/runs/{expected.run_id}/artifacts"] = {
            "artifacts": [
                {
                    "id": 321,
                    "name": f"qualification-linux-amd64-{expected.run_id}-1",
                    "expired": False,
                    "size_in_bytes": len(raw),
                    "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                    "workflow_run": {
                        "id": expected.run_id,
                        "repository_id": expected.repository_id,
                        "head_repository_id": expected.repository_id,
                        "head_sha": expected.candidate,
                        "head_branch": expected.branch,
                    },
                }
            ]
        }

    def plan(self, api, expected, heads):
        from catalog_publication import plan_publication

        return plan_publication(
            api,
            expected,
            expected_heads=heads,
            expected_sources=self.data["sources"],
            required_jobs={"qualify / linux-amd64": ["Full qualification"]},
            publisher_app_id=42,
        )

    def test_distinct_synchronized_branch_heads_produce_only_an_immutable_plan(self):
        api, expected, heads, _ = self.example()
        result = self.plan(api, expected, heads)
        self.assertEqual("planned", result["status"])
        self.assertFalse(result["published"])
        self.assertEqual(expected.candidate, result["parent"])
        self.assertEqual(heads, result["expected_heads"])
        self.assertEqual(
            ["catalog/approval", "catalog/approval.json"],
            [item["path"] for item in result["files"]],
        )
        self.assertEqual("development", result["pull_request"]["base"])
        self.assertEqual("held-until-reviewed-development-merge", result["main_phase"])
        self.assertEqual(64, len(result["transaction_id"]))
        self.assertEqual(result, self.plan(api, expected, heads))

    def test_personal_repository_does_not_require_organization_only_push_restrictions(self):
        api, expected, heads, _ = self.example()
        root = "repos/PamuduW/dotfiles-shared"
        api.responses[root]["owner"] = {"type": "User"}
        for branch in ("main", "development"):
            policy = api.responses[f"{root}/branches/{branch}/protection"]
            policy["restrictions"] = None
            policy["required_pull_request_reviews"].pop("bypass_pull_request_allowances")
        self.assertEqual("planned", self.plan(api, expected, heads)["status"])

    def test_partial_or_forged_receipts_never_authorize_a_plan(self):
        from software_catalog import CatalogError

        for mutation in (
            lambda data: data["receipt"].update(scope="inventory-smoke"),
            lambda data: data["receipt"]["scenarios"].pop("upgrade"),
            lambda data: data["receipt"]["observations"].clear(),
            lambda data: data["receipt"].update(catalog_digest="0" * 64),
            lambda data: data.update(run_attempt=2),
            lambda data: data["approved_predecessor"].update(commit="f" * 40),
            lambda data: data.pop("approved_predecessor"),
            lambda data: data["receipt"]["sources"].update(agentbot="f" * 40),
        ):
            api, expected, heads, envelope = self.example()
            mutation(envelope)
            self.set_receipt(api, expected, envelope)
            with self.subTest(mutation=mutation), self.assertRaises(CatalogError):
                self.plan(api, expected, heads)

    def test_branch_protection_cannot_be_missing_loose_or_bypassed(self):
        from software_catalog import CatalogError

        for mutate in (
            lambda p: p.clear(),
            lambda p: p["required_status_checks"].update(strict=False),
            lambda p: p["required_status_checks"]["checks"][0].update(app_id=99),
            lambda p: p["required_pull_request_reviews"].update(required_approving_review_count=0),
            lambda p: p["required_pull_request_reviews"]["bypass_pull_request_allowances"][
                "apps"
            ].append({"id": 42}),
            lambda p: p["enforce_admins"].update(enabled=False),
            lambda p: p["restrictions"]["users"].append({"login": "other"}),
        ):
            api, expected, heads, _ = self.example()
            mutate(api.responses["repos/PamuduW/dotfiles-shared/branches/main/protection"])
            with self.subTest(mutate=mutate), self.assertRaises(CatalogError):
                self.plan(api, expected, heads)

    def test_moved_source_base_unrelated_history_and_changed_tree_are_held(self):
        from software_catalog import CatalogError

        for kind in (
            "source",
            "base",
            "candidate-ref",
            "ancestry",
            "tree",
            "truncated",
            "mode",
            "parent",
            "blob",
        ):
            api, expected, heads, _ = self.example()
            root = "repos/PamuduW/dotfiles-shared"
            if kind == "source":
                api.responses["repos/PamuduW/agentbot/git/ref/heads/main"]["object"]["sha"] = (
                    "f" * 40
                )
            elif kind == "candidate-ref":
                api.responses[f"{root}/git/ref/heads/{expected.branch}"]["object"]["sha"] = "f" * 40
            elif kind == "base":
                endpoint = f"{root}/git/ref/heads/main"
                original = copy.deepcopy(api.responses[endpoint])
                calls = []

                def moved(calls=calls, original=original):
                    calls.append(1)
                    return (
                        original
                        if len(calls) == 1
                        else {**original, "object": {"type": "commit", "sha": "f" * 40}}
                    )

                api.responses[endpoint] = moved
            elif kind == "ancestry":
                api.responses[
                    f"{root}/compare/{heads['shared-development']}...{heads['shared-main']}"
                ]["merge_base_commit"]["sha"] = "f" * 40
            elif kind == "parent":
                api.responses[f"{root}/git/commits/{expected.candidate}"]["parents"] = [
                    {"sha": "f" * 40}
                ]
            else:
                tree = api.responses[f"{root}/git/commits/{expected.candidate}"]["tree"]["sha"]
                data = api.responses[f"{root}/git/trees/{tree}?recursive=1"]
                if kind == "truncated":
                    data["truncated"] = True
                elif kind == "tree":
                    next(item for item in data["tree"] if item["path"] == "README.md")["sha"] = (
                        "f" * 40
                    )
                elif kind == "mode":
                    next(item for item in data["tree"] if item["path"] == "catalog/software.json")[
                        "mode"
                    ] = "120000"
                else:
                    blob = next(
                        item for item in data["tree"] if item["path"] == "catalog/software.json"
                    )["sha"]
                    api.responses[f"{root}/git/blobs/{blob}"]["content"] = base64.b64encode(
                        b"changed"
                    ).decode()
            with self.subTest(kind=kind), self.assertRaises(CatalogError):
                self.plan(api, expected, heads)

    def test_live_job_failure_and_rerun_hold_even_with_success_receipts(self):
        from software_catalog import CatalogError

        for kind in (
            "failed",
            "rerun",
            "expired",
            "artifact-origin",
            "artifact-digest",
            "artifact-name",
        ):
            api, expected, heads, _ = self.example()
            root = "repos/PamuduW/dotfiles-shared/actions/runs/789"
            if kind == "failed":
                api.responses[root + "/attempts/1/jobs"]["jobs"][0]["conclusion"] = "failure"
            elif kind == "rerun":
                original = copy.deepcopy(api.responses[root])
                calls = []

                def rerun(calls=calls, original=original):
                    calls.append(1)
                    return original if len(calls) == 1 else {**original, "run_attempt": 2}

                api.responses[root] = rerun
            else:
                artifact = api.responses[root + "/artifacts"]["artifacts"][0]
                if kind == "expired":
                    artifact["expired"] = True
                elif kind == "artifact-origin":
                    artifact["workflow_run"]["head_repository_id"] = 999
                elif kind == "artifact-digest":
                    artifact["digest"] = "sha256:" + "0" * 64
                else:
                    artifact["name"] = "inventory-smoke-789-1"
            with self.subTest(kind=kind), self.assertRaises(CatalogError):
                self.plan(api, expected, heads)

    def test_status_returns_held_and_cli_never_needs_a_token_for_invalid_policy(self):
        import subprocess

        from catalog_publication import publication_status

        api, expected, heads, _ = self.example()
        api.responses["repos/PamuduW/dotfiles-shared/branches/main/protection"] = {}
        result = publication_status(
            api,
            expected,
            expected_heads=heads,
            expected_sources=self.data["sources"],
            required_jobs={"qualify / linux-amd64": ["Full qualification"]},
            publisher_app_id=42,
        )
        self.assertEqual("held", result["status"])
        self.assertFalse(result["published"])
        policy = self.directory / "policy.json"
        policy.write_text('{"schema_version":1}')
        script = (
            shared_python_path(Path(__file__).resolve().parents[1]).parents[2]
            / "catalog/publication-status.py"
        )
        completed = subprocess.run(
            [sys.executable, str(script), str(policy)], capture_output=True, text=True, check=False
        )
        self.assertEqual(2, completed.returncode, completed.stderr)
        self.assertEqual("held", json.loads(completed.stdout)["status"])

    def test_seed_disabled_policy_and_unknown_protections_remain_held(self):
        from software_catalog import CatalogError

        for options in ({"approved": False}, {"auto_promote": False}):
            api, expected, heads, _ = self.example(**options)
            with self.subTest(options=options), self.assertRaises(CatalogError):
                self.plan(api, expected, heads)
        api, expected, heads, _ = self.example()
        api.responses["repos/PamuduW/dotfiles-shared/rulesets"] = [
            {"id": 1, "enforcement": "active"}
        ]
        with self.assertRaisesRegex(CatalogError, "ruleset"):
            self.plan(api, expected, heads)

    def test_cli_reads_real_bounded_gh_json_and_zip_to_emit_a_plan(self):
        import dataclasses
        import os
        import subprocess

        api, expected, heads, _ = self.example()
        workspace = self.directory / "reader"
        workspace.mkdir()
        payloads = {
            key: base64.b64encode(json.dumps(value).encode()).decode()
            for key, value in api.responses.items()
        }
        for key, value in api.responses.items():
            for collection in ("jobs", "artifacts"):
                if isinstance(value, dict) and collection in value:
                    data = {**value, "total_count": len(value[collection])}
                    payloads[key + "?per_page=100&page=1"] = base64.b64encode(
                        json.dumps(data).encode()
                    ).decode()
        payloads.update(
            {key: base64.b64encode(value).decode() for key, value in api.archives.items()}
        )
        (workspace / "responses.json").write_text(json.dumps(payloads))
        executable = workspace / "gh"
        executable.write_text(
            "#!/usr/bin/python3\nimport base64, json, pathlib, sys\n"
            "assert sys.argv[1:6] == ['api', '--hostname', 'github.com', '--method', 'GET']\n"
            "responses = json.loads(pathlib.Path(__file__).with_name('responses.json').read_text())\n"
            "sys.stdout.buffer.write(base64.b64decode(responses[sys.argv[-1]]))\n"
        )
        executable.chmod(0o700)
        policy = workspace / "policy.json"
        policy.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "identity": dataclasses.asdict(expected),
                    "expected_heads": heads,
                    "expected_sources": self.data["sources"],
                    "required_jobs": {"qualify / linux-amd64": ["Full qualification"]},
                    "publisher_app_id": 42,
                }
            )
        )
        script = (
            shared_python_path(Path(__file__).resolve().parents[1]).parents[2]
            / "catalog/publication-status.py"
        )
        result = subprocess.run(
            [sys.executable, str(script), str(policy)],
            env={**os.environ, "PATH": str(workspace)},
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        planned = json.loads(result.stdout)
        self.assertEqual("planned", planned["status"])
        self.assertEqual(
            self.plan(api, expected, heads)["transaction_id"], planned["transaction_id"]
        )


if __name__ == "__main__":
    unittest.main()
