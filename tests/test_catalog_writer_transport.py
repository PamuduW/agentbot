from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
from catalog_github import PREFIX, GitHubError


class WriterTransportTests(unittest.TestCase):
    def client(self):
        from catalog_writer_transport import GhAppWriter

        with patch.dict(os.environ, {"GH_TOKEN": "synthetic-installation-token"}):
            return GhAppWriter()

    def test_writer_refuses_implicit_credential_fallback(self):
        from catalog_writer_transport import GhAppWriter

        with (
            patch.dict(os.environ, {"GITHUB_TOKEN": "synthetic-other-token"}, clear=True),
            self.assertRaises(GitHubError),
        ):
            GhAppWriter()

    def test_unsafe_endpoints_or_protected_refs_never_start_a_process(self):
        client = self.client()
        attempts = (
            (PREFIX + "/git/refs", {"ref": "refs/heads/main", "sha": "a" * 40}),
            (PREFIX + "/git/refs", {"ref": "refs/heads/development", "sha": "a" * 40}),
            (PREFIX + "/pulls/1/merge", {}),
            (PREFIX + "/branches/main/protection", {}),
            ("repos/another/repository/git/refs", {}),
            ("--method=DELETE", {}),
            (
                PREFIX + "/git/trees",
                {
                    "base_tree": "a" * 40,
                    "tree": [
                        {
                            "path": ".github/workflows/run.yml",
                            "mode": "100644",
                            "type": "blob",
                            "content": "unsafe",
                        }
                    ],
                },
            ),
        )
        for endpoint, data in attempts:
            with self.subTest(endpoint=endpoint), patch("subprocess.Popen") as process:
                with self.assertRaises(GitHubError):
                    client.post(endpoint, data)
                process.assert_not_called()
        for endpoint in ("https://evil.invalid", PREFIX + "/pulls?token=private", "repos/x/y"):
            with self.subTest(endpoint=endpoint), patch("subprocess.Popen") as process:
                with self.assertRaises(GitHubError):
                    client.read(endpoint)
                process.assert_not_called()

    def test_fixed_public_source_refs_never_use_credentials_or_gh(self):
        client = self.client()
        with (
            patch.object(
                client,
                "_run_process",
                return_value=b'{"object":{"sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}',
            ) as process,
            patch.dict(os.environ, {"CATALOG_GITHUB_TOKEN": "synthetic-unused-token"}),
        ):
            result = client.json("repos/PamuduW/agentbot/git/ref/heads/main")
        self.assertEqual("a" * 40, result["object"]["sha"])
        command, body, environment = process.call_args.args
        self.assertEqual([sys.executable, "-I", "-S", "-B", "-c"], command[:5])
        self.assertIn("repos/PamuduW/agentbot/git/ref/heads/main", command)
        self.assertEqual(b"", body)
        self.assertNotIn("GH_TOKEN", environment)
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("CATALOG_GITHUB_TOKEN", environment)
        self.assertEqual(30, process.call_args.kwargs["timeout"])

    def test_slow_streaming_process_cannot_extend_the_total_request_deadline(self):
        client = self.client()
        start = time.monotonic()
        with self.assertRaisesRegex(GitHubError, "timed out"):
            client._run_process(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    "import sys,time\nfor i in range(100):\n print('x',flush=True);time.sleep(.02)",
                ],
                b"",
                {},
                maximum=1024,
                timeout=0.12,
            )
        self.assertLess(time.monotonic() - start, 1)

    def test_real_gh_process_receives_structured_stdin_and_no_debug_or_fallback_tokens(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "gh"
            binary.write_text(
                "#!/usr/bin/python3\nimport json,os,sys\n"
                "assert os.environ['GH_TOKEN']=='synthetic-installation-token'\n"
                "assert 'GITHUB_TOKEN' not in os.environ and 'GH_DEBUG' not in os.environ\n"
                "assert 'CATALOG_GITHUB_TOKEN' not in os.environ\n"
                "assert 'synthetic-installation-token' not in ' '.join(sys.argv)\n"
                "assert sys.argv[1:6]==['api','--hostname','github.com','--method','POST']\n"
                "assert sys.argv[-2:]==['--input','-']\n"
                "print(json.dumps({'received':json.load(sys.stdin)}))\n"
            )
            binary.chmod(0o700)
            with patch.dict(
                os.environ,
                {
                    "PATH": temporary,
                    "GH_TOKEN": "synthetic-installation-token",
                    "GITHUB_TOKEN": "synthetic-other-token",
                    "GH_DEBUG": "api",
                    "CATALOG_GITHUB_TOKEN": "synthetic-metadata-token",
                },
            ):
                client = self.client()
                payload = {"ref": "refs/heads/catalog-publish/" + "a" * 64, "sha": "b" * 40}
                self.assertEqual({"received": payload}, client.post(PREFIX + "/git/refs", payload))

    def test_real_process_output_is_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "gh"
            binary.write_text("#!/usr/bin/python3\nprint('x'*65536)\n")
            binary.chmod(0o700)
            with patch.dict(os.environ, {"PATH": temporary}):
                client = self.client()
                with self.assertRaisesRegex(GitHubError, "size limit"):
                    client.read(PREFIX, maximum=64)


if __name__ == "__main__":
    unittest.main()
