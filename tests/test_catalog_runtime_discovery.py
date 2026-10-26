from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

from src.shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
from catalog_discovery import DiscoveryError


class RuntimeDiscoveryTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[2] / "dotfiles/scripts/catalog/runtime_discovery.py"
        self.assertTrue(path.is_file(), "Dotfiles has no native runtime publisher resolver")
        spec = importlib.util.spec_from_file_location("dotfiles_runtime_discovery", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_node_selects_newest_lts_and_exact_publisher_checksum(self):
        index = b'[{"version":"v26.1.0","lts":false,"files":["linux-x64"]},{"version":"v24.2.0","lts":"Krypton","files":["linux-x64"]},{"version":"v24.10.0","lts":"Krypton","files":["linux-x64"]}]'

        def fetch(url):
            if url == "https://nodejs.org/dist/index.json":
                return index
            self.assertEqual("https://nodejs.org/dist/v24.10.0/SHASUMS256.txt", url)
            return b"a" * 64 + b"  node-v24.10.0-linux-x64.tar.xz\n"

        result = self.module.RuntimeCandidateResolver(fetch=fetch).resolve(
            "nodejs", {"provider": "node-lts", "locator": "nodejs.org"}, "linux-amd64"
        )
        self.assertEqual(
            {
                "version": "24.10.0",
                "url": "https://nodejs.org/dist/v24.10.0/node-v24.10.0-linux-x64.tar.xz",
                "digest": "sha256:" + "a" * 64,
                "archive": "tar.xz",
                "binary": "node-v24.10.0-linux-x64/bin/node",
            },
            result,
        )
        with self.assertRaises(DiscoveryError):
            self.module.RuntimeCandidateResolver(
                fetch=lambda url: index if url.endswith("index.json") else b"no checksum"
            ).resolve("nodejs", {"provider": "node-lts", "locator": "nodejs.org"}, "linux-amd64")

    def test_go_requires_matching_stable_archive_identity(self):
        import json

        metadata = [
            {
                "version": "go1.27.3",
                "stable": True,
                "files": [
                    {
                        "filename": "go1.27.3.linux-amd64.tar.gz",
                        "os": "linux",
                        "arch": "amd64",
                        "version": "go1.27.3",
                        "sha256": "b" * 64,
                        "kind": "archive",
                    }
                ],
            }
        ]
        resolver = self.module.RuntimeCandidateResolver(
            fetch=lambda url: json.dumps(metadata).encode()
        )
        result = resolver.resolve(
            "go", {"provider": "go-stable", "locator": "go.dev"}, "linux-amd64"
        )
        self.assertEqual("1.27.3", result["version"])
        self.assertEqual("https://go.dev/dl/go1.27.3.linux-amd64.tar.gz", result["url"])
        self.assertEqual("sha256:" + "b" * 64, result["digest"])
        metadata[0]["files"][0]["version"] = "go1.27.2"
        with self.assertRaises(DiscoveryError):
            resolver.resolve("go", {"provider": "go-stable", "locator": "go.dev"}, "linux-amd64")
        with self.assertRaises(DiscoveryError):
            resolver.resolve(
                "go", {"provider": "go-stable", "locator": "unrelated.invalid"}, "linux-amd64"
            )

    def test_chrome_uses_stable_headless_bundle_and_hashes_only_canonical_artifact(self):
        import hashlib
        import json

        version = "156.0.8078.47"
        url = f"https://storage.googleapis.com/chrome-for-testing-public/{version}/linux64/chrome-headless-shell-linux64.zip"
        document = {
            "channels": {
                "Stable": {
                    "channel": "Stable",
                    "version": version,
                    "downloads": {"chrome-headless-shell": [{"platform": "linux64", "url": url}]},
                }
            }
        }

        def checksum(selected):
            if selected != url:
                self.fail("Chrome hashed an unrelated publisher artifact")
            return hashlib.sha256(b"fixture archive").hexdigest()

        resolver = self.module.RuntimeCandidateResolver(
            fetch=lambda selected: json.dumps(document).encode(), checksum=checksum
        )
        result = resolver.resolve(
            "chrome_headless",
            {"provider": "chrome-for-testing", "locator": "Stable"},
            "linux-amd64",
        )
        self.assertEqual(version, result["version"])
        self.assertEqual("chrome-headless-shell-linux64/chrome-headless-shell", result["binary"])
        self.assertEqual(
            "sha256:" + hashlib.sha256(b"fixture archive").hexdigest(), result["digest"]
        )
        document["channels"]["Stable"]["downloads"]["chrome-headless-shell"][0]["url"] = (
            "https://example.invalid/chrome.zip"
        )
        with self.assertRaises(DiscoveryError):
            resolver.resolve(
                "chrome_headless",
                {"provider": "chrome-for-testing", "locator": "Stable"},
                "linux-amd64",
            )

    def test_vendor_agents_bind_exact_version_and_platform_checksums(self):
        import json

        manifest_host = "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/linux_amd64.json"
        agy = {
            "version": "1.2.3",
            "url": "https://storage.googleapis.com/antigravity-public/releases/1.2.3/linux_amd64.tar.gz",
            "sha512": "c" * 128,
        }

        def fetch(url):
            if url == manifest_host:
                return json.dumps(agy).encode()
            if url == "https://downloads.claude.ai/claude-code-releases/stable":
                return b"2.1.211\n"
            if url == "https://downloads.claude.ai/claude-code-releases/2.1.211/manifest.json":
                return json.dumps(
                    {"version": "2.1.211", "platforms": {"linux-x64": {"checksum": "d" * 64}}}
                ).encode()
            self.fail("vendor metadata used an unexpected endpoint: " + url)

        resolver = self.module.RuntimeCandidateResolver(fetch=fetch)
        claude = resolver.resolve(
            "claude_cli",
            {
                "provider": "claude-native",
                "locator": "stable",
                "auto_update_env": "DISABLE_UPDATES",
                "auto_update_value": "1",
            },
            "linux-amd64",
        )
        self.assertEqual(
            "https://downloads.claude.ai/claude-code-releases/2.1.211/linux-x64/claude",
            claude["url"],
        )
        self.assertEqual("sha256:" + "d" * 64, claude["digest"])
        result = resolver.resolve(
            "antigravity_cli",
            {
                "provider": "antigravity-manifest",
                "locator": "linux_amd64",
                "auto_update_env": "AGY_CLI_DISABLE_AUTO_UPDATE",
                "auto_update_value": "true",
            },
            "linux-amd64",
        )
        self.assertEqual("sha512:" + "c" * 128, result["digest"])
        self.assertEqual("antigravity", result["binary"])
        agy["url"] = "https://storage.googleapis.com/unrelated/agent.tar.gz"
        with self.assertRaises(DiscoveryError):
            resolver.resolve(
                "antigravity_cli",
                {
                    "provider": "antigravity-manifest",
                    "locator": "linux_amd64",
                    "auto_update_env": "AGY_CLI_DISABLE_AUTO_UPDATE",
                    "auto_update_value": "true",
                },
                "linux-amd64",
            )

    def test_public_metadata_decodes_gzip_before_applying_its_content_limit(self):
        import gzip
        import io
        from types import SimpleNamespace
        from unittest.mock import patch

        raw = b'{"version":"1.2.3"}'
        response = io.BytesIO(gzip.compress(raw))
        response.url = "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/linux_amd64.json"
        response.headers = {"Content-Encoding": "gzip"}
        with patch.object(
            self.module.urllib.request,
            "build_opener",
            return_value=SimpleNamespace(open=lambda request, timeout: response),
        ):
            self.assertEqual(raw, self.module.fetch_public(response.url))

    def test_graphify_candidate_uses_frozen_python_closure_with_selected_uv(self):
        import hashlib
        from unittest.mock import patch

        raw = (
            b"graphifyy==1.2.3 --hash=sha256:"
            + b"a" * 64
            + b"\nleaf==2.0.0 --hash=sha256:"
            + b"b" * 64
            + b"\n"
        )
        locks = {}
        resolver = self.module.RuntimeCandidateResolver(
            uv=Path("/selected/uv"), uv_version="0.13.4", locks=locks
        )
        with patch("catalog_dependencies.generate_python_lock", return_value=raw):
            result = resolver.resolve(
                "graphify_cli",
                {"provider": "pypi-tool", "locator": "graphifyy", "binary": "graphify"},
                "linux-amd64",
            )
        self.assertEqual("1.2.3", result["version"])
        self.assertEqual("python-lock", result["archive"])
        self.assertEqual("graphify", result["binary"])
        self.assertEqual(raw, locks[result["lock"]])
        self.assertEqual("sha256:" + hashlib.sha256(raw).hexdigest(), result["digest"])
        with self.assertRaises(DiscoveryError):
            self.module.RuntimeCandidateResolver().resolve(
                "graphify_cli",
                {"provider": "pypi-tool", "locator": "graphifyy", "binary": "graphify"},
                "linux-amd64",
            )

    def test_docker_apt_uses_debian_version_order_and_package_architecture_hash(self):
        import gzip

        def stanza(version, filename, digest):
            return f"Package: docker-ce\nVersion: {version}\nArchitecture: amd64\nFilename: dists/noble/pool/stable/amd64/{filename}\nSHA256: {digest}\n\n"

        raw = stanza(
            "5:29.0.0-1~ubuntu.24.04~noble",
            "docker-ce_29.0.0-1~ubuntu.24.04~noble_amd64.deb",
            "a" * 64,
        ) + stanza(
            "6:1.0.0-1~ubuntu.24.04~noble",
            "docker-ce_1.0.0-1~ubuntu.24.04~noble_amd64.deb",
            "b" * 64,
        )
        resolver = self.module.RuntimeCandidateResolver(
            fetch=lambda url: gzip.compress(raw.encode())
        )
        result = resolver.resolve(
            "docker.docker-ce",
            {"provider": "docker-apt", "locator": "docker-ce", "channel": "noble"},
            "linux-amd64",
        )
        self.assertEqual("6:1.0.0-1~ubuntu.24.04~noble", result["version"])
        self.assertEqual("docker-ce", result["package"])
        self.assertEqual("sha256:" + "b" * 64, result["digest"])
        self.assertEqual("deb", result["archive"])
        self.assertEqual(
            "https://download.docker.com/linux/ubuntu/dists/noble/pool/stable/amd64/docker-ce_1.0.0-1~ubuntu.24.04~noble_amd64.deb",
            result["url"],
        )
        with self.assertRaises(DiscoveryError):
            self.module.RuntimeCandidateResolver(
                fetch=lambda url: gzip.compress(
                    raw.replace("Architecture: amd64", "Architecture: arm64").encode()
                )
            ).resolve(
                "docker.docker-ce",
                {"provider": "docker-apt", "locator": "docker-ce", "channel": "noble"},
                "linux-amd64",
            )

    def test_portainer_lts_binds_linux_digest_to_exact_release_tag(self):
        import json

        digest = "sha256:" + "a" * 64
        lts = {
            "name": "lts",
            "images": [
                {"architecture": "arm64", "os": "linux", "digest": "sha256:" + "b" * 64},
                {"architecture": "amd64", "os": "linux", "digest": digest},
            ],
        }
        tags = {
            "next": None,
            "results": [
                {"name": "2.33.5", "images": lts["images"]},
                {
                    "name": "2.34.0",
                    "images": [
                        {"architecture": "amd64", "os": "linux", "digest": "sha256:" + "c" * 64}
                    ],
                },
            ],
        }

        def fetch(url):
            if url == "https://hub.docker.com/v2/repositories/portainer/portainer-ce/tags/lts":
                return json.dumps(lts).encode()
            self.assertEqual(
                "https://hub.docker.com/v2/repositories/portainer/portainer-ce/tags?page_size=100&page=1&ordering=last_updated",
                url,
            )
            return json.dumps(tags).encode()

        result = self.module.RuntimeCandidateResolver(fetch=fetch).resolve(
            "portainer",
            {"provider": "portainer-lts", "locator": "portainer/portainer-ce"},
            "linux-amd64",
        )
        self.assertEqual("2.33.5", result["version"])
        self.assertEqual("portainer/portainer-ce", result["package"])
        self.assertEqual(digest, result["digest"])
        tags["results"] = []
        with self.assertRaises(DiscoveryError):
            self.module.RuntimeCandidateResolver(fetch=fetch).resolve(
                "portainer",
                {"provider": "portainer-lts", "locator": "portainer/portainer-ce"},
                "linux-amd64",
            )
