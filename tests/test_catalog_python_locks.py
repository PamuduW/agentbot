from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.shared_paths import shared_python_path

sys.path.insert(0, str(shared_python_path(Path(__file__).resolve().parents[1])))
from software_catalog import CatalogError


class PythonLockTests(unittest.TestCase):
    def test_lock_rejects_unpinned_missing_hash_duplicate_and_executable_directives(self):
        from catalog_dependencies import validate_python_lock

        good = b"fixture==1.2.3 \\\n    --hash=sha256:" + b"a" * 64 + b"\n"
        self.assertEqual("1.2.3", validate_python_lock(good)["fixture"]["version"])
        for bad in (
            b"fixture>=1\n",
            b"fixture==1.2.3\n",
            good + good,
            b"--extra-index-url https://example.invalid\n" + good,
            b"fixture @ https://example.invalid/package.whl\n",
            good.replace(b"a" * 64, b"abc"),
            b"",
        ):
            with self.subTest(raw=bad), self.assertRaises(CatalogError):
                validate_python_lock(bad)

    def test_generation_uses_exact_uv_and_keeps_constraints_without_inherited_auth(self):
        from catalog_dependencies import generate_python_lock, validate_python_lock

        with tempfile.TemporaryDirectory() as temporary:
            uv = Path(temporary) / "uv"
            uv.write_text(
                "#!"
                + sys.executable
                + "\n"
                + """
import os,pathlib,sys
if sys.argv[1:] == ['--version']:
    print('uv 0.13.4 (x86_64-unknown-linux-gnu)'); sys.exit(0)
args=sys.argv[1:]
assert args[:2] == ['pip','compile']
for flag in ('--generate-hashes','--only-binary=:all:','--no-config','--no-python-downloads','--no-header','--no-annotate','--upgrade'):
    assert flag in args
assert args[args.index('--python-platform')+1] == 'x86_64-unknown-linux-gnu'
assert args[args.index('--python-version')+1] == '3.12'
assert args[args.index('--index-url')+1] == 'https://pypi.org/simple'
assert 'UV_INDEX_URL' not in os.environ and 'GITHUB_TOKEN' not in os.environ
assert pathlib.Path('requirements.in').read_text() == 'fixture>=1,<2\\n'
pathlib.Path(args[args.index('--output-file')+1]).write_text('fixture==1.2.3 --hash=sha256:'+'a'*64+'\\n')
"""
            )
            uv.chmod(0o700)
            with patch.dict(
                os.environ,
                {"UV_INDEX_URL": "https://example.invalid", "GITHUB_TOKEN": "fixture-only"},
            ):
                raw = generate_python_lock(
                    "fixture>=1,<2\n",
                    uv=uv,
                    uv_version="0.13.4",
                    python_version="3.12",
                    target="linux-amd64",
                )
            self.assertEqual("1.2.3", validate_python_lock(raw)["fixture"]["version"])
            with self.assertRaisesRegex(CatalogError, "tool version"):
                generate_python_lock(
                    "fixture>=1,<2\n",
                    uv=uv,
                    uv_version="0.0.0",
                    python_version="3.12",
                    target="linux-amd64",
                )
            with self.assertRaises(CatalogError):
                generate_python_lock(
                    "-r /etc/passwd\n",
                    uv=uv,
                    uv_version="0.13.4",
                    python_version="3.12",
                    target="linux-amd64",
                )

    def test_product_profiles_share_versions_but_keep_development_packages_out_of_runtime(self):
        from catalog_dependencies import validate_python_lock

        from src.catalog_python_discovery import PythonCandidateResolver

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "requirements.txt").write_text("foo>=1,<2\n")
            (root / "requirements-dev.txt").write_text("bar>=2,<3\nfoo>=1,<2\n")
            uv = root / "uv"
            uv.write_text(
                "#!"
                + sys.executable
                + "\n"
                + """
import pathlib,sys
if sys.argv[1:] == ['--version']:
    print('uv 0.13.4 (x86_64-unknown-linux-gnu)'); sys.exit(0)
args=sys.argv[1:]; requirements=pathlib.Path('requirements.in').read_text()
packages={'foo':'1.5.0','pip':'26.0'}
if 'bar' in requirements:
    packages['bar']='2.2.0'
else:
    assert '--constraint' in args
    constraints=pathlib.Path(args[args.index('--constraint')+1]).read_text()
    assert 'foo==1.5.0' in constraints and 'bar==2.2.0' in constraints
pathlib.Path(args[args.index('--output-file')+1]).write_text(''.join(name+'=='+version+' --hash=sha256:'+'a'*64+'\\n' for name,version in packages.items()))
"""
            )
            uv.chmod(0o700)
            locks = {}
            resolver = PythonCandidateResolver(root, locks, uv=uv, uv_version="0.13.4")
            foo = resolver.resolve("foo", "linux-amd64")
            bar = resolver.resolve("bar", "linux-amd64")
            pip = resolver.resolve("pip", "linux-amd64")
            self.assertEqual("1.5.0", foo["version"])
            self.assertEqual(foo["lock"], pip["lock"])
            self.assertNotEqual(foo["lock"], bar["lock"])
            self.assertEqual({"foo", "pip"}, set(validate_python_lock(locks[foo["lock"]])))
            self.assertEqual({"foo", "pip", "bar"}, set(validate_python_lock(locks[bar["lock"]])))
            with self.assertRaises(CatalogError):
                resolver.resolve("undeclared", "linux-amd64")
            with self.assertRaises(CatalogError):
                resolver.resolve("foo", "wsl2-amd64")
