"""Memory Core status and validation against a sanitized schema 3 vault tree.

Every fixture value is fake. SENTINEL marks note bodies and field values that
must never appear in a report.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import memory
from tests.support import run_cli_main

SENTINEL = "zqxsentinelzqx"
ID_A = "47ee7dd9-dc50-4f57-8eda-4ae11045e713"
ID_B = "0b6f3c1e-2d4a-4b8c-9e1f-3a5d7c9b1e2f"
ID_C = "9c2e4a6b-8d0f-4e1a-b3c5-d7e9f1a3b5c7"
ID_D = "5e7a9c1b-3d5f-4a7c-8e9b-1d3f5a7c9e1b"
P_ALPHA = "a1a1a1a1-0000-4000-8000-000000000001"
VAULT_ID = "b2b2b2b2-0000-4000-8000-000000000002"
GITIGNORE = "exports/*\n!exports/README.md\n"
# Built at runtime so this file never carries a scannable token itself.
FAKE_GITHUB_TOKEN = "ghp_" + "A1b2C3d4" * 5
FAKE_PASSWORD_LINE = "password" + ": " + "hunter2hunter2"


def _value(value: object) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(str(item) for item in value) + "]"
    return str(value)


def note(fields: dict[str, object], body: str = f"Body {SENTINEL}.\n") -> str:
    lines = ["---", *(f"{key}: {_value(value)}" for key, value in fields.items()), "---", ""]
    return "\n".join(lines) + body


def uid(n: int) -> str:
    return f"{n:08x}-1111-4000-8000-000000000000"


def v3(kind: str, record_id: str, scope: str, **overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "schema": 3,
        "id": record_id,
        "type": kind,
        "title": f"A {kind}",
        "date": "2026-09-27",
        "status": "accepted",
        "scope": scope,
        "projects": [],
        "tags": [],
    }
    fields.update(overrides)
    return {k: v for k, v in fields.items() if v is not None}


class VaultFixture:
    """An empty schema 3 vault: marker, registry, READMEs, one template."""

    def __init__(self, root: Path) -> None:
        self.root = root
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(root)], check=True, capture_output=True
        )
        self.write(".gitignore", GITIGNORE)
        self.write(
            ".meta/vault.json", json.dumps({"agentbot_memory_schema": 3, "vault_id": VAULT_ID})
        )
        self.write(".meta/projects.json", json.dumps({"projects": []}))
        for readme in sorted(memory.V3_README_EXEMPT):
            self.write(readme, "# Readme without front matter\n")
        self.write("templates/lesson.md", "---\ntitle: {{title}}\ndate: {{date}}\n---\n")

    def write(self, relative: str, text: str | bytes) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")
        return path

    def commit(self) -> None:
        git = [
            "git",
            "-C",
            str(self.root),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
        ]
        subprocess.run([*git, "add", "-A"], check=True, capture_output=True)
        subprocess.run(
            [*git, "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"],
            check=True,
            capture_output=True,
        )


def v3_vault(root: Path) -> VaultFixture:
    """Every tier populated: core, one registered project, one open proposal."""
    vault = VaultFixture(root)
    vault.write(
        ".meta/projects.json",
        json.dumps(
            {"projects": [{"id": P_ALPHA, "origin": "github.com/me/alpha", "folder": "alpha"}]}
        ),
    )
    vault.write("core/user/preferences.md", note(v3("preference", uid(1), "global")))
    vault.write("core/user/profile.md", note(v3("profile", uid(2), "global")))
    vault.write("core/lessons/2026-09-27-core.md", note(v3("lesson", uid(3), "global")))
    vault.write(
        "core/decisions/2026-09-27-shared.md",
        note(v3("decision", uid(4), "shared", projects=["alpha"])),
    )
    project = {"scope": "project", "projects": ["alpha"]}
    vault.write("projects/alpha/project.md", note(v3("project", P_ALPHA, **project)))
    vault.write("projects/alpha/active-context.md", note(v3("context", uid(5), **project)))
    vault.write(
        "projects/alpha/lessons/2026-09-27-l.md",
        note(v3("lesson", uid(6), **project, title="Alpha build lesson")),
    )
    vault.write("projects/alpha/notes/n.md", note(v3("project", uid(7), **project)))
    vault.write("proposals/core/lessons/p.md", note(v3("lesson", uid(8), "global", status="draft")))
    return vault


def old_schema_vault(root: Path) -> VaultFixture:
    """A vault from before schema 3; Agentbot refuses to read it."""
    vault = VaultFixture(root)
    vault.write(".meta/vault.json", json.dumps({"agentbot_memory_schema": 2}))
    return vault


def rules(report: memory.ValidationReport) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for item in report.findings:
        found.setdefault(item.path, set()).add(item.rule)
    return found


class MemoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def assertNoLeak(self, report: memory.ValidationReport) -> None:
        dumped = json.dumps(memory.report_json(report))
        for secret in (SENTINEL, FAKE_GITHUB_TOKEN, "hunter2hunter2"):
            self.assertNotIn(secret, dumped)


class ResolutionTests(MemoryTestCase):
    def test_no_checkout_is_unconfigured_not_an_error(self) -> None:
        status = memory.status(self.tmp / "agentbot", home=self.tmp / "home", environ={})
        self.assertEqual("unconfigured", status.state)
        self.assertEqual(
            (self.tmp / "home" / ".config" / "agentbot" / "memory.json",), status.looked_in
        )

    def test_nothing_is_searched_for(self) -> None:
        # A sibling or home checkout used to be found by guessing; it no longer is.
        v3_vault(self.tmp / "agent-memory")
        v3_vault(self.tmp / "home" / "agent-memory")
        found, _ = memory.find_vault(self.tmp / "agentbot", home=self.tmp / "home", environ={})
        self.assertIsNone(found)

    def test_environment_names_the_checkout_first(self) -> None:
        v3_vault(self.tmp / "named")
        v3_vault(self.tmp / "agent-memory")
        found, _ = memory.find_vault(
            self.tmp / "agentbot",
            home=self.tmp,
            environ={"AGENTBOT_MEMORY_DIR": str(self.tmp / "named")},
        )
        self.assertEqual(self.tmp / "named", found)

    def test_a_directory_without_git_or_marker_is_not_a_vault(self) -> None:
        (self.tmp / "agent-memory" / ".meta").mkdir(parents=True)
        (self.tmp / "agent-memory" / ".meta" / "vault.json").write_text(
            '{"agentbot_memory_schema": 3}'
        )
        found, _ = memory.find_vault(self.tmp / "agentbot", home=self.tmp / "h", environ={})
        self.assertIsNone(found)

    def test_marker_must_be_exact(self) -> None:
        vault = VaultFixture(self.tmp / "v")
        v = VAULT_ID
        for text in (
            "not json",
            '{"agentbot_memory_schema": 3}',
            '{"agentbot_memory_schema": true, "vault_id": "' + v + '"}',
            '{"agentbot_memory_schema": 1}',
            '{"agentbot_memory_schema": 2, "vault_id": "' + v + '"}',
            '{"agentbot_memory_schema": 3, "vault_id": "' + v + '", "extra": 1}',
            '{"agentbot_memory_schema": 3, "agentbot_memory_schema": 3, "vault_id": "' + v + '"}',
        ):
            with self.subTest(text=text):
                vault.write(".meta/vault.json", text)
                with self.assertRaises(memory.MemoryVaultError):
                    memory.read_marker(vault.root)

    def test_a_symlinked_vault_root_is_broken(self) -> None:
        v3_vault(self.tmp / "real")
        (self.tmp / "agent-memory").symlink_to(self.tmp / "real")
        status = memory.status(
            self.tmp / "agentbot",
            home=self.tmp / "h",
            environ={"AGENTBOT_MEMORY_DIR": str(self.tmp / "agent-memory")},
        )
        self.assertEqual("broken", status.state)


class ValidationTests(MemoryTestCase):
    def test_a_sanitized_tree_is_valid(self) -> None:
        vault = v3_vault(self.tmp / "v")
        vault.write("exports/generated.md", "no front matter, never validated\n")
        vault.write(".obsidian/app.json", "{}")
        report = memory.validate(vault.root)
        self.assertEqual([], report.findings)
        self.assertEqual(3, report.schema)

    def test_record_rules(self) -> None:
        vault = v3_vault(self.tmp / "v")
        e = "6d1f3a5c-7e9b-4d2f-a4c6-e8a0b2d4f6a8"
        cases = {
            "core/lessons/upper.md": (v3("lesson", ID_A.upper(), "global"), "MEMORY_ID"),
            "core/lessons/v1uuid.md": (
                v3("lesson", "47ee7dd9-dc50-1f57-8eda-4ae11045e713", "global"),
                "MEMORY_ID",
            ),
            "core/lessons/scope.md": (v3("lesson", e, "team"), "MEMORY_SCOPE"),
            "core/lessons/global.md": (
                v3("lesson", e, "global", projects=["alpha"]),
                "MEMORY_SCOPE",
            ),
            "core/decisions/type.md": (v3("lesson", e, "global"), "MEMORY_TYPE"),
            "core/lessons/date.md": (v3("lesson", e, "global", date="2026-02-30"), "MEMORY_DATE"),
            "core/lessons/title.md": (
                v3("lesson", e, "global", title="'  padded '"),
                "MEMORY_TITLE",
            ),
            "core/lessons/tags.md": (v3("lesson", e, "global", tags=["a", "a"]), "MEMORY_TAGS"),
            "core/lessons/schema.md": (v3("lesson", e, "global", schema=2), "MEMORY_SCHEMA"),
            "core/lessons/draft.md": (v3("lesson", e, "global", status="draft"), "MEMORY_STATUS"),
            "core/lessons/early.md": (
                v3("lesson", e, "global", review_after="2026-01-01"),
                "MEMORY_DATE",
            ),
            "core/lessons/order.md": (
                v3("lesson", e, "global", review_after="2026-12-01", valid_until="2026-11-01"),
                "MEMORY_DATE",
            ),
            "core/lessons/baddate.md": (
                v3("lesson", e, "global", valid_until="soon"),
                "MEMORY_DATE",
            ),
            "core/lessons/extra.md": (v3("lesson", e, "global", aliases=["x"]), "MEMORY_FIELD"),
            "core/lessons/noscope.md": (
                {k: v for k, v in v3("lesson", e, "global").items() if k != "scope"},
                "MEMORY_FIELD",
            ),
        }
        for relative, (fields, _) in cases.items():
            vault.write(relative, note(fields))
        vault.write("core/lessons/bare.md", f"No front matter {SENTINEL}\n")
        vault.write("core/lessons/open.md", "---\ntitle: x\n")
        found = rules(memory.validate(vault.root))
        for relative, (_, rule) in cases.items():
            with self.subTest(path=relative):
                self.assertIn(rule, found.get(relative, set()))
        for relative in ("core/lessons/bare.md", "core/lessons/open.md"):
            self.assertIn("MEMORY_FRONT_MATTER", found.get(relative, set()))

    def test_malformed_and_unsafe_yaml_is_rejected_without_quoting_it(self) -> None:
        vault = v3_vault(self.tmp / "v")
        base = (
            f"schema: 3\nid: {ID_A}\ntype: lesson\ntitle: t\ndate: 2026-09-23\n"
            "status: accepted\nscope: global\nprojects: []\n"
        )
        cases = {
            "core/lessons/malformed.md": f"---\n{base}tags: [{SENTINEL}\n---\n",
            "core/lessons/duplicate.md": f"---\n{base}tags: []\ntags: []\n---\n",
            "core/lessons/alias.md": f"---\n{base}tags: &x []\n---\n",
            "core/lessons/tag.md": f"---\n{base}tags: !!python/object:os.system []\n---\n",
            "core/lessons/list.md": "---\n- one\n- two\n---\n",
        }
        for relative, text in cases.items():
            vault.write(relative, text)
        report = memory.validate(vault.root)
        found = rules(report)
        for relative in cases:
            with self.subTest(path=relative):
                self.assertEqual({"MEMORY_YAML"}, found.get(relative))
        self.assertNoLeak(report)

    def test_file_type_symlink_encoding_and_size(self) -> None:
        vault = v3_vault(self.tmp / "v")
        lessons = vault.root / "core" / "lessons"
        vault.write("core/lessons/image.png", b"\x89PNG")
        vault.write("core/lessons/board.canvas", "{}")
        vault.write("core/lessons/nul.md", b"---\x00\n")
        vault.write("core/lessons/latin.md", b"\xff\xfe")
        vault.write("core/lessons/big.md", "x" * (memory.MAX_FILE_BYTES + 1))
        (lessons / "link.md").symlink_to(vault.root / "core/user/preferences.md")
        (vault.root / "proposals" / "linked").symlink_to(lessons, target_is_directory=True)
        (vault.root / "proposals" / "escape.md").symlink_to("/etc/hostname")
        found = rules(memory.validate(vault.root))
        self.assertEqual({"MEMORY_FILE_TYPE"}, found["core/lessons/image.png"])
        self.assertEqual({"MEMORY_FILE_TYPE"}, found["core/lessons/board.canvas"])
        self.assertEqual({"MEMORY_ENCODING"}, found["core/lessons/nul.md"])
        self.assertEqual({"MEMORY_ENCODING"}, found["core/lessons/latin.md"])
        self.assertEqual({"MEMORY_SIZE"}, found["core/lessons/big.md"])
        self.assertEqual({"MEMORY_SYMLINK"}, found["core/lessons/link.md"])
        self.assertEqual({"MEMORY_SYMLINK"}, found["proposals/linked"])
        self.assertEqual({"MEMORY_SYMLINK"}, found["proposals/escape.md"])

    def test_read_bounded_refuses_a_symlinked_parent(self) -> None:
        vault = v3_vault(self.tmp / "v")
        (vault.root / "linked").symlink_to(vault.root / "core/lessons", target_is_directory=True)
        with self.assertRaises(memory._Unsafe) as raised:
            memory.read_bounded(vault.root, "linked/2026-09-27-core.md", 1024)
        self.assertEqual("MEMORY_SYMLINK", raised.exception.rule)
        for bad in ("../x.md", "/etc/passwd", "a//b.md", "./a.md"):
            with self.subTest(path=bad), self.assertRaises(memory._Unsafe):
                memory.read_bounded(vault.root, bad, 1024)

    def test_duplicate_ids_across_records_and_proposals(self) -> None:
        vault = v3_vault(self.tmp / "v")
        vault.write(
            "proposals/core/lessons/dup.md", note(v3("lesson", uid(1), "global", status="draft"))
        )
        report = memory.validate(vault.root)
        duplicates = [item for item in report.findings if item.rule == "MEMORY_DUPLICATE_ID"]
        self.assertEqual(1, len(duplicates))
        paths = {duplicates[0].path, duplicates[0].message.rsplit(" ", 1)[-1]}
        self.assertEqual({"proposals/core/lessons/dup.md", "core/user/preferences.md"}, paths)
        self.assertNoLeak(report)

    def test_supersession_edges(self) -> None:
        vault = v3_vault(self.tmp / "v")
        missing = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
        e, f = "6d1f3a5c-7e9b-4d2f-a4c6-e8a0b2d4f6a8", "7e2a4b6c-8d0e-4f1a-b2c3-d4e5f6a7b8c9"
        vault.write(
            "core/lessons/missing.md", note(v3("lesson", e, "global", supersedes=[missing]))
        )
        vault.write("core/lessons/live.md", note(v3("lesson", f, "global", supersedes=[uid(3)])))
        g, h = "8f3b5c7d-9e1f-4a2b-83c4-d5e6f7a8b9c0", "9a4c6d8e-0f2a-4b3c-94d5-e6f7a8b9c0d1"
        vault.write("core/decisions/g.md", note(v3("decision", g, "global", supersedes=[h])))
        vault.write("core/decisions/h.md", note(v3("decision", h, "global", supersedes=[g])))
        i = "0b5d7e9f-1a3b-4c4d-a5e6-f7a8b9c0d1e2"
        vault.write("core/lessons/self.md", note(v3("lesson", i, "global", supersedes=[i])))
        vault.write(
            "core/user/preferences.md",
            note(v3("preference", uid(1), "global", supersedes=[uid(3)])),
        )
        found = rules(memory.validate(vault.root))
        self.assertIn("MEMORY_SUPERSEDES_TARGET", found["core/lessons/missing.md"])
        self.assertIn("MEMORY_SUPERSEDES_STATUS", found["core/lessons/live.md"])
        self.assertIn("MEMORY_SUPERSEDES_CYCLE", found["core/decisions/g.md"])
        self.assertIn("MEMORY_SUPERSEDES_CYCLE", found["core/decisions/h.md"])
        self.assertIn("MEMORY_SUPERSEDES", found["core/lessons/self.md"])
        self.assertIn("MEMORY_SUPERSEDES", found["core/user/preferences.md"])

    def test_a_proposal_may_supersede_an_accepted_record(self) -> None:
        vault = v3_vault(self.tmp / "v")
        e = "6d1f3a5c-7e9b-4d2f-a4c6-e8a0b2d4f6a8"
        vault.write(
            "proposals/core/lessons/replace.md",
            note(v3("lesson", e, "global", status="draft", supersedes=[uid(3)])),
        )
        self.assertTrue(memory.validate(vault.root).valid)


class ScannerTests(MemoryTestCase):
    def test_blocking_secret_fails_and_is_not_printed(self) -> None:
        vault = v3_vault(self.tmp / "v")
        vault.write(
            "core/lessons/leak.md",
            note(v3("lesson", ID_A, "global"), body=f"line one\ntoken {FAKE_GITHUB_TOKEN}\n"),
        )
        vault.write("templates/leak.md", f"-----BEGIN OPENSSH PRIVATE KEY-----\n{SENTINEL}\n")
        report = memory.validate(vault.root, acknowledge=["SECRET_GITHUB_CLASSIC_TOKEN"])
        leak = [item for item in report.findings if item.path == "core/lessons/leak.md"]
        self.assertEqual(
            [("error", "SECRET_GITHUB_CLASSIC_TOKEN", 13)],
            [(i.severity, i.rule, i.line) for i in leak],
        )
        self.assertIn("SECRET_PRIVATE_KEY", rules(report)["templates/leak.md"])
        self.assertFalse(report.valid)
        self.assertNoLeak(report)

    def test_exports_are_not_scanned(self) -> None:
        vault = v3_vault(self.tmp / "v")
        vault.write("exports/generated.md", FAKE_GITHUB_TOKEN)
        self.assertTrue(memory.validate(vault.root).valid)

    def test_a_warning_can_be_acknowledged_for_one_run(self) -> None:
        vault = v3_vault(self.tmp / "v")
        vault.write(
            "core/lessons/pw.md", note(v3("lesson", ID_A, "global"), body=FAKE_PASSWORD_LINE + "\n")
        )
        first = memory.validate(vault.root)
        self.assertEqual(["WARN_PASSWORD_ASSIGNMENT"], [item.rule for item in first.warnings])
        self.assertFalse(first.valid)
        self.assertTrue(memory.validate(vault.root, acknowledge=["WARN_PASSWORD_ASSIGNMENT"]).valid)
        self.assertFalse(memory.validate(vault.root).valid)
        self.assertNoLeak(first)

    def test_benign_text_does_not_match(self) -> None:
        text = "Use sk-short, AKIA123, password: short, postgres://host/db and ghp_tooshort.\n"
        self.assertEqual([], memory.scan_secrets("core/lessons/x.md", text))


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


class StatusAndCliTests(MemoryTestCase):
    def _env(self, vault: Path | None) -> dict[str, str]:
        env = {"HOME": str(self.tmp / "home"), "NO_COLOR": "1"}
        if vault is not None:
            env["AGENTBOT_MEMORY_DIR"] = str(vault)
        return env

    def _cli(self, *argv: str, vault: Path | None) -> tuple[int, str]:
        clean = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBOT_MEMORY")}
        with patch.dict(os.environ, {**clean, **self._env(vault)}, clear=True):
            rc, stdout, _ = run_cli_main(["agentbot", "--root", str(self.tmp / "agentbot"), *argv])
        return rc, stdout

    def test_status_reports_git_state_and_counts(self) -> None:
        vault = v3_vault(self.tmp / "v")
        vault.commit()
        clean = memory.status(
            self.tmp / "agentbot", home=self.tmp, environ={"AGENTBOT_MEMORY_DIR": str(vault.root)}
        )
        self.assertEqual("ready", clean.state)
        self.assertEqual(0, clean.changed_paths)
        self.assertEqual({"accepted": 8, "retired": 0, "superseded": 0}, dict(clean.statuses))
        self.assertEqual(1, clean.drafts)
        self.assertIsNone(clean.ahead)
        vault.write("core/lessons/new.md", note(v3("lesson", ID_A, "global")))
        dirty = memory.status(
            self.tmp / "agentbot", home=self.tmp, environ={"AGENTBOT_MEMORY_DIR": str(vault.root)}
        )
        self.assertEqual(1, dirty.changed_paths)

    def test_staged_validation_judges_the_staged_bytes(self) -> None:
        """Review 2's COULD: the pre-commit check reads what the commit will hold.

        The hook validated working files, so an invalid record could be staged
        and committed while the editor's copy on disk was valid again, and the
        other way round.
        """
        vault = v3_vault(self.tmp / "v")
        vault.commit()
        record = "core/lessons/2026-09-27-core.md"
        valid = (vault.root / record).read_text(encoding="utf-8")
        git = ["git", "-C", str(vault.root)]

        vault.write(record, "not a record\n")
        subprocess.run([*git, "add", record], check=True, capture_output=True)
        vault.write(record, valid)
        self.assertEqual(0, self._cli("memory", "validate", vault=vault.root)[0])
        self.assertEqual(1, self._cli("memory", "validate", "--staged", vault=vault.root)[0])

        subprocess.run([*git, "add", record], check=True, capture_output=True)
        vault.write(record, "not a record\n")
        self.assertEqual(1, self._cli("memory", "validate", vault=vault.root)[0])
        self.assertEqual(0, self._cli("memory", "validate", "--staged", vault=vault.root)[0])

    def test_the_pre_commit_hook_validates_the_staged_index(self) -> None:
        from src import memory_hook

        script = memory_hook.render_hook(self.tmp / "agentbot")
        self.assertIn("pre-commit) scope=--staged", script)
        self.assertIn("memory validate $scope", script)

    def test_update_refreshes_the_vault_hooks(self) -> None:
        """Every update keeps the vault checks current, so a changed hook
        (such as the 2026-10-08 staged pre-commit) needs no manual step."""
        from src import memory_hook

        vault = v3_vault(self.tmp / "v")
        vault.commit()
        home = self.tmp / "agentbot"
        hooks = memory_hook.hooks_directory(vault.root)
        hooks.mkdir(parents=True, exist_ok=True)
        stale = memory_hook.render_hook(home).replace("--staged", "")
        (hooks / "pre-commit").write_text(stale, encoding="utf-8")
        (hooks / "pre-commit").chmod(0o755)
        with patch.dict(os.environ, {"AGENTBOT_MEMORY_DIR": str(vault.root)}):
            detail, result = memory_hook.refresh(home, self.tmp / "config")
        self.assertEqual("ok", result)
        self.assertIn("pre-commit", detail)
        for name in memory_hook.HOOKS:
            self.assertEqual(memory_hook.render_hook(home), (hooks / name).read_text())

        with patch.dict(os.environ, {"AGENTBOT_MEMORY_DIR": str(vault.root)}):
            self.assertEqual(("current", "ok"), memory_hook.refresh(home, self.tmp / "config"))

    def test_update_never_replaces_a_hook_agentbot_does_not_own(self) -> None:
        from src import memory_hook

        vault = v3_vault(self.tmp / "v")
        vault.commit()
        hooks = memory_hook.hooks_directory(vault.root)
        hooks.mkdir(parents=True, exist_ok=True)
        (hooks / "pre-commit").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        with patch.dict(os.environ, {"AGENTBOT_MEMORY_DIR": str(vault.root)}):
            detail, result = memory_hook.refresh(self.tmp / "agentbot", self.tmp / "config")
        self.assertEqual("check", result)
        self.assertIn("not Agentbot's", detail)
        self.assertIn("echo mine", (hooks / "pre-commit").read_text())

    def test_update_skips_the_hooks_without_a_vault(self) -> None:
        from src import memory_hook

        clean = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBOT_MEMORY")}
        with patch.dict(os.environ, {**clean, "HOME": str(self.tmp / "home")}, clear=True):
            _, result = memory_hook.refresh(self.tmp / "agentbot", self.tmp / "config")
        self.assertEqual("skipped", result)

    def test_validate_writes_nothing(self) -> None:
        # Hooks run it mid-commit and mid-push; status may sync, validate never does.
        vault = v3_vault(self.tmp / "v")
        vault.commit()
        before = _tree_digest(vault.root)
        self._cli("memory", "validate", "--json", vault=vault.root)
        self.assertEqual(before, _tree_digest(vault.root))

    def test_exit_codes(self) -> None:
        self.assertEqual(2, self._cli("memory", "status", vault=None)[0])
        self.assertEqual(2, self._cli("memory", "validate", vault=None)[0])
        vault = v3_vault(self.tmp / "v")
        self.assertEqual(0, self._cli("memory", "validate", vault=vault.root)[0])
        vault.write("core/lessons/bad.md", note(v3("decision", ID_A, "global")))
        rc, stdout = self._cli("memory", "validate", vault=vault.root)
        self.assertEqual(1, rc)
        self.assertIn("core/lessons/bad.md", stdout)
        self.assertIn("MEMORY_TYPE", stdout)
        self.assertNotIn(SENTINEL, stdout)
        self.assertEqual(0, self._cli("memory", "status", vault=vault.root)[0])
        vault.write(".meta/vault.json", "{}")
        self.assertEqual(1, self._cli("memory", "status", vault=vault.root)[0])
        self.assertEqual(1, self._cli("memory", "validate", vault=vault.root)[0])

    def test_json_output(self) -> None:
        rc, stdout = self._cli("memory", "status", "--json", vault=None)
        self.assertEqual("unconfigured", json.loads(stdout)["state"])
        vault = v3_vault(self.tmp / "v")
        vault.write(
            "core/lessons/pw.md", note(v3("lesson", ID_A, "global"), body=FAKE_PASSWORD_LINE + "\n")
        )
        rc, stdout = self._cli(
            "memory",
            "validate",
            "--json",
            "--acknowledge-warning",
            "WARN_PASSWORD_ASSIGNMENT",
            vault=vault.root,
        )
        payload = json.loads(stdout)
        self.assertEqual(0, rc)
        self.assertEqual("valid", payload["state"])
        self.assertTrue(payload["findings"][0]["acknowledged"])
        self.assertNotIn("hunter2", stdout)

    def test_a_blocking_rule_cannot_be_acknowledged(self) -> None:
        with self.assertRaises(SystemExit):
            self._cli(
                "memory", "validate", "--acknowledge-warning", "SECRET_PRIVATE_KEY", vault=None
            )


if __name__ == "__main__":
    unittest.main()
