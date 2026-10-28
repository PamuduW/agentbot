from __future__ import annotations

import base64
import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs

from src.shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
from catalog_github import PREFIX, GitHubError
from catalog_publication import plan_publication
from software_catalog import CatalogError

from . import test_catalog_publication as publications


class WritableGitHub(publications.FakeGitHub):
    def __init__(self, original, heads):
        super().__init__()
        self.responses = copy.deepcopy(original.responses)
        self.archives = copy.deepcopy(original.archives)
        self.writes = []
        self.refs = {}
        self.pulls = []
        self.heads = heads
        self.signed = True
        self.after_write = lambda endpoint: None
        self.ambiguous = set()
        self.pull_reads = 0
        self.close_on_third_read = False
        self.responses["installation/repositories?per_page=100&page=1"] = {
            "total_count": 1,
            "repositories": [{"id": 123, "full_name": "PamuduW/dotfiles-shared"}],
        }

    def json(self, endpoint):
        if endpoint.startswith(PREFIX + "/git/matching-refs/heads/catalog-publish/"):
            prefix = endpoint.split("/matching-refs/", 1)[1]
            return [
                {"ref": "refs/" + name, "object": {"type": "commit", "sha": sha}}
                for name, sha in self.refs.items()
                if name.startswith(prefix)
            ]
        if endpoint.startswith(PREFIX + "/pulls?"):
            self.pull_reads += 1
            if self.close_on_third_read and self.pull_reads == 3:
                self.pulls[0]["state"] = "closed"
            query = parse_qs(endpoint.partition("?")[2])
            return copy.deepcopy(
                [
                    pull
                    for pull in self.pulls
                    if "base" not in query or pull["base"]["ref"] in query["base"]
                ]
            )
        return super().json(endpoint)

    def post(self, endpoint, payload):
        self.writes.append((endpoint, copy.deepcopy(payload)))
        if endpoint == PREFIX + "/git/trees":
            tree = self.responses[f"{PREFIX}/git/trees/{payload['base_tree']}?recursive=1"]["tree"]
            files = {
                entry["path"]: base64.b64decode(
                    self.responses[f"{PREFIX}/git/blobs/{entry['sha']}"]["content"]
                )
                for entry in tree
            }
            for entry in payload["tree"]:
                self.assert_file(entry)
                files[entry["path"]] = entry["content"].encode()
            result = {"sha": self.tree(files)}
        elif endpoint == PREFIX + "/git/commits":
            if set(payload) != {"tree", "parents", "message"}:
                raise AssertionError("custom commit identity/signature must not be supplied")
            sha = hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            result = {
                "sha": sha,
                "tree": {"sha": payload["tree"]},
                "parents": [{"sha": parent} for parent in payload["parents"]],
                "message": payload["message"],
                "verification": {"verified": self.signed, "reason": "valid"},
            }
            self.responses[f"{PREFIX}/git/commits/{sha}"] = result
        elif endpoint == PREFIX + "/git/refs":
            name = payload["ref"].removeprefix("refs/")
            if not name.startswith("heads/catalog-publish/") or name in self.refs:
                raise GitHubError("unexpected or existing reference")
            self.refs[name] = payload["sha"]
            result = {"ref": payload["ref"], "object": {"type": "commit", "sha": payload["sha"]}}
        elif endpoint == PREFIX + "/pulls":
            sha = self.refs["heads/" + payload["head"]]
            result = {
                "number": 19,
                "state": "open",
                "merged_at": None,
                "html_url": "https://github.com/PamuduW/dotfiles-shared/pull/19",
                "head": {"ref": payload["head"], "sha": sha, "repo": {"id": 123}},
                "base": {
                    "ref": payload["base"],
                    "sha": self.heads["shared-development"],
                    "repo": {"id": 123},
                },
            }
            self.pulls.append(result)
        else:
            raise AssertionError("writer reached an unapproved mutation endpoint")
        self.after_write(endpoint)
        if endpoint in self.ambiguous:
            raise GitHubError("ambiguous write response")
        return copy.deepcopy(result)

    @staticmethod
    def assert_file(entry):
        assert set(entry) == {"path", "mode", "type", "content"}
        assert entry["mode"] == "100644" and entry["type"] == "blob"
        assert entry["path"] in {"catalog/approval", "catalog/approval.json"}


