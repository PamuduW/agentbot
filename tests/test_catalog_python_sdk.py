from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.command_runner import CommandResult


class PrivatePythonSDKTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        environment = patch.dict("os.environ", {"XDG_DATA_HOME": str(self.root / "data")})
        environment.start()
        self.addCleanup(environment.stop)
        self.catalog = self.root / "catalog"
        self.catalog.mkdir()
        self.artifact = {
            "version": "3.12.16+20261026",
            "archive": "tar.gz",
            "binary": "python/bin/python3.12",
            "package": "cpython",
            "digest": "sha256:" + "a" * 64,
            "url": "https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.12.16%2B20261026-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
        }
        try:
            self.module = importlib.import_module("src.catalog_python_runtime")
        except ImportError:
            self.fail("private catalog Python SDK is missing")
        self.snapshot = SimpleNamespace(directory=self.catalog, digest="b" * 64, approved=True)
        self.sources = {name: self.root / name for name in ("dotfiles", "dotfiles-shared")}
        self.actions = []

    def test_artifact_identity_covers_every_pinned_field_and_rejects_foreign_builds(self):
        identity = self.module.runtime_identity(self.artifact)
        self.assertRegex(identity, r"^[a-f0-9]{64}$")
        self.assertNotEqual(
            identity,
            self.module.runtime_identity({**self.artifact, "digest": "sha256:" + "c" * 64}),
        )
        for field, value in [
            ("version", "3.14.1+20261026"),
            ("url", "https://example.invalid/python"),
            ("binary", "../python"),
            ("archive", "zip"),
            ("digest", "latest"),
            ("package", "other"),
        ]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.module.runtime_identity({**self.artifact, field: value})

    def test_runtime_location_is_persistent_across_qualification_snapshot_namespaces(self):
        first = self.provision(install=False)
        alternate = self.root / "other/snapshot"
        alternate.mkdir(parents=True)
        self.snapshot = SimpleNamespace(directory=alternate, digest="b" * 64, approved=True)
        self.assertEqual(first, self.provision(install=False))
        self.assertTrue(first.is_relative_to(self.root / "data/software-catalog/python-runtime"))
        self.assertFalse((self.root / "data").exists())

    def test_relative_application_data_cannot_redirect_runtime_installation(self):
        with (
            patch.dict("os.environ", {"XDG_DATA_HOME": "relative-data"}),
            self.assertRaises(ValueError),
        ):
            self.provision(install=False)
        self.assertFalse((self.root / "data").exists())

    def test_payload_fingerprint_ignores_generated_bytecode_but_detects_changes_and_escapes(self):
        payload = self.root / "payload"
        (payload / "python/lib").mkdir(parents=True)
        source = payload / "python/lib/example.py"
        source.write_text("value = 1\n")
        original = self.module.payload_fingerprint(payload)
        (payload / "python/lib/__pycache__").mkdir()
        (payload / "python/lib/__pycache__/example.pyc").write_bytes(b"cache")
        self.assertEqual(original, self.module.payload_fingerprint(payload))
        source.write_text("value = 2\n")
        self.assertNotEqual(original, self.module.payload_fingerprint(payload))
        source.unlink()
        source.symlink_to("/etc/passwd")
        with self.assertRaises(ValueError):
            self.module.payload_fingerprint(payload)

    def provision(self, *, bad_digest=False, fail_probe=False, install=True):
        module, case = self.module, self

        @contextmanager
        def sources(*args, **kwargs):
            case.actions.append("lease")
            yield case.sources

        class Runner:
            def run(self, argv, **kwargs):
                case.actions.append(argv[0])
                if argv[0] == "curl":
                    Path(argv[argv.index("--output") + 1]).write_bytes(b"archive")
                    return CommandResult(0)
                if argv[0] == "bash":
                    payload = Path(argv[-1])
                    (payload / "python/bin").mkdir(parents=True)
                    binary = payload / "python/bin/python3.12"
                    binary.write_text("synthetic SDK fixture\n")
                    binary.chmod(0o700)
                    return CommandResult(0)
                if fail_probe:
                    return CommandResult(31)
                return CommandResult(
                    0,
                    json.dumps(
                        {
                            "version": "3.12.16",
                            "implementation": "cpython",
                            "executable_matches": True,
                        }
                    ),
                )

        import hashlib

        artifact = {**self.artifact, "digest": "sha256:" + hashlib.sha256(b"archive").hexdigest()}
        if bad_digest:
            artifact["digest"] = "sha256:" + "0" * 64
        with (
            patch.object(module, "resolve_entry", return_value=artifact),
            patch.object(module, "lease_catalog_sources", sources),
            patch.object(module, "load_snapshot", return_value=self.snapshot),
        ):
            return module.private_python_runtime(
                self.snapshot, self.root, Runner(), install=install
            )

    def test_fresh_repeat_and_read_only_do_not_use_public_python(self):
        expected = self.provision(install=False)
        self.assertFalse(expected.exists())
        self.assertEqual([], self.actions)
        first = self.provision()
        self.assertEqual(expected, first)
        self.assertTrue(first.is_file())
        self.assertEqual(1, self.actions.count("curl"))
        self.assertEqual(first, self.provision())
        self.assertEqual(1, self.actions.count("curl"))
        first.write_text("tampered\n")
        with self.assertRaises(ValueError):
            self.provision()
        self.assertEqual(1, self.actions.count("curl"))

    def test_download_or_probe_failure_publishes_no_sdk(self):
        for options in ({"bad_digest": True}, {"fail_probe": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.provision(**options)
        for receipt in self.root.rglob("receipt.json"):
            self.fail(f"failed SDK published ownership: {receipt}")

    def test_unowned_payload_is_preserved(self):
        destination = self.provision(install=False)
        destination.parent.mkdir(parents=True)
        destination.write_text("external\n")
        with self.assertRaises(ValueError):
            self.provision()
        self.assertEqual("external\n", destination.read_text())
        self.assertNotIn("curl", self.actions)

    def test_environment_reuse_requires_the_exact_distribution_closure(self):
        module = self.module
        lock = self.catalog / "runtime.txt"
        lock.write_text("pip==25.0 --hash=sha256:" + "a" * 64 + "\n")
        runner = unittest.mock.Mock()
        with patch.object(module, "resolve_entry", return_value={"lock": "runtime.txt"}):
            runner.run.return_value = CommandResult(0, '{"pip":"25.0"}')
            module.verify_python_environment(self.snapshot, self.root / "python", runner)
            command = runner.run.call_args.args[0]
            self.assertEqual(["-I", "-B", "-c"], command[1:4])
            self.assertNotIn("PYTHONPATH", runner.run.call_args.kwargs["env"])
            for changed in ("{}", '{"pip":"24.0"}', '{"pip":"25.0","extra":"1"}', "invalid"):
                runner.run.return_value = CommandResult(0, changed)
                with self.subTest(changed=changed), self.assertRaises(ValueError):
                    module.verify_python_environment(self.snapshot, self.root / "python", runner)

    def test_real_runner_probe_does_not_compare_a_redacted_private_path(self):
        import platform
        import sys

        from src.command_runner import CommandRunner

        binary = Path(sys.executable).resolve()
        with patch.dict("os.environ", {"HOME": str(binary.parent.parent)}):
            self.module._probe(binary, platform.python_version(), CommandRunner())
