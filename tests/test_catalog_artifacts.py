from __future__ import annotations

import gzip
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
from catalog_discovery import DiscoveryError, download_artifact


class PublicArtifactTests(unittest.TestCase):
    def test_http_gzip_is_decoded_before_artifact_hashing_or_zip_inspection(self):
        payload = b"PK\x03\x04public fixture archive"
        response = io.BytesIO(gzip.compress(payload))
        response.headers = {"Content-Encoding": "gzip"}
        opener = Mock()
        opener.open.return_value = response
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("urllib.request.build_opener", return_value=opener),
        ):
            output = Path(temporary) / "package"
            download_artifact(
                "https://github.com/fixture/repo/releases/download/v1.2.3/package", output
            )
            self.assertEqual(payload, output.read_bytes())

    def test_unsupported_content_encoding_leaves_no_candidate_bytes(self):
        response = io.BytesIO(b"unsupported transport encoding")
        response.headers = {"Content-Encoding": "br"}
        opener = Mock()
        opener.open.return_value = response
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("urllib.request.build_opener", return_value=opener),
        ):
            output = Path(temporary) / "package"
            with self.assertRaises(DiscoveryError):
                download_artifact(
                    "https://github.com/fixture/repo/releases/download/v1.2.3/package", output
                )
            self.assertFalse(output.exists())
