"""The saved GitLab read credential.

A token store is worth testing for what it refuses, not what it stores: the
failure that matters is a secret written world-readable, a symlink followed out
of the private directory, or a value echoed somewhere it can be read back.
"""

from __future__ import annotations

import stat
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from src import gitlab_token as store

_TOKEN = "glpat-abcdefghijklmnopqrstuvwx"
_OTHER = "glpat-zyxwvutsrqponmlkjihgfed"


class ShapeTests(unittest.TestCase):
    def test_a_prefixed_token_is_accepted(self) -> None:
        self.assertTrue(store.is_valid(_TOKEN))

    def test_a_truncated_paste_is_refused(self) -> None:
        """The failure this catches is silent: a short token saves, then fails
        later against a live endpoint with nothing pointing back to the paste."""
        self.assertFalse(store.is_valid("glpat-short"))
        self.assertFalse(store.is_valid(""))

    def test_a_routable_token_with_dots_is_accepted(self) -> None:
        """The refusal this catches was real: GitLab's newer routable tokens
        embed a period-separated payload, and a character class without the dot
        rejected a valid credential while reporting only "invalid"."""
        for value in (
            "glpat-AABBCCDDEEFFGG.01.1a2b3c4d5e6f7g8h9i",
            "AABBCCDDEEFFGG.01.1a2b3c4d5e6f7g8h9i0j",
        ):
            with self.subTest(value=value[:12]):
                self.assertTrue(store.is_valid(value))

    def test_a_refusal_says_what_the_rule_is_without_echoing_the_value(self) -> None:
        with self.assertRaises(store.TokenError) as caught:
            store.write(Path("/nonexistent"), "glpat-short")

        message = str(caught.exception)
        self.assertIn("at least 20 characters", message)
        self.assertNotIn("glpat-short", message)

    def test_a_value_carrying_shell_or_newline_characters_is_refused(self) -> None:
        for value in (f"{_TOKEN} extra", f"{_TOKEN}\nGITLAB=x", f"{_TOKEN};id", "a" * 19):
            with self.subTest(value=value):
                self.assertFalse(store.is_valid(value))

    def test_the_fingerprint_identifies_without_reconstructing(self) -> None:
        printed = store.fingerprint(_TOKEN)

        self.assertNotIn(_TOKEN, printed)
        self.assertIn(_TOKEN[-4:], printed)
        self.assertNotEqual(printed, store.fingerprint(_OTHER))

    def test_an_invalid_token_has_no_fingerprint(self) -> None:
        with self.assertRaises(store.TokenError):
            store.fingerprint("glpat-short")


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config_home = Path(self.temporary.name) / "agentbot"

    def test_a_saved_token_round_trips(self) -> None:
        store.write(self.config_home, _TOKEN)

        self.assertEqual(_TOKEN, store.read(self.config_home))
        self.assertIsNone(store.inspect(self.config_home))

    def test_the_secret_is_never_world_readable(self) -> None:
        path = store.write(self.config_home, _TOKEN)

        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(path.parent.stat().st_mode))

    def test_the_file_holds_one_assignment_under_the_exported_name(self) -> None:
        """The key matches the variable the facade and its admission test read,
        so a saved credential can be exported without translation."""
        path = store.write(self.config_home, _TOKEN)

        self.assertEqual([f"{store.TOKEN_KEY}={_TOKEN}"], path.read_text().splitlines())

    def test_replacing_a_token_leaves_no_temporary_behind(self) -> None:
        store.write(self.config_home, _TOKEN)
        store.write(self.config_home, _OTHER)

        self.assertEqual(_OTHER, store.read(self.config_home))
        self.assertEqual(["gitlab.env"], sorted(child.name for child in self.config_home.iterdir()))

    def test_an_invalid_token_is_never_written(self) -> None:
        with self.assertRaises(store.TokenError):
            store.write(self.config_home, "glpat-short")

        self.assertFalse(store.token_file(self.config_home).exists())

    def test_a_loosened_file_is_not_read_back(self) -> None:
        """Mode 600 is the contract. A file something else widened is not a
        saved token any more, and returning it would hand back a secret the
        machine has already exposed."""
        path = store.write(self.config_home, _TOKEN)
        path.chmod(0o644)

        self.assertEqual("", store.read(self.config_home))
        self.assertIn("mode 600", str(store.inspect(self.config_home)))

    def test_a_loosened_directory_is_not_read_back(self) -> None:
        path = store.write(self.config_home, _TOKEN)
        path.parent.chmod(0o755)

        self.assertEqual("", store.read(self.config_home))
        self.assertIn("700", str(store.inspect(self.config_home)))

    def test_a_symlink_is_neither_read_nor_removed(self) -> None:
        """Following it would read, or delete, whatever it points at."""
        self.config_home.mkdir(parents=True)
        self.config_home.chmod(0o700)
        elsewhere = Path(self.temporary.name) / "elsewhere.env"
        elsewhere.write_text(f"{store.TOKEN_KEY}={_TOKEN}\n", encoding="utf-8")
        store.token_file(self.config_home).symlink_to(elsewhere)

        self.assertEqual("", store.read(self.config_home))
        with self.assertRaises(store.TokenError):
            store.remove(self.config_home)
        self.assertTrue(elsewhere.exists())

    def test_extra_lines_are_refused(self) -> None:
        self.config_home.mkdir(parents=True)
        self.config_home.chmod(0o700)
        path = store.token_file(self.config_home)
        path.write_text(f"{store.TOKEN_KEY}={_TOKEN}\nSOMETHING=else\n", encoding="utf-8")
        path.chmod(0o600)

        self.assertEqual("", store.read(self.config_home))
        self.assertIn("exactly one assignment", str(store.inspect(self.config_home)))

    def test_a_foreign_key_is_refused(self) -> None:
        self.config_home.mkdir(parents=True)
        self.config_home.chmod(0o700)
        path = store.token_file(self.config_home)
        path.write_text(f"GITHUB_TOKEN={_TOKEN}\n", encoding="utf-8")
        path.chmod(0o600)

        self.assertEqual("", store.read(self.config_home))
        self.assertIn("invalid key", str(store.inspect(self.config_home)))

    def test_removing_reports_whether_anything_was_there(self) -> None:
        self.assertFalse(store.remove(self.config_home))
        store.write(self.config_home, _TOKEN)

        self.assertTrue(store.remove(self.config_home))
        self.assertEqual("", store.read(self.config_home))

    def test_an_absent_store_reads_empty_without_complaining(self) -> None:
        self.assertEqual("", store.read(self.config_home))
        self.assertIsNone(store.inspect(self.config_home))


