from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

from src.shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
from catalog_discovery import (
    DiscoveryError,
    discover_identity,
    github_artifact,
    reject_replaced_artifact,
)


class DiscoveryTests(unittest.TestCase):
    def test_github_resolves_one_stable_asset_with_publisher_integrity(self) -> None:
        release = {
            "draft": False,
            "prerelease": False,
            "tag_name": "v1.2.3",
            "assets": [
                {
                    "name": "tool-linux-amd64.tar.gz",
                    "digest": "sha256:" + "a" * 64,
                    "browser_download_url": "https://github.com/owner/repo/releases/download/v1.2.3/tool-linux-amd64.tar.gz",
                }
            ],
        }
        fetch = Mock(return_value=release)
        discovery = {
            "provider": "github-release",
            "locator": "owner/repo",
            "asset": "tool-linux-{arch}.tar.gz",
        }
        version, metadata = discover_identity(discovery, fetch=fetch)
        self.assertEqual("1.2.3", version)
        artifact = github_artifact(discovery, metadata, version, "linux-amd64")
        self.assertEqual("sha256:" + "a" * 64, artifact["digest"])
        with self.assertRaises(DiscoveryError):
            github_artifact(discovery, metadata, version, "linux-arm64")
        with self.assertRaises(DiscoveryError):
            reject_replaced_artifact(artifact, {**artifact, "digest": "sha256:" + "b" * 64})
        release["prerelease"] = True
        with self.assertRaises(DiscoveryError):
            discover_identity(discovery, fetch=fetch)

    def test_missing_provider_or_unsafe_source_refuses_before_network(self) -> None:
        fetch = Mock()
        for discovery in (
            {},
            {"provider": "github-source", "locator": "../unsafe"},
            {"provider": "npm", "locator": "https://example.invalid"},
        ):
            with self.assertRaises(DiscoveryError):
                discover_identity(discovery, fetch=fetch)
        fetch.assert_not_called()


class ClosureReplacementTests(unittest.TestCase):
    def test_dependency_only_updates_are_allowed_but_same_version_replacements_are_held(self):
        import json

        import catalog_discovery

        self.assertTrue(
            hasattr(catalog_discovery, "reject_replaced_closure"),
            "candidate locks do not guard same-version dependency replacement",
        )
        for format_name, table, digest_field in (
            ("npm-lock", "packages", "integrity"),
            ("vsix-lock", "extensions", "digest"),
        ):
            previous = {table: {"dependency": {"version": "1.2.3", digest_field: "original"}}}
            updated = {table: {"dependency": {"version": "1.2.4", digest_field: "changed"}}}
            catalog_discovery.reject_replaced_closure(
                json.dumps(previous).encode(), json.dumps(updated).encode(), format_name
            )
            updated[table]["dependency"]["version"] = "1.2.3"
            with self.subTest(format=format_name), self.assertRaises(DiscoveryError):
                catalog_discovery.reject_replaced_closure(
                    json.dumps(previous).encode(), json.dumps(updated).encode(), format_name
                )

    def test_artifact_redirects_stay_inside_the_selected_public_publisher(self):
        from urllib.request import Request

        from catalog_discovery import _ArtifactRedirect

        request = Request("https://marketplace.visualstudio.com/public.vsix")
        handler = _ArtifactRedirect("vscode-marketplace")
        for url in (
            "https://publisher.gallerycdn.vsassets.io/public.vsix",
            "https://publisher.gallery.vsassets.io/public.vsix",
        ):
            self.assertEqual(
                url, handler.redirect_request(request, None, 302, "redirect", {}, url).full_url
            )
        for url in (
            "http://publisher.gallerycdn.vsassets.io/public.vsix",
            "https://publisher.gallerycdn.vsassets.io.unrelated.invalid/public.vsix",
            "https://github.com/unrelated/release",
        ):
            with self.subTest(url=url), self.assertRaises(DiscoveryError):
                handler.redirect_request(request, None, 302, "redirect", {}, url)

    def test_builtin_editor_requirements_preserve_leaf_replacement_checks(self):
        import json

        import catalog_discovery

        builtin = {"builtin": True, "requires": []}
        leaf = {"version": "1.2.3", "digest": "sha256:" + "a" * 64}
        lock = {
            "schema_version": 2,
            "extensions": {"vscode.github-authentication": builtin, "publisher.root": leaf},
        }
        raw = json.dumps(lock).encode()
        catalog_discovery.reject_replaced_closure(raw, raw, "vsix-lock")
        lock["extensions"]["publisher.root"]["digest"] = "sha256:" + "b" * 64
        with self.assertRaisesRegex(catalog_discovery.DiscoveryError, "replaced dependency bytes"):
            catalog_discovery.reject_replaced_closure(raw, json.dumps(lock).encode(), "vsix-lock")
        lock["extensions"]["publisher.root"] = leaf
        lock["extensions"]["vscode.github-authentication"] = leaf
        with self.assertRaisesRegex(catalog_discovery.DiscoveryError, "built-in"):
            catalog_discovery.reject_replaced_closure(raw, json.dumps(lock).encode(), "vsix-lock")


