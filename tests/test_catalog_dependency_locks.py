from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path

from src.shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
from software_catalog import CatalogError


class DependencyLockTests(unittest.TestCase):
    def test_complete_registry_closure_requires_exact_root_and_sha512_for_every_package(self):
        try:
            from catalog_dependencies import validate_npm_lock
        except ImportError:
            self.fail("candidate discovery has no complete npm dependency-lock validator")
        integrity = "sha512-" + base64.b64encode(b"a" * 64).decode()
        lock = {
            "lockfileVersion": 3,
            "packages": {
                "": {"dependencies": {"cspell": "9.1.0"}},
                "node_modules/cspell": {
                    "version": "9.1.0",
                    "resolved": "https://registry.npmjs.org/cspell/-/cspell-9.1.0.tgz",
                    "integrity": integrity,
                },
                "node_modules/dependency": {
                    "version": "1.0.0",
                    "resolved": "https://registry.npmjs.org/dependency/-/dependency-1.0.0.tgz",
                    "integrity": integrity,
                },
            },
        }
        validate_npm_lock(json.dumps(lock).encode(), "cspell", "9.1.0")
        mutations = [
            lambda value: value["packages"]["node_modules/dependency"].update(
                resolved="git+https://example.invalid/source"
            ),
            lambda value: value["packages"]["node_modules/dependency"].update(
                integrity="sha512-YQ=="
            ),
            lambda value: value["packages"]["node_modules/dependency"].update(link=True),
            lambda value: value["packages"][""].update(dependencies={"cspell": "latest"}),
            lambda value: value["packages"].pop("node_modules/cspell"),
            lambda value: value["packages"].update(
                {"node_modules/../escape": value["packages"]["node_modules/cspell"]}
            ),
        ]
        for mutate in mutations:
            value = json.loads(json.dumps(lock))
            mutate(value)
            with self.subTest(mutate=mutate), self.assertRaises(CatalogError):
                validate_npm_lock(json.dumps(value).encode(), "cspell", "9.1.0")

    def test_lock_generation_isolated_from_user_config_and_requires_selected_tool_versions(self):
        import os
        import tempfile

        try:
            from catalog_dependencies import generate_npm_lock
        except ImportError:
            self.fail("candidate discovery cannot generate a frozen npm lock")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            node = root / "node"
            node.write_text("#!/bin/sh\nprintf 'v24.1.0\\n'\n")
            node.chmod(0o700)
            npm = root / "npm"
            npm.write_text(
                "#!"
                + sys.executable
                + "\n"
                + """
import base64,json,os,pathlib,sys
if sys.argv[1:] == ['--version']:
    print('11.2.0'); sys.exit(0)
assert '--ignore-scripts' in sys.argv and '--package-lock-only' in sys.argv
assert 'NPM_TOKEN' not in os.environ and 'GITHUB_TOKEN' not in os.environ
assert os.environ['NPM_CONFIG_USERCONFIG'] == '/dev/null'
assert os.environ['GIT_ALLOW_PROTOCOL'] == ''
p=pathlib.Path('package.json'); root=json.loads(p.read_text())
package,version=next(iter(root['dependencies'].items()))
lock={'lockfileVersion':3,'packages':{'':{'dependencies':root['dependencies']},'node_modules/'+package:{'version':version,'resolved':'https://registry.npmjs.org/'+package+'/-/'+package+'-'+version+'.tgz','integrity':'sha512-'+base64.b64encode(b'a'*64).decode()}}}
pathlib.Path('package-lock.json').write_text(json.dumps(lock))
"""
            )
            npm.chmod(0o700)
            from unittest.mock import patch

            with patch.dict(
                os.environ, {"NPM_TOKEN": "fixture-only", "GITHUB_TOKEN": "fixture-only"}
            ):
                raw = generate_npm_lock(
                    "cspell",
                    "9.1.0",
                    npm=npm,
                    node=node,
                    npm_version="11.2.0",
                    node_version="24.1.0",
                )
                self.assertEqual(
                    "9.1.0", json.loads(raw)["packages"]["node_modules/cspell"]["version"]
                )
                with self.assertRaisesRegex(CatalogError, "tool version"):
                    generate_npm_lock(
                        "cspell",
                        "9.1.0",
                        npm=npm,
                        node=node,
                        npm_version="99.0.0",
                        node_version="24.1.0",
                    )