class VerifyTests(unittest.TestCase):
    """Three outcomes, and the middle one matters most: being unable to ask is
    not a refusal, and a caller that treats it as one discards a good token."""

    def test_the_token_travels_in_a_header_not_a_url(self) -> None:
        captured: dict[str, object] = {}

        def fake_get(url: str, token: str) -> object:
            captured["url"] = url
            captured["token"] = token
            return {"username": "someone"}

        with mock.patch.object(store, "_get", side_effect=fake_get):
            result = store.verify(_TOKEN)

        self.assertTrue(result.accepted)
        self.assertNotIn(_TOKEN, str(captured["url"]))

    def test_a_rejection_is_definitive(self) -> None:
        error = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)  # type: ignore[arg-type]
        with mock.patch.object(store, "_get", side_effect=error):
            result = store.verify(_TOKEN)

        self.assertIs(False, result.accepted)

    def test_being_unable_to_ask_is_not_a_rejection(self) -> None:
        with mock.patch.object(store, "_get", side_effect=urllib.error.URLError("offline")):
            result = store.verify(_TOKEN)

        self.assertIsNone(result.accepted)

    def test_an_invalid_token_is_refused_without_a_request(self) -> None:
        with mock.patch.object(store, "_get", side_effect=AssertionError("must not be called")):
            result = store.verify("glpat-short")

        self.assertIs(False, result.accepted)

    def test_a_write_scope_is_reported_rather_than_passed_silently(self) -> None:
        """GitLab accepts an `api` token happily. It is still the wrong
        credential for a read-only facade."""
        responses = [{"username": "someone"}, {"scopes": ["api", "read_api"]}]
        with mock.patch.object(store, "_get", side_effect=responses):
            result = store.verify(_TOKEN)

        self.assertTrue(result.accepted)
        self.assertFalse(result.read_only)

    def test_read_only_scopes_are_recognised(self) -> None:
        responses = [{"username": "someone"}, {"scopes": ["read_api"]}]
        with mock.patch.object(store, "_get", side_effect=responses):
            result = store.verify(_TOKEN)

        self.assertTrue(result.read_only)

    def test_a_project_token_without_self_inspection_still_passes(self) -> None:
        """Project and group tokens answer 404 on the personal-token endpoint
        while reading perfectly, so its absence must not fail the check."""
        error = urllib.error.HTTPError("u", 404, "Not Found", {}, None)  # type: ignore[arg-type]
        with mock.patch.object(store, "_get", side_effect=[{"username": "bot"}, error]):
            result = store.verify(_TOKEN)

        self.assertTrue(result.accepted)
        self.assertEqual((), result.scopes)
        self.assertFalse(result.read_only)


