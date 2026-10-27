from __future__ import annotations

import copy
import hashlib
import io
import stat
import sys
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from src.shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))


def archive(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as output:
        for name, content, mode in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            output.writestr(info, content)
    return stream.getvalue()


def identity():
    from catalog_github import RunIdentity

    return RunIdentity(123, 456, 789, 1, "c" * 40, "catalog-candidate/example", "a" * 40)


def run(identity):
    return {
        "id": identity.run_id,
        "run_attempt": identity.attempt,
        "repository": {"id": identity.repository_id, "full_name": "PamuduW/dotfiles-shared"},
        "head_repository": {"id": identity.repository_id},
        "workflow_id": identity.workflow_id,
        "path": ".github/workflows/catalog-candidate.yml",
        "head_sha": identity.candidate,
        "head_branch": identity.branch,
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "referenced_workflows": [
            {
                "path": "PamuduW/dotfiles-shared/.github/workflows/catalog-qualify.yml@"
                + identity.workflow_revision,
                "sha": identity.workflow_revision,
            }
        ],
    }


def jobs(identity):
    return [
        {
            "id": 91,
            "name": "qualify / linux-amd64",
            "run_id": identity.run_id,
            "run_attempt": identity.attempt,
            "head_sha": identity.candidate,
            "status": "completed",
            "conclusion": "success",
            "labels": ["ubuntu-24.04"],
            "runner_name": "hosted-runner",
            "steps": [
                {"name": "Full qualification", "status": "completed", "conclusion": "success"}
            ],
        }
    ]


class GitHubEvidenceTests(unittest.TestCase):
    def test_run_rejects_identity_attempt_event_and_workflow_substitution(self):
        from catalog_github import GitHubError, validate_run

        expected = identity()
        valid = run(expected)
        validate_run(valid, expected)
        mutations = [
            lambda r: r.update(run_attempt=2),
            lambda r: r.update(head_sha="b" * 40),
            lambda r: r.update(event="pull_request_target"),
            lambda r: r.update(workflow_id=1),
            lambda r: r.update(path=".github/workflows/validate.yml"),
            lambda r: r.update(conclusion="skipped"),
            lambda r: r["repository"].update(id=999),
            lambda r: r["head_repository"].update(id=999),
            lambda r: r["referenced_workflows"][0].update(sha="b" * 40),
            lambda r: r.update(referenced_workflows=[]),
        ]
        for mutate in mutations:
            altered = copy.deepcopy(valid)
            mutate(altered)
            with self.subTest(mutate=mutate), self.assertRaises(GitHubError):
                validate_run(altered, expected)

    def test_jobs_require_actual_full_steps_once_on_the_frozen_attempt(self):
        from catalog_github import GitHubError, validate_jobs

        expected = identity()
        required = {"qualify / linux-amd64": ["Full qualification"]}
        valid = jobs(expected)
        validate_jobs(valid, expected, required)
        for conclusion in ("failure", "cancelled", "skipped", "timed_out", "neutral", None):
            altered = copy.deepcopy(valid)
            altered[0]["conclusion"] = conclusion
            with self.subTest(conclusion=conclusion), self.assertRaises(GitHubError):
                validate_jobs(altered, expected, required)
        for altered in (
            [],
            valid * 2,
            [{**valid[0], "run_attempt": 2}],
            [{**valid[0], "steps": []}],
        ):
            with self.assertRaises(GitHubError):
                validate_jobs(altered, expected, required)

    def test_zip_accepts_only_bounded_regular_json_and_checks_archive_hash(self):
        from catalog_github import GitHubError, read_receipt_zip

        content = b'{"schema_version":1}'
        raw = archive([("receipt.json", content, stat.S_IFREG | 0o600)])
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        self.assertEqual({"schema_version": 1}, read_receipt_zip(raw, digest))
        with self.assertRaises(GitHubError):
            read_receipt_zip(raw, "sha256:" + "0" * 64)
        cases = [
            [("../receipt.json", content, stat.S_IFREG | 0o600)],
            [("receipt.json", content, stat.S_IFLNK | 0o777)],
            [("receipt.json", content, stat.S_IFREG | 0o700)],
            [
                ("receipt.json", content, stat.S_IFREG | 0o600),
                ("code.py", b"pass", stat.S_IFREG | 0o600),
            ],
            [("receipt.json", b'{"x":1,"x":2}', stat.S_IFREG | 0o600)],
            [("receipt.json", b" " * (4_194_304 + 1), stat.S_IFREG | 0o600)],
        ]
        for entries in cases:
            raw = archive(entries)
            with self.subTest(entries=entries[0][0]), self.assertRaises(GitHubError):
                read_receipt_zip(raw, "sha256:" + hashlib.sha256(raw).hexdigest())

    def test_pagination_fetches_all_pages_and_rejects_inconsistent_totals(self):
        from catalog_github import GhApi, GitHubError

        api = GhApi()
        pages = [
            {"total_count": 101, "jobs": [{"id": i} for i in range(100)]},
            {"total_count": 101, "jobs": [{"id": 100}]},
        ]
        with patch.object(api, "json", side_effect=pages):
            self.assertEqual(
                101, len(api.pages("repos/PamuduW/dotfiles-shared/actions/runs/789/jobs", "jobs"))
            )
        with (
            patch.object(api, "json", return_value={"total_count": 101, "jobs": []}),
            self.assertRaises(GitHubError),
        ):
            api.pages("repos/PamuduW/dotfiles-shared/actions/runs/789/jobs", "jobs")

    def test_adapter_rejects_urls_and_mutation_options_before_starting_gh(self):
        from catalog_github import GhApi, GitHubError

        for endpoint in (
            "https://evil.invalid",
            "--method=POST",
            "repos/x/y/../../app",
            "repos/x/y?token=secret",
        ):
            with patch("subprocess.Popen") as process, self.assertRaises(GitHubError):
                GhApi().read(endpoint)
            process.assert_not_called()

    def test_duplicate_json_keys_are_rejected(self):
        from catalog_github import GhApi, GitHubError

        with (
            patch.object(GhApi, "read", return_value=b'{"id":1,"id":2}'),
            self.assertRaises(GitHubError),
        ):
            GhApi().json("repos/PamuduW/dotfiles-shared")

    def test_adapter_allows_immutable_compare_paths_and_bounds_real_process_output(self):
        import os
        import tempfile

        from catalog_github import GhApi, GitHubError

        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "gh"
            executable.write_text(
                "#!/usr/bin/python3\nimport os, sys\n"
                "assert sys.argv[1:6] == ['api', '--hostname', 'github.com', '--method', 'GET']\n"
                "assert 'GH_DEBUG' not in os.environ\n"
                "assert os.environ['GH_TOKEN'] == 'fixture-private-token'\n"
                "assert 'fixture-private-token' not in ' '.join(sys.argv)\n"
                "print('x' * 128)\n"
            )
            executable.chmod(0o700)
            with patch.dict(
                os.environ,
                {"PATH": temporary, "GH_TOKEN": "fixture-private-token", "GH_DEBUG": "api"},
            ):
                raw = GhApi().read(
                    "repos/PamuduW/dotfiles-shared/compare/" + "a" * 40 + "..." + "b" * 40,
                    maximum=256,
                )
                self.assertEqual(b"x" * 128 + b"\n", raw)
                with self.assertRaisesRegex(GitHubError, "size limit"):
                    GhApi().read("repos/PamuduW/dotfiles-shared", maximum=64)

    def test_malformed_external_jobs_are_rejected_without_type_errors(self):
        from catalog_github import GitHubError, validate_jobs

        for key, value in (("labels", None), ("runner_name", {}), ("steps", {})):
            altered = jobs(identity())
            altered[0][key] = value
            with self.subTest(key=key), self.assertRaises(GitHubError):
                validate_jobs(
                    altered, identity(), {"qualify / linux-amd64": ["Full qualification"]}
                )


if __name__ == "__main__":
    unittest.main()