class WriterTests(unittest.TestCase):
    setUp = publications.PublicationTests.setUp
    write = publications.PublicationTests.write
    example = publications.PublicationTests.example
    set_receipt = publications.PublicationTests.set_receipt

    def fixture(self):
        original, identity, heads, envelope = self.example()
        api = WritableGitHub(original, heads)
        for suffix in ("", f"/attempts/{identity.attempt}"):
            api.responses[f"{PREFIX}/actions/runs/{identity.run_id}{suffix}"]["actor"] = {
                "login": "catalog-publisher[bot]",
                "type": "Bot",
            }
        api.responses[f"{PREFIX}/git/commits/{identity.candidate}"]["verification"] = {
            "verified": True,
            "reason": "valid",
        }
        policy = {
            "expected_heads": heads,
            "expected_sources": envelope["receipt"]["sources"],
            "required_jobs": {"qualify / linux-amd64": ["Full qualification"]},
            "publisher_app_id": 42,
            "publisher_app_slug": "catalog-publisher",
        }
        return api, identity, policy, envelope

    def submit(self, api, identity, policy):
        from catalog_writer import submit_publication

        return submit_publication(api, identity, **policy)

    def test_submission_is_catalog_only_reviewable_and_idempotent(self):
        api, identity, policy, _ = self.fixture()
        result = self.submit(api, identity, policy)
        self.assertEqual("submitted", result["status"])
        self.assertFalse(result["published"])
        self.assertEqual(19, result["pull_request"]["number"])
        self.assertEqual("held-until-reviewed-development-merge", result["main_phase"])
        self.assertEqual(1, len(api.refs))
        self.assertEqual(1, len(api.pulls))
        writes = copy.deepcopy(api.writes)
        self.assertEqual(result, self.submit(api, identity, policy))
        self.assertEqual(writes, api.writes)
        self.assertTrue(all("/merge" not in endpoint for endpoint, _ in api.writes))

    def test_cli_defaults_to_read_only_and_requires_explicit_execution(self):
        import contextlib
        import dataclasses
        import importlib.util
        import io
        from unittest.mock import patch

        shared = shared_python_path(Path(__file__).resolve().parents[1]).parents[3]
        script = shared / "scripts/catalog/publish.py"
        self.assertTrue(script.is_file(), "protected publisher CLI is missing")
        spec = importlib.util.spec_from_file_location("catalog_publish_cli", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for execute in (False, True):
            with self.subTest(execute=execute):
                api, identity, policy, _ = self.fixture()
                path = self.directory / "policy.json"
                path.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "identity": dataclasses.asdict(identity),
                            **policy,
                        }
                    )
                )
                args = [str(script), str(path)] + (["--execute"] if execute else [])
                output = io.StringIO()
                with (
                    patch.object(module, "GhApi", return_value=api),
                    patch.object(module, "GhAppWriter", return_value=api) as writer,
                    patch.object(sys, "argv", args),
                    contextlib.redirect_stdout(output),
                ):
                    self.assertEqual(0, module.main())
                result = json.loads(output.getvalue())
                self.assertEqual("submitted" if execute else "planned", result["status"])
                if not execute:
                    writer.assert_not_called()
                    self.assertEqual([], api.writes)

    def test_invalid_scope_or_incomplete_qualification_performs_no_write(self):
        for mode in (
            "scope",
            "qualification",
            "actor",
            "attempt-actor",
            "candidate-signature",
            "no-app",
        ):
            with self.subTest(mode=mode):
                api, identity, policy, envelope = self.fixture()
                if mode == "scope":
                    api.responses["installation/repositories?per_page=100&page=1"][
                        "total_count"
                    ] = 2
                elif mode == "actor":
                    api.responses[f"{PREFIX}/actions/runs/{identity.run_id}"]["actor"] = {
                        "login": "human-pusher",
                        "type": "User",
                    }
                elif mode == "attempt-actor":
                    api.responses[
                        f"{PREFIX}/actions/runs/{identity.run_id}/attempts/{identity.attempt}"
                    ]["actor"] = {
                        "login": "another-app[bot]",
                        "type": "Bot",
                    }
                elif mode == "candidate-signature":
                    api.responses[f"{PREFIX}/git/commits/{identity.candidate}"]["verification"][
                        "verified"
                    ] = False
                elif mode == "no-app":
                    policy.pop("publisher_app_slug")
                else:
                    envelope["receipt"]["scenarios"]["upgrade"] = "skipped"
                    self.set_receipt(api, identity, envelope)
                with self.assertRaises(CatalogError):
                    self.submit(api, identity, policy)
                self.assertEqual([], api.writes)

    def test_unsigned_commit_is_never_referenced(self):
        api, identity, policy, _ = self.fixture()
        api.signed = False
        with self.assertRaisesRegex(CatalogError, "signature"):
            self.submit(api, identity, policy)
        self.assertEqual({}, api.refs)
        self.assertEqual([], api.pulls)

    def test_source_movement_stops_before_branch_or_pr(self):
        for boundary in ("/git/commits", "/git/refs"):
            with self.subTest(boundary=boundary):
                api, identity, policy, _ = self.fixture()

                def move(endpoint, boundary=boundary, api=api):
                    if endpoint.endswith(boundary):
                        api.responses["repos/PamuduW/agentbot/git/ref/heads/main"]["object"][
                            "sha"
                        ] = "f" * 40

                api.after_write = move
                with self.assertRaises(CatalogError):
                    self.submit(api, identity, policy)
                self.assertEqual([], api.pulls)
                if boundary == "/git/commits":
                    self.assertEqual({}, api.refs)

    def test_ambiguous_visible_writes_are_reconciled_without_duplicates(self):
        api, identity, policy, _ = self.fixture()
        api.ambiguous = {PREFIX + "/git/refs", PREFIX + "/pulls"}
        result = self.submit(api, identity, policy)
        self.assertEqual("submitted", result["status"])
        self.assertEqual(1, len(api.refs))
        self.assertEqual(1, len(api.pulls))

    def test_pr_movement_during_final_revalidation_is_held(self):
        api, identity, policy, _ = self.fixture()
        api.close_on_third_read = True
        with self.assertRaises(CatalogError):
            self.submit(api, identity, policy)

    def test_unexpected_existing_ref_and_changed_or_closed_pr_are_held(self):
        api, identity, policy, _ = self.fixture()
        plan = plan_publication(api, identity, **policy)
        api.refs["heads/catalog-publish/" + plan["transaction_id"]] = identity.candidate
        with self.assertRaises(CatalogError):
            self.submit(api, identity, policy)
        self.assertEqual([], api.writes)
        for mode in ("closed", "head", "base", "base-ref", "duplicate"):
            with self.subTest(mode=mode):
                api, identity, policy, _ = self.fixture()
                self.submit(api, identity, policy)
                if mode == "closed":
                    api.pulls[0]["state"] = "closed"
                elif mode == "duplicate":
                    api.pulls.append(copy.deepcopy(api.pulls[0]))
                elif mode == "base-ref":
                    api.pulls[0]["base"]["ref"] = "main"
                else:
                    api.pulls[0][mode]["sha"] = "f" * 40
                count = len(api.writes)
                with self.assertRaises(CatalogError):
                    self.submit(api, identity, policy)
                self.assertEqual(count, len(api.writes))


if __name__ == "__main__":
    unittest.main()
