from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
import catalog_discovery


class DebMetadataTests(unittest.TestCase):
    def test_catalog_uses_actual_debian_version_package_and_architecture(self):
        self.assertTrue(
            hasattr(catalog_discovery, "inspect_deb"),
            "release discovery does not inspect exact Debian metadata",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            control = root / "package/DEBIAN/control"
            control.parent.mkdir(parents=True)
            control.write_text(
                "Package: powershell\nVersion: 7.6.3-1~ubuntu.24.04\nArchitecture: amd64\nMaintainer: Fixture <fixture@example.invalid>\nDescription: qualification fixture\n"
            )
            archive = root / "package.deb"
            subprocess.run(
                [
                    "dpkg-deb",
                    "--build",
                    "--root-owner-group",
                    str(control.parent.parent),
                    str(archive),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            artifact = {
                "version": "7.6.3",
                "url": "https://github.com/PowerShell/PowerShell/releases/download/v7.6.3/package.deb",
                "digest": "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest(),
                "archive": "deb",
            }

            def download(_url, path):
                shutil.copyfile(archive, path)

            result = catalog_discovery.inspect_deb(
                artifact, "powershell", "linux-amd64", download=download
            )
            self.assertEqual("7.6.3-1~ubuntu.24.04", result["version"])
            self.assertEqual("powershell", result["package"])
            for package, target in (("unrelated", "linux-amd64"), ("powershell", "linux-arm64")):
                with (
                    self.subTest(package=package, target=target),
                    self.assertRaises(catalog_discovery.DiscoveryError),
                ):
                    catalog_discovery.inspect_deb(artifact, package, target, download=download)
            with self.assertRaises(catalog_discovery.DiscoveryError):
                catalog_discovery.inspect_deb(
                    {**artifact, "digest": "sha256:" + "0" * 64},
                    "powershell",
                    "linux-amd64",
                    download=download,
                )
