from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.shared_paths import (
    CONTRACT_REQUIRED,
    SharedCheckoutError,
    shared_python_path,
    shared_root,
)


def _checkout(parent: Path, contract: str = str(CONTRACT_REQUIRED)) -> Path:
    """A directory shaped like a dotfiles-shared checkout."""
    root = parent / "dotfiles-shared"
    (root / "scripts" / "lib" / "shared" / "tui").mkdir(parents=True)
    (root / "scripts" / "lib" / "shared" / "tui" / "colors.sh").write_text("")
    (root / "CONTRACT").write_text(f"{contract}\n")
    return root


class SharedPathsTests(unittest.TestCase):
    """The Python resolver must agree with scripts/lib/shared_resolve.sh.

    Its failure paths are the ones that matter: a consumer that cannot find the
    checkout has to say which one it wanted, because the alternative is dying on
    whichever `source` or import ran first.
    """

    def setUp(self) -> None:
        self._saved = {
            k: os.environ.get(k) for k in ("DOTFILES_SHARED_ROOT", "DOTFILES_SHARED_DIR")
        }
        for key in self._saved:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_sibling_of_the_repository_is_found(self) -> None:
        with TemporaryDirectory() as tmp:
            parent = Path(tmp)
            expected = _checkout(parent)
            self.assertEqual(expected.resolve(), shared_root(parent / "agentbot"))

    def test_environment_override_outranks_the_sibling(self) -> None:
        with TemporaryDirectory() as tmp, TemporaryDirectory() as other:
            parent = Path(tmp)
            _checkout(parent)
            override = _checkout(Path(other))
            os.environ["DOTFILES_SHARED_DIR"] = str(override)
            self.assertEqual(override.resolve(), shared_root(parent / "agentbot"))

    def test_a_directory_that_is_not_a_checkout_is_not_accepted(self) -> None:
        with TemporaryDirectory() as tmp:
            parent = Path(tmp)
            (parent / "dotfiles-shared").mkdir()  # no CONTRACT, no colors.sh
            os.environ["HOME"] = tmp
            with self.assertRaises(SharedCheckoutError) as caught:
                shared_root(parent / "agentbot")
            self.assertIn("no dotfiles-shared checkout found", str(caught.exception))

    def test_a_missing_checkout_names_the_clone_that_fixes_it(self) -> None:
        with TemporaryDirectory() as tmp:
            parent = Path(tmp)
            os.environ["HOME"] = tmp
            with self.assertRaises(SharedCheckoutError) as caught:
                shared_root(parent / "agentbot")
            message = str(caught.exception)
            self.assertIn("git clone", message)
            self.assertIn("dotfiles-shared", message)

    def test_a_shared_checkout_ahead_of_this_one_is_accepted(self) -> None:
        """Ahead is a superset, and rejecting it deadlocked a self-update.

        A revision is raised when a consumer starts needing something the
        shared tree gained, so a shared checkout ahead of this repository has
        everything it loads and more. Refusing that pairing meant a consumer
        older than the raise could not start, so it could not run the gate that
        would have pulled the newer consumer.
        """
        with TemporaryDirectory() as tmp:
            parent = Path(tmp)
            expected = _checkout(parent, contract=str(CONTRACT_REQUIRED + 1))
            self.assertEqual(expected.resolve(), shared_root(parent / "agentbot"))

    def test_a_shared_checkout_behind_this_one_is_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            parent = Path(tmp)
            _checkout(parent, contract=str(CONTRACT_REQUIRED - 1))
            with self.assertRaises(SharedCheckoutError) as caught:
                shared_root(parent / "agentbot")
            message = str(caught.exception)
            self.assertIn(str(CONTRACT_REQUIRED - 1), message)
            self.assertIn(str(CONTRACT_REQUIRED), message)

    def test_an_unreadable_contract_is_a_mismatch_not_a_crash(self) -> None:
        with TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = _checkout(parent, contract="")
            self.assertTrue((root / "CONTRACT").is_file())
            with self.assertRaises(SharedCheckoutError):
                shared_root(parent / "agentbot")

    def test_the_python_directory_sits_under_the_resolved_root(self) -> None:
        with TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = _checkout(parent)
            self.assertEqual(
                root.resolve() / "scripts" / "lib" / "shared" / "python",
                shared_python_path(parent / "agentbot"),
            )


if __name__ == "__main__":
    unittest.main()
