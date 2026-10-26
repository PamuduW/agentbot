from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


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
