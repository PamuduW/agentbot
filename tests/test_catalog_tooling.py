from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class ToolingSmokeTests(unittest.TestCase):
    def test_invalid_context_cannot_start_installation(self):
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts/catalog/qualify-tooling.py"
        self.assertTrue(script.is_file(), "missing real tooling smoke driver")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "evidence.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    str(Path(temporary) / "missing-context"),
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(2, result.returncode, result.stderr)
            self.assertFalse(output.exists())
            self.assertNotIn("Traceback", result.stderr)


class DevelopmentProfileTests(unittest.TestCase):
    def test_development_lock_must_cover_every_selected_python_closure(self):
        script = Path(__file__).resolve().parents[1] / "scripts/catalog/qualify-tooling.py"
        spec = importlib.util.spec_from_file_location("catalog_tooling_smoke", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(
            hasattr(module, "development_profile"), "development closure is not qualified"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            full = (
                b"pip==1 --hash=sha256:"
                + b"a" * 64
                + b"\nruff==2 --hash=sha256:"
                + b"b" * 64
                + b"\n"
            )
            minimal = b"pip==1 --hash=sha256:" + b"a" * 64 + b"\n"
            (root / "full.txt").write_bytes(full)
            (root / "runtime.txt").write_bytes(minimal)
            snapshot = SimpleNamespace(
                directory=root,
                data={
                    "components": {
                        "python.pip": {"owner": "agentbot", "kind": "python-package"},
                        "python.ruff": {"owner": "agentbot", "kind": "python-package"},
                    }
                },
            )
            entries = {"python.pip": {"lock": "runtime.txt"}, "python.ruff": {"lock": "full.txt"}}
            with patch.object(
                module, "resolve_entry", side_effect=lambda snapshot, key, target: entries[key]
            ):
                lock, packages = module.development_profile(snapshot)
                self.assertEqual(root / "full.txt", lock)
                self.assertEqual({"pip", "ruff"}, set(packages))
                for changed in (
                    b"pip==3 --hash=sha256:" + b"a" * 64 + b"\n",
                    b"pip==1 --hash=sha256:" + b"c" * 64 + b"\n",
                    b"extra==1 --hash=sha256:" + b"a" * 64 + b"\n",
                ):
                    (root / "runtime.txt").write_bytes(changed)
                    with self.subTest(changed=changed), self.assertRaises(ValueError):
                        module.development_profile(snapshot)
