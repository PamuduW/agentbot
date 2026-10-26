from __future__ import annotations

import io
import json
import unittest
import zipfile
from urllib.parse import urlsplit

from src.catalog_extensions import validate_extension_lock


class ExtensionDiscoveryTests(unittest.TestCase):
    @staticmethod
    def archive(identity, version, requires=(), platform="linux-arm64", prerelease=False):
        publisher, name = identity.split(".", 1)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr(
                "extension/package.json",
                json.dumps(
                    {
                        "publisher": publisher,
                        "name": name,
                        "version": version,
                        "extensionDependencies": list(requires),
                    }
                ),
            )
            archive.writestr(
                "extension.vsixmanifest",
                "<PackageManifest><Metadata><Properties>"
                f'<Property Id="Microsoft.VisualStudio.Code.TargetPlatform" Value="{platform}"/>'
                f'<Property Id="Microsoft.VisualStudio.Code.PreRelease" Value="{str(prerelease).lower()}"/>'
                "</Properties></Metadata></PackageManifest>",
            )
        return stream.getvalue()

    def resolver(self, transport, locks):
        try:
            from src.catalog_extension_discovery import ExtensionCandidateResolver
        except ImportError:
            self.fail("catalog discovery cannot resolve platform-specific VSIX closures")
        return ExtensionCandidateResolver(
            locks,
            download=transport,
            release_resolver=lambda identity, platform: (
                "4.5.6" if identity.endswith(".dependency") else "1.2.3",
                platform,
            ),
        )

    def test_real_vsix_manifests_produce_a_complete_cached_platform_closure(self):
        calls = []
        fixtures = {
            "root": self.archive("publisher.root", "1.2.3", ["publisher.dependency"]),
            "dependency": self.archive("publisher.dependency", "4.5.6"),
        }

        def transport(url, path, *, publisher):
            self.assertEqual("vscode-marketplace", publisher)
            self.assertEqual("targetPlatform=linux-arm64", urlsplit(url).query)
            calls.append(url)
            name = url.split("/vsextensions/", 1)[1].split("/", 1)[0]
            path.write_bytes(fixtures[name])

        locks = {}
        resolver = self.resolver(transport, locks)
        artifact = resolver.resolve("publisher.root", "linux-arm64")
        entries = validate_extension_lock(
            json.loads(locks[artifact["lock"]]), "publisher.root", "1.2.3"
        )
        self.assertEqual({"publisher.root", "publisher.dependency"}, set(entries))
        self.assertEqual("4.5.6", entries["publisher.dependency"]["version"])
        self.assertEqual(2, len(calls))
        self.assertEqual(artifact, resolver.resolve("publisher.root", "wsl2-arm64"))
        self.assertEqual(2, len(calls))
        self.assertFalse(any("/latest/" in entry["url"] for entry in entries.values()))

    def test_wrong_platform_prerelease_cycle_and_changed_exact_bytes_hold_without_lock(self):
        from src.software_versions import CatalogError

        scenarios = [
            (self.archive("publisher.root", "1.2.3", platform="win32-x64"), None),
            (self.archive("publisher.root", "1.2.3", prerelease=True), None),
            (self.archive("publisher.root", "1.2.3", ["publisher.root"]), None),
            (self.archive("publisher.root", "1.2.3"), self.archive("publisher.root", "1.2.4")),
        ]
        for latest, exact in scenarios:

            def transport(url, path, *, publisher, latest=latest, exact=exact):
                path.write_bytes(latest if "/latest/" in url else (exact or latest))

            locks = {}
            resolver = self.resolver(transport, locks)
            with self.subTest(exact=exact is not None), self.assertRaises(CatalogError):
                resolver.resolve("publisher.root", "linux-arm64")
            self.assertEqual({}, locks)

    def test_gallery_metadata_selects_stable_compatible_version_and_refuses_other_identity(self):
        try:
            from src.catalog_extension_discovery import select_extension_release
        except ImportError:
            self.fail("extension discovery has no publisher metadata version selection")
        from src.software_versions import CatalogError

        extension = {
            "publisher": {"publisherName": "publisher"},
            "extensionName": "root",
            "versions": [
                {"version": "9.0.0", "targetPlatform": "win32-x64", "properties": []},
                {
                    "version": "2.0.0",
                    "targetPlatform": "linux-arm64",
                    "properties": [
                        {"key": "Microsoft.VisualStudio.Code.PreRelease", "value": "true"}
                    ],
                },
                {"version": "1.2.3", "properties": []},
                {"version": "1.2.2", "targetPlatform": "linux-arm64", "properties": []},
            ],
        }
        data = {"results": [{"extensions": [extension]}]}
        self.assertEqual(
            ("1.2.3", None), select_extension_release(data, "publisher.root", "linux-arm64")
        )
        extension["extensionName"] = "another"
        with self.assertRaises(CatalogError):
            select_extension_release(data, "publisher.root", "linux-arm64")

    def test_universal_package_uses_its_exact_version_without_a_native_platform_query(self):
        from src.catalog_extension_discovery import ExtensionCandidateResolver

        calls = []
        payload = self.archive("publisher.root", "1.2.3", platform="")

        def transport(url, path, *, publisher):
            calls.append(url)
            self.assertEqual("", urlsplit(url).query)
            path.write_bytes(payload)

        locks = {}
        resolver = ExtensionCandidateResolver(
            locks, download=transport, release_resolver=lambda _identity, _platform: ("1.2.3", None)
        )
        artifact = resolver.resolve("publisher.root", "linux-arm64")
        self.assertEqual("1.2.3", artifact["version"])
        self.assertEqual(1, len(calls))

    def test_shipped_editor_dependency_is_recorded_without_a_marketplace_download(self):
        from src.catalog_extension_discovery import ExtensionCandidateResolver

        payload = self.archive(
            "publisher.root", "1.2.3", ["vscode.github-authentication"], platform=""
        )
        calls = []

        def release(identity, platform):
            calls.append(identity)
            if identity != "publisher.root":
                self.fail("built-in editor dependency was requested from Marketplace")
            return "1.2.3", None

        locks = {}
        artifact = ExtensionCandidateResolver(
            locks,
            download=lambda url, path, **kwargs: path.write_bytes(payload),
            release_resolver=release,
        ).resolve("publisher.root", "linux-amd64")
        lock = json.loads(locks[artifact["lock"]])
        self.assertEqual(2, lock["schema_version"])
        entries = validate_extension_lock(lock, "publisher.root", "1.2.3")
        self.assertEqual({"builtin": True, "requires": []}, entries["vscode.github-authentication"])
        self.assertEqual(["publisher.root"], calls)

    def test_extension_pack_cycle_keeps_exact_closure_and_orders_hard_dependencies(self):
        from src.catalog_extension_discovery import ExtensionCandidateResolver
        from src.catalog_extensions import _closure_order

        def payload(identity, requires=(), pack=()):
            source = io.BytesIO(self.archive(identity, "1.2.3", requires, platform=""))
            destination = io.BytesIO()
            with zipfile.ZipFile(source) as old, zipfile.ZipFile(destination, "w") as new:
                for name in old.namelist():
                    raw = old.read(name)
                    if name == "extension/package.json":
                        manifest = json.loads(raw)
                        manifest["extensionPack"] = list(pack)
                        raw = json.dumps(manifest).encode()
                    new.writestr(name, raw)
            return destination.getvalue()

        fixtures = {
            "publisher.python": payload("publisher.python", pack=["publisher.debugger"]),
            "publisher.debugger": payload("publisher.debugger", requires=["publisher.python"]),
        }
        locks = {}
        artifact = ExtensionCandidateResolver(
            locks,
            release_resolver=lambda *args: ("1.2.3", None),
            download=lambda url, path, **kwargs: path.write_bytes(
                fixtures["publisher." + url.split("/vsextensions/", 1)[1].split("/", 1)[0]]
            ),
        ).resolve("publisher.python", "linux-amd64")
        entries = validate_extension_lock(
            json.loads(locks[artifact["lock"]]), "publisher.python", "1.2.3"
        )
        self.assertEqual(["publisher.debugger"], entries["publisher.python"]["pack"])
        self.assertEqual(
            ("publisher.python", "publisher.debugger"),
            _closure_order(entries, ("publisher.debugger",)),
        )
        self.assertEqual(
            ("publisher.python", "publisher.debugger"),
            _closure_order(entries, ("publisher.python",)),
        )

    def test_every_declared_builtin_remains_in_the_frozen_closure(self):
        from src.catalog_extension_discovery import ExtensionCandidateResolver
        from src.catalog_extensions import BUILTIN_EXTENSIONS

        payload = self.archive("publisher.root", "1.2.3", tuple(BUILTIN_EXTENSIONS), platform="")
        locks = {}
        artifact = ExtensionCandidateResolver(
            locks,
            release_resolver=lambda *args: ("1.2.3", None),
            download=lambda url, path, **kwargs: path.write_bytes(payload),
        ).resolve("publisher.root", "linux-amd64")
        entries = validate_extension_lock(
            json.loads(locks[artifact["lock"]]), "publisher.root", "1.2.3"
        )
        self.assertEqual({"publisher.root", *BUILTIN_EXTENSIONS}, set(entries))
        for identity, requires in BUILTIN_EXTENSIONS.items():
            self.assertEqual(list(requires), entries[identity]["requires"])
        entries["publisher.root"]["requires"] = ["vscode.git"]
        del entries["vscode.git-base"]
        from src.catalog_extensions import ExtensionCatalogError

        with self.assertRaisesRegex(ExtensionCatalogError, "built-in"):
            validate_extension_lock(
                {"schema_version": 2, "extensions": entries}, "publisher.root", "1.2.3"
            )