if __name__ == "__main__":
    unittest.main()


class CommandTests(unittest.TestCase):
    """The surface the Token Config screen drives.

    The screen shells out for every action, so these are the contract: what is
    printed, what exit status the screen branches on, and -- the one that
    matters -- that the secret arrives on stdin rather than in an argument
    vector, where /proc publishes it to every user on the machine.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_home = self.root / "config" / "agentbot"

    #: What `set` sees when it checks before writing. Stubbed by default so no
    #: test reaches the network -- `set` verifies now, and an unstubbed suite
    #: both hangs on DNS and depends on GitLab being up.
    VERIFY = store.VerifyResult(True, "accepted for someone", ("read_api",))

    def _run(
        self,
        *argv: str,
        stdin: str = "",
        verify: store.VerifyResult | None = None,
    ) -> tuple[int, str, str]:
        import io

        from tests.support import run_cli_main

        with (
            mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": str(self.root / "config")}),
            mock.patch("sys.stdin", new=io.StringIO(stdin)),
            mock.patch.object(store, "verify", return_value=verify or self.VERIFY),
        ):
            return run_cli_main(["agentbot", "--root", str(Path.cwd()), *argv])

    def test_status_reports_absence_as_a_nonzero_status(self) -> None:
        rc, stdout, _ = self._run("gitlab-token", "status")

        self.assertEqual(1, rc)
        self.assertIn("not configured", stdout)

    def test_set_reads_the_secret_from_stdin_and_prints_only_a_fingerprint(self) -> None:
        rc, stdout, _ = self._run("gitlab-token", "set", stdin=f"{_TOKEN}\n")

        self.assertEqual(0, rc)
        self.assertNotIn(_TOKEN, stdout)
        self.assertIn(_TOKEN[-4:], stdout)
        self.assertEqual(_TOKEN, store.read(self.config_home))

    def test_a_pasted_token_with_surrounding_whitespace_is_accepted(self) -> None:
        """A terminal paste carries a newline and sometimes a trailing space.
        Refusing those gives the operator a shape error with nothing to see."""
        rc, _, _ = self._run("gitlab-token", "set", stdin=f"  {_TOKEN}  \n")

        self.assertEqual(0, rc)
        self.assertEqual(_TOKEN, store.read(self.config_home))

    def test_set_refuses_an_invalid_token_and_saves_nothing(self) -> None:
        rc, _, stderr = self._run("gitlab-token", "set", stdin="glpat-short\n")

        self.assertEqual(1, rc)
        self.assertIn("invalid", stderr)
        self.assertEqual("", store.read(self.config_home))

    def test_set_refuses_empty_input(self) -> None:
        rc, _, stderr = self._run("gitlab-token", "set", stdin="\n")

        self.assertEqual(1, rc)
        self.assertIn("No token", stderr)

    def test_status_shows_a_fingerprint_once_saved(self) -> None:
        self._run("gitlab-token", "set", stdin=f"{_TOKEN}\n")

        rc, stdout, _ = self._run("gitlab-token", "status")

        self.assertEqual(0, rc)
        self.assertNotIn(_TOKEN, stdout)
        self.assertIn("sha256:", stdout)

    def test_status_explains_unusable_saved_state(self) -> None:
        path = store.write(self.config_home, _TOKEN)
        path.chmod(0o644)

        rc, stdout, _ = self._run("gitlab-token", "status")

        self.assertEqual(1, rc)
        self.assertIn("unusable", stdout)

    def test_set_refuses_a_token_gitlab_rejects(self) -> None:
        """The guarantee the GitHub screen already made: a credential the
        provider refuses is saved by nobody."""
        rejected = store.VerifyResult(False, "GitLab rejected the token (HTTP 401)")

        rc, _, stderr = self._run("gitlab-token", "set", stdin=f"{_TOKEN}\n", verify=rejected)

        self.assertEqual(1, rc)
        self.assertIn("nothing was saved", stderr)
        self.assertEqual("", store.read(self.config_home))

    def test_set_saves_when_the_check_cannot_be_made(self) -> None:
        """Being unable to ask is not a refusal. Discarding a good token
        because the network was down would be its own defect."""
        unknown = store.VerifyResult(None, "could not reach GitLab: URLError")

        rc, stdout, _ = self._run("gitlab-token", "set", stdin=f"{_TOKEN}\n", verify=unknown)

        self.assertEqual(0, rc)
        self.assertIn("without a check", stdout)
        self.assertEqual(_TOKEN, store.read(self.config_home))

    def test_set_warns_when_the_token_can_write(self) -> None:
        writable = store.VerifyResult(True, "accepted", ("api",))

        rc, stdout, _ = self._run("gitlab-token", "set", stdin=f"{_TOKEN}\n", verify=writable)

        self.assertEqual(0, rc)
        self.assertIn("write scope", stdout)

    def test_check_separates_rejection_from_being_unable_to_ask(self) -> None:
        self._run("gitlab-token", "set", stdin=f"{_TOKEN}\n")
        rejected = store.VerifyResult(False, "GitLab rejected the token (HTTP 401)")
        unknown = store.VerifyResult(None, "could not reach GitLab: URLError")

        self.assertEqual(1, self._run("gitlab-token", "check", verify=rejected)[0])
        self.assertEqual(2, self._run("gitlab-token", "check", verify=unknown)[0])

    def test_check_warns_when_the_token_can_write(self) -> None:
        self._run("gitlab-token", "set", stdin=f"{_TOKEN}\n")
        writable = store.VerifyResult(True, "accepted for someone", ("api",))

        rc, stdout, _ = self._run("gitlab-token", "check", verify=writable)

        self.assertEqual(0, rc)
        self.assertIn("write scope", stdout)

    def test_check_reports_read_only_scopes_without_warning(self) -> None:
        self._run("gitlab-token", "set", stdin=f"{_TOKEN}\n")
        good = store.VerifyResult(True, "accepted for someone", ("read_api",))

        rc, stdout, _ = self._run("gitlab-token", "check", verify=good)

        self.assertEqual(0, rc)
        self.assertIn("read_api", stdout)
        self.assertNotIn("write scope", stdout)

    def test_check_and_reveal_need_a_saved_token(self) -> None:
        self.assertEqual(1, self._run("gitlab-token", "check")[0])
        self.assertEqual(1, self._run("gitlab-token", "reveal")[0])

    def test_reveal_prints_the_token_only_when_asked(self) -> None:
        self._run("gitlab-token", "set", stdin=f"{_TOKEN}\n")

        rc, stdout, _ = self._run("gitlab-token", "reveal")

        self.assertEqual(0, rc)
        self.assertIn(_TOKEN, stdout)

    def test_remove_is_idempotent_and_says_which_happened(self) -> None:
        self._run("gitlab-token", "set", stdin=f"{_TOKEN}\n")

        first = self._run("gitlab-token", "remove")
        second = self._run("gitlab-token", "remove")

        self.assertEqual(0, first[0])
        self.assertIn("removed", first[1])
        self.assertEqual(0, second[0])
        self.assertIn("No saved token file", second[1])

    def test_remove_reports_an_unsafe_destination_rather_than_following_it(self) -> None:
        self.config_home.mkdir(parents=True)
        self.config_home.chmod(0o700)
        elsewhere = self.root / "elsewhere.env"
        elsewhere.write_text("x", encoding="utf-8")
        store.token_file(self.config_home).symlink_to(elsewhere)

        rc, _, stderr = self._run("gitlab-token", "remove")

        self.assertEqual(1, rc)
        self.assertIn("symlink", stderr)
        self.assertTrue(elsewhere.exists())
