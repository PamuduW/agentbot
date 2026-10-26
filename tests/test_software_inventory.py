from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from src.software_inventory import build_discovery, build_inventory, check_coverage


class SoftwareInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[2]
        self.inventory = build_inventory(self.root / "dotfiles", self.root / "agentbot")

    def test_canonical_catalog_covers_the_current_declared_domain(self) -> None:
        """invariant: the catalog covers the live nonempty component inventory."""
        import json

        catalog = json.loads((self.root / "dotfiles-shared/catalog/software.json").read_text())
        check_coverage(self.inventory, catalog["components"])

    def test_catalog_publisher_recipes_match_the_live_product_recipe_domain(self) -> None:
        import json

        catalog = json.loads((self.root / "dotfiles-shared/catalog/software.json").read_text())
        recipes = build_discovery(self.root / "dotfiles", self.inventory)
        self.assertTrue(recipes)
        for key, recipe in recipes.items():
            with self.subTest(key=key):
                self.assertEqual(recipe, catalog["components"][key]["discovery"])

    def test_configuration_and_distro_packages_have_no_invented_version(self) -> None:
        self.assertEqual("configuration", self.inventory["git_identity"]["kind"])
        self.assertEqual("distro", self.inventory["os.git"]["kind"])
        self.assertEqual("group", self.inventory["docker"]["kind"])
        self.assertNotIn("version", self.inventory["git_identity"])

    def test_runtime_scripts_are_git_sources_and_asdf_is_a_release(self) -> None:
        self.assertEqual("git-source", self.inventory["nvm"]["kind"])
        self.assertEqual("git-source", self.inventory["asdf-golang"]["kind"])
        self.assertEqual("software", self.inventory["asdf"]["kind"])

    def test_cursor_is_inventoried_as_operator_managed(self) -> None:
        self.assertEqual("external", self.inventory["cursor_cli"]["kind"])

    def test_npm_discovery_uses_the_bundled_archive_for_bootstrap(self) -> None:
        discovery = build_discovery(self.root / "dotfiles", self.inventory)
        self.assertEqual(
            {
                "provider": "npm",
                "locator": "npm",
                "archive": "tar",
                "binary": "package/bin/npm-cli.js",
            },
            discovery.get("npm"),
        )

    def test_native_manager_and_archive_publishers_have_exact_discovery_recipes(self) -> None:
        discovery = build_discovery(self.root / "dotfiles", self.inventory)
        self.assertEqual("github-release", discovery["nvm"]["provider"])
        self.assertEqual("nvm-sh/nvm", discovery["nvm"]["locator"])
        self.assertEqual("github-source", discovery["asdf-golang"]["provider"])
        expected = {
            "asdf": ("asdf-vm/asdf", "asdf-v{version}-linux-{arch}.tar.gz"),
            "uv": ("astral-sh/uv", "uv-{triple}-unknown-linux-gnu.tar.gz"),
            "boost_cli": ("jfrog/boost", "boost-linux-{arch}.tar.gz"),
            "direnv": ("direnv/direnv", "direnv.linux-{arch}"),
            "lazydocker": (
                "jesseduffield/lazydocker",
                "lazydocker_{version}_Linux_{release_arch}.tar.gz",
            ),
            "obsidian": ("obsidianmd/obsidian-releases", "obsidian_{version}_{arch}.deb"),
            "powershell": ("PowerShell/PowerShell", "powershell_{version}-1.deb_{arch}.deb"),
            "ubuntu_sans_mono_fonts": (
                "canonical/Ubuntu-Sans-Mono-fonts",
                "Ubuntu-Sans-Mono-fonts-v{version}.zip",
            ),
        }
        for key, (repository, asset) in expected.items():
            with self.subTest(key=key):
                self.assertEqual(repository, discovery[key]["locator"])
                self.assertEqual(asset, discovery[key]["asset"])

    def test_inventory_includes_indirect_dependencies_and_selected_integrations(self) -> None:
        for key in (
            "nvm",
            "asdf",
            "uv",
            "npm",
            "skills_cli",
            "python.pyyaml",
            "mcp.github",
            "extension.charliermarsh.ruff",
            "skill-source.superpowers",
        ):
            with self.subTest(key=key):
                self.assertIn(key, self.inventory)

    def test_new_component_is_detected_without_updating_a_copied_test_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dotfiles = Path(temporary) / "dotfiles"
            shutil.copytree(
                self.root / "dotfiles/scripts/lib/components", dotfiles / "scripts/lib/components"
            )
            shutil.copytree(self.root / "dotfiles/packages", dotfiles / "packages")
            shutil.copy(
                self.root / "dotfiles/scripts/lib/managed_tool_state.sh",
                dotfiles / "scripts/lib/managed_tool_state.sh",
            )
            registry = dotfiles / "scripts/lib/components/registry.sh"
            registry.write_text(
                registry.read_text()
                + "\ncomp_define new_fixture_tool \\\n    --label 'fixture' --order 99\n"
            )
            changed = build_inventory(dotfiles, self.root / "agentbot")
            with self.assertRaisesRegex(ValueError, "new_fixture_tool"):
                check_coverage(changed, self.inventory)

    def test_missing_duplicate_owner_or_empty_catalog_fails_coverage(self) -> None:
        altered = dict(self.inventory)
        altered.pop("codex_cli")
        with self.assertRaisesRegex(ValueError, "codex_cli"):
            check_coverage(self.inventory, altered)
        altered = {
            **self.inventory,
            "codex_cli": {**self.inventory["codex_cli"], "owner": "agentbot"},
        }
        with self.assertRaisesRegex(ValueError, "owner"):
            check_coverage(self.inventory, altered)
        with self.assertRaises(ValueError):
            check_coverage({}, {})