class MetadataTransportTests(unittest.TestCase):
    def test_explicit_read_token_stays_on_github_and_cannot_follow_cross_publisher_redirects(self):
        import io
        import os
        from types import SimpleNamespace
        from unittest.mock import patch

        import catalog_discovery

        requests = []

        def open_request(request, timeout):
            requests.append(request)
            response = io.BytesIO(b'{"fixture":true}')
            response.url = request.full_url
            return response

        with (
            patch.dict(os.environ, {"CATALOG_GITHUB_TOKEN": "fixture_only"}),
            patch.object(
                catalog_discovery.urllib.request,
                "build_opener",
                return_value=SimpleNamespace(open=open_request),
            ),
        ):
            catalog_discovery.fetch_metadata("https://api.github.com/repos/fixture/tool")
            catalog_discovery.fetch_metadata("https://registry.npmjs.org/fixture")
        self.assertIsNotNone(requests[0].get_header("Authorization"))
        self.assertIsNone(requests[1].get_header("Authorization"))
        with self.assertRaisesRegex(DiscoveryError, "credential"):
            catalog_discovery._PublisherRedirect().redirect_request(
                requests[0], None, 302, "redirect", {}, "https://registry.npmjs.org/fixture"
            )

    def test_rate_limit_holds_without_repeating_the_refused_request(self):
        import urllib.error
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        import catalog_discovery

        opening = Mock(
            side_effect=urllib.error.HTTPError(
                "https://api.github.com/repos/fixture/tool", 403, "rate limit", {}, None
            )
        )
        with (
            patch.object(
                catalog_discovery.urllib.request,
                "build_opener",
                return_value=SimpleNamespace(open=opening),
            ),
            patch.object(catalog_discovery.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(DiscoveryError, "403"):
                catalog_discovery.fetch_metadata("https://api.github.com/repos/fixture/tool")
        opening.assert_called_once()
        sleep.assert_not_called()


class RelocatedDependencyTests(unittest.TestCase):
    def test_npm_relocation_does_not_hide_replaced_bytes_at_the_same_version(self):
        import json

        import catalog_discovery

        old = {"packages": {"node_modules/foo": {"version": "1.2.3", "integrity": "original"}}}
        changed = {
            "packages": {
                "node_modules/bar/node_modules/foo": {"version": "1.2.3", "integrity": "replaced"}
            }
        }
        with self.assertRaisesRegex(DiscoveryError, "replaced dependency bytes"):
            catalog_discovery.reject_replaced_closure(
                json.dumps(old).encode(), json.dumps(changed).encode(), "npm-lock"
            )
        changed["packages"]["node_modules/bar/node_modules/foo"]["integrity"] = "original"
        catalog_discovery.reject_replaced_closure(
            json.dumps(old).encode(), json.dumps(changed).encode(), "npm-lock"
        )
