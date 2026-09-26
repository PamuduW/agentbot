"""Memory Core status and validation against sanitized v1 and v2 vault trees.

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
GITIGNORE = "drafts/*\n!drafts/README.md\nexports/*\n!exports/README.md\n"
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


def v1(kind: str, **overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "schema": 1,
        "type": kind,
        "title": f"A {kind} record",
        "date": "2026-09-23",
        "status": "accepted",
        "projects": [],
        "tags": [],
    }
    fields.update(overrides)
    return {key: value for key, value in fields.items() if value is not None}


def v2(kind: str, record_id: str, scope: str, **overrides: object) -> dict[str, object]:
    fields = v1(kind, schema=2)
    fields = {"schema": 2, "id": record_id, **{k: v for k, v in fields.items() if k != "schema"}}
    fields["scope"] = scope
    fields.update(overrides)
    return {key: value for key, value in fields.items() if value is not None}


class VaultFixture:
    def __init__(self, root: Path, schema: int) -> None:
        self.root = root
        subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
        self.write(".gitignore", GITIGNORE)
        self.write(".meta/vault.json", json.dumps({"agentbot_memory_schema": schema}))
        for readme in sorted(memory.README_EXEMPT):
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


def v1_vault(root: Path) -> VaultFixture:
    vault = VaultFixture(root, 1)
    vault.write("preferences.md", note(v1("preference", title="Working preferences")))
    vault.write("active-context.md", note(v1("context", title="Active context")))
    vault.write("decisions/2026-09-23-a-decision.md", note(v1("decision", projects=["alpha"])))
    vault.write("lessons/2026-09-23-a-lesson.md", note(v1("lesson", tags=["wsl", "flutter"])))
    vault.write("projects/alpha/2026-09-23-a-project.md", note(v1("project", projects=["alpha"])))
    vault.write("drafts/2026-09-23-a-draft.md", note(v1("lesson", status="draft")))
    vault.write("exports/generated.md", "no front matter, never validated\n")
    vault.write(".obsidian/app.json", "{}")
    return vault


def v2_vault(root: Path) -> VaultFixture:
    vault = VaultFixture(root, 2)
    vault.write("preferences.md", note(v2("preference", ID_A, "global")))
    vault.write(
        "lessons/2026-09-01-old.md",
        note(v2("lesson", ID_B, "shared", status="superseded", projects=["alpha", "beta"])),
    )
    vault.write(
        "lessons/2026-09-23-new.md",
        note(
            v2(
                "lesson",
                ID_C,
                "project",
                projects=["alpha"],
                supersedes=[ID_B],
                review_after="2026-09-24",
                valid_until="2026-09-25",
            )
        ),
    )
    vault.write(
        "projects/alpha/2026-09-23-p.md", note(v2("project", ID_D, "project", projects=["alpha"]))
    )
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
            (self.tmp / "agent-memory", self.tmp / "home" / "agent-memory"), status.looked_in
        )

    def test_environment_names_the_checkout_first(self) -> None:
        v1_vault(self.tmp / "named")
        v1_vault(self.tmp / "agent-memory")
        found, _ = memory.find_vault(
            self.tmp / "agentbot",
            home=self.tmp,
            environ={"AGENTBOT_MEMORY_DIR": str(self.tmp / "named")},
        )
        self.assertEqual(self.tmp / "named", found)

    def test_a_directory_without_git_or_marker_is_not_a_vault(self) -> None:
        (self.tmp / "agent-memory" / ".meta").mkdir(parents=True)
        (self.tmp / "agent-memory" / ".meta" / "vault.json").write_text(
            '{"agentbot_memory_schema": 1}'
        )
        found, _ = memory.find_vault(self.tmp / "agentbot", home=self.tmp / "h", environ={})
        self.assertIsNone(found)

    def test_marker_must_be_exact(self) -> None:
        vault = VaultFixture(self.tmp / "v", 1)
        for text in (
            "not json",
            '{"agentbot_memory_schema": 3}',
            '{"agentbot_memory_schema": true}',
            '{"agentbot_memory_schema": 1, "extra": 1}',
            '{"agentbot_memory_schema": 1, "agentbot_memory_schema": 1}',
        ):
            with self.subTest(text=text):
                vault.write(".meta/vault.json", text)
                with self.assertRaises(memory.MemoryVaultError):
                    memory.read_marker(vault.root)

    def test_a_symlinked_vault_root_is_broken(self) -> None:
        v1_vault(self.tmp / "real")
        (self.tmp / "agent-memory").symlink_to(self.tmp / "real")
        status = memory.status(self.tmp / "agentbot", home=self.tmp / "h", environ={})
        self.assertEqual("broken", status.state)


class V1ValidationTests(MemoryTestCase):
    def test_a_sanitized_v1_tree_is_valid(self) -> None:
        report = memory.validate(v1_vault(self.tmp / "v").root)
        self.assertEqual([], report.findings)
        self.assertEqual(1, report.schema)
        self.assertEqual(5, sum(1 for record in report.records if not record.draft))
        self.assertEqual(1, sum(1 for record in report.records if record.draft))

    def test_v1_record_rules(self) -> None:
        vault = v1_vault(self.tmp / "v")
        cases = {
            "decisions/id.md": (note(v1("decision", id=ID_A)), "MEMORY_FIELD"),
            "decisions/missing.md": (note(v1("decision", tags=None)), "MEMORY_FIELD"),
            "decisions/type.md": (note(v1("lesson")), "MEMORY_TYPE"),
            "decisions/date.md": (note(v1("decision", date="2026-02-30")), "MEMORY_DATE"),
            "decisions/title.md": (note(v1("decision", title="'  padded '")), "MEMORY_TITLE"),
            "decisions/status.md": (note(v1("decision", status="draft")), "MEMORY_STATUS"),
            "decisions/tags.md": (note(v1("decision", tags=["a", "a"])), "MEMORY_TAGS"),
            "decisions/slug.md": (note(v1("decision", projects=["Not_A_Slug"])), "MEMORY_PROJECTS"),
            "decisions/schema.md": (note(v1("decision", schema=2)), "MEMORY_SCHEMA"),
            "projects/beta/x.md": (note(v1("project", projects=["alpha"])), "MEMORY_PROJECTS"),
            "projects/alpha/two.md": (
                note(v1("project", projects=["alpha", "beta"])),
                "MEMORY_PROJECTS",
            ),
            "projects/loose.md": (note(v1("project", projects=["alpha"])), "MEMORY_LOCATION"),
            "drafts/accepted.md": (note(v1("lesson")), "MEMORY_STATUS"),
            "drafts/context.md": (note(v1("context", status="draft")), "MEMORY_TYPE"),
            "notes/x.md": (note(v1("lesson")), "MEMORY_LOCATION"),
            "stray.md": (note(v1("lesson")), "MEMORY_LOCATION"),
            "lessons/bare.md": (f"No front matter {SENTINEL}\n", "MEMORY_FRONT_MATTER"),
            "lessons/open.md": ("---\ntitle: x\n", "MEMORY_FRONT_MATTER"),
        }
        for relative, (text, _) in cases.items():
            vault.write(relative, text)
        found = rules(memory.validate(vault.root))
        for relative, (_, rule) in cases.items():
            with self.subTest(path=relative):
                self.assertIn(rule, found.get(relative, set()))

    def test_malformed_and_unsafe_yaml_is_rejected_without_quoting_it(self) -> None:
        vault = v1_vault(self.tmp / "v")
        base = (
            "schema: 1\ntype: lesson\ntitle: t\ndate: 2026-09-23\nstatus: accepted\nprojects: []\n"
        )
        cases = {
            "lessons/malformed.md": f"---\n{base}tags: [{SENTINEL}\n---\n",
            "lessons/duplicate.md": f"---\n{base}tags: []\ntags: []\n---\n",
            "lessons/alias.md": f"---\n{base}tags: &x []\n---\n",
            "lessons/tag.md": f"---\n{base}tags: !!python/object:os.system []\n---\n",
            "lessons/list.md": "---\n- one\n- two\n---\n",
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
        vault = v1_vault(self.tmp / "v")
        vault.write("lessons/image.png", b"\x89PNG")
        vault.write("lessons/board.canvas", "{}")
        vault.write("lessons/nul.md", b"---\x00\n")
        vault.write("lessons/latin.md", b"\xff\xfe")
        vault.write("lessons/big.md", "x" * (memory.MAX_FILE_BYTES + 1))
        (vault.root / "lessons" / "link.md").symlink_to(vault.root / "preferences.md")
        (vault.root / "drafts" / "linked").symlink_to(
            vault.root / "lessons", target_is_directory=True
        )
        (vault.root / "drafts" / "escape.md").symlink_to("/etc/hostname")
        found = rules(memory.validate(vault.root))
        self.assertEqual({"MEMORY_FILE_TYPE"}, found["lessons/image.png"])
        self.assertEqual({"MEMORY_FILE_TYPE"}, found["lessons/board.canvas"])
        self.assertEqual({"MEMORY_ENCODING"}, found["lessons/nul.md"])
        self.assertEqual({"MEMORY_ENCODING"}, found["lessons/latin.md"])
        self.assertEqual({"MEMORY_SIZE"}, found["lessons/big.md"])
        self.assertEqual({"MEMORY_SYMLINK"}, found["lessons/link.md"])
        self.assertEqual({"MEMORY_SYMLINK"}, found["drafts/linked"])
        self.assertEqual({"MEMORY_SYMLINK"}, found["drafts/escape.md"])

    def test_read_bounded_refuses_a_symlinked_parent(self) -> None:
        vault = v1_vault(self.tmp / "v")
        (vault.root / "linked").symlink_to(vault.root / "lessons", target_is_directory=True)
        with self.assertRaises(memory._Unsafe) as raised:
            memory.read_bounded(vault.root, "linked/2026-09-23-a-lesson.md", 1024)
        self.assertEqual("MEMORY_SYMLINK", raised.exception.rule)
        for bad in ("../x.md", "/etc/passwd", "a//b.md", "./a.md"):
            with self.subTest(path=bad), self.assertRaises(memory._Unsafe):
                memory.read_bounded(vault.root, bad, 1024)


class ScannerTests(MemoryTestCase):
    def test_blocking_secret_fails_and_is_not_printed(self) -> None:
        vault = v1_vault(self.tmp / "v")
        vault.write(
            "lessons/leak.md", note(v1("lesson"), body=f"line one\ntoken {FAKE_GITHUB_TOKEN}\n")
        )
        vault.write("templates/leak.md", f"-----BEGIN OPENSSH PRIVATE KEY-----\n{SENTINEL}\n")
        report = memory.validate(vault.root, acknowledge=["SECRET_GITHUB_CLASSIC_TOKEN"])
        leak = [item for item in report.findings if item.path == "lessons/leak.md"]
        self.assertEqual(
            [("error", "SECRET_GITHUB_CLASSIC_TOKEN", 11)],
            [(i.severity, i.rule, i.line) for i in leak],
        )
        self.assertIn("SECRET_PRIVATE_KEY", rules(report)["templates/leak.md"])
        self.assertFalse(report.valid)
        self.assertNoLeak(report)

    def test_exports_are_not_scanned(self) -> None:
        vault = v1_vault(self.tmp / "v")
        vault.write("exports/generated.md", FAKE_GITHUB_TOKEN)
        self.assertTrue(memory.validate(vault.root).valid)

    def test_a_warning_can_be_acknowledged_for_one_run(self) -> None:
        vault = v1_vault(self.tmp / "v")
        vault.write("lessons/pw.md", note(v1("lesson"), body=FAKE_PASSWORD_LINE + "\n"))
        first = memory.validate(vault.root)
        self.assertEqual(["WARN_PASSWORD_ASSIGNMENT"], [item.rule for item in first.warnings])
        self.assertFalse(first.valid)
        self.assertTrue(memory.validate(vault.root, acknowledge=["WARN_PASSWORD_ASSIGNMENT"]).valid)
        self.assertFalse(memory.validate(vault.root).valid)
        self.assertNoLeak(first)

    def test_benign_text_does_not_match(self) -> None:
        text = "Use sk-short, AKIA123, password: short, postgres://host/db and ghp_tooshort.\n"
        self.assertEqual([], memory.scan_secrets("lessons/x.md", text))


class V2ValidationTests(MemoryTestCase):
    def test_a_sanitized_v2_tree_is_valid_including_an_expired_record(self) -> None:
        report = memory.validate(v2_vault(self.tmp / "v").root)
        self.assertEqual([], report.findings)
        self.assertEqual(2, report.schema)

    def test_a_v1_record_in_a_v2_vault_fails(self) -> None:
        vault = v2_vault(self.tmp / "v")
        vault.write("lessons/old-style.md", note(v1("lesson")))
        self.assertEqual(
            {"MEMORY_FIELD"}, rules(memory.validate(vault.root))["lessons/old-style.md"]
        )

    def test_duplicate_ids_across_records_and_drafts(self) -> None:
        vault = v2_vault(self.tmp / "v")
        vault.write("drafts/dup.md", note(v2("lesson", ID_A, "global", status="draft")))
        report = memory.validate(vault.root)
        duplicates = [item for item in report.findings if item.rule == "MEMORY_DUPLICATE_ID"]
        self.assertEqual(1, len(duplicates))
        paths = {duplicates[0].path, duplicates[0].message.rsplit(" ", 1)[-1]}
        self.assertEqual({"drafts/dup.md", "preferences.md"}, paths)
        self.assertNoLeak(report)

    def test_v2_field_rules(self) -> None:
        vault = v2_vault(self.tmp / "v")
        e = "6d1f3a5c-7e9b-4d2f-a4c6-e8a0b2d4f6a8"
        cases = {
            "lessons/upper.md": (v2("lesson", ID_A.upper(), "global"), "MEMORY_ID"),
            "lessons/v1uuid.md": (
                v2("lesson", "47ee7dd9-dc50-1f57-8eda-4ae11045e713", "global"),
                "MEMORY_ID",
            ),
            "lessons/scope.md": (v2("lesson", e, "team"), "MEMORY_SCOPE"),
            "lessons/global.md": (v2("lesson", e, "global", projects=["alpha"]), "MEMORY_SCOPE"),
            "lessons/proj.md": (v2("lesson", e, "project", projects=["a", "b"]), "MEMORY_SCOPE"),
            "decisions/none.md": (v2("decision", e, "project"), "MEMORY_SCOPE"),
            "projects/alpha/s.md": (v2("project", e, "shared", projects=["alpha"]), "MEMORY_SCOPE"),
            "active-context.md": (v2("context", e, "project", projects=["alpha"]), "MEMORY_SCOPE"),
            "lessons/early.md": (
                v2("lesson", e, "global", review_after="2026-01-01"),
                "MEMORY_DATE",
            ),
            "lessons/order.md": (
                v2("lesson", e, "global", review_after="2026-12-01", valid_until="2026-11-01"),
                "MEMORY_DATE",
            ),
            "lessons/baddate.md": (v2("lesson", e, "global", valid_until="soon"), "MEMORY_DATE"),
            "lessons/extra.md": (v2("lesson", e, "global", aliases=["x"]), "MEMORY_FIELD"),
            "lessons/noscope.md": (
                {k: v for k, v in v2("lesson", e, "global").items() if k != "scope"},
                "MEMORY_FIELD",
            ),
        }
        for relative, (fields, _) in cases.items():
            vault.write(relative, note(fields))
        found = rules(memory.validate(vault.root))
        for relative, (_, rule) in cases.items():
            with self.subTest(path=relative):
                self.assertIn(rule, found.get(relative, set()))

    def test_supersession_edges(self) -> None:
        vault = v2_vault(self.tmp / "v")
        missing = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
        e, f = "6d1f3a5c-7e9b-4d2f-a4c6-e8a0b2d4f6a8", "7e2a4b6c-8d0e-4f1a-b2c3-d4e5f6a7b8c9"
        vault.write("lessons/missing.md", note(v2("lesson", e, "global", supersedes=[missing])))
        vault.write("lessons/live.md", note(v2("lesson", f, "global", supersedes=[ID_D])))
        g, h = "8f3b5c7d-9e1f-4a2b-83c4-d5e6f7a8b9c0", "9a4c6d8e-0f2a-4b3c-94d5-e6f7a8b9c0d1"
        vault.write("decisions/g.md", note(v2("decision", g, "global", supersedes=[h])))
        vault.write("decisions/h.md", note(v2("decision", h, "global", supersedes=[g])))
        i = "0b5d7e9f-1a3b-4c4d-a5e6-f7a8b9c0d1e2"
        vault.write("lessons/self.md", note(v2("lesson", i, "global", supersedes=[i])))
        j = "1c6e8f0a-2b4c-4d5e-b6f7-a8b9c0d1e2f3"
        vault.write(
            "lessons/retired.md",
            note(v2("lesson", j, "global", status="retired", supersedes=[ID_B])),
        )
        k = "2d7f9a1b-3c5d-4e6f-87a8-b9c0d1e2f3a4"
        vault.write("preferences.md", note(v2("preference", k, "global", supersedes=[ID_B])))
        found = rules(memory.validate(vault.root))
        self.assertIn("MEMORY_SUPERSEDES_TARGET", found["lessons/missing.md"])
        self.assertIn("MEMORY_SUPERSEDES_STATUS", found["lessons/live.md"])
        self.assertIn("MEMORY_SUPERSEDES_CYCLE", found["decisions/g.md"])
        self.assertIn("MEMORY_SUPERSEDES_CYCLE", found["decisions/h.md"])
        self.assertIn("MEMORY_SUPERSEDES", found["lessons/self.md"])
        self.assertIn("MEMORY_SUPERSEDES", found["lessons/retired.md"])
        self.assertIn("MEMORY_SUPERSEDES", found["preferences.md"])

    def test_a_draft_may_propose_superseding_an_accepted_record(self) -> None:
        vault = v2_vault(self.tmp / "v")
        e = "6d1f3a5c-7e9b-4d2f-a4c6-e8a0b2d4f6a8"
        vault.write(
            "drafts/replace.md", note(v2("lesson", e, "global", status="draft", supersedes=[ID_C]))
        )
        self.assertTrue(memory.validate(vault.root).valid)

    def test_a_draft_cannot_target_another_draft(self) -> None:
        vault = v2_vault(self.tmp / "v")
        e, f = "6d1f3a5c-7e9b-4d2f-a4c6-e8a0b2d4f6a8", "7e2a4b6c-8d0e-4f1a-b2c3-d4e5f6a7b8c9"
        vault.write("drafts/one.md", note(v2("lesson", e, "global", status="draft")))
        vault.write(
            "drafts/two.md", note(v2("lesson", f, "global", status="draft", supersedes=[e]))
        )
        self.assertIn(
            "MEMORY_SUPERSEDES_TARGET", rules(memory.validate(vault.root))["drafts/two.md"]
        )


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
        vault = v1_vault(self.tmp / "v")
        vault.commit()
        clean = memory.status(
            self.tmp / "agentbot", home=self.tmp, environ={"AGENTBOT_MEMORY_DIR": str(vault.root)}
        )
        self.assertEqual("ready", clean.state)
        self.assertEqual(0, clean.changed_paths)
        self.assertEqual({"accepted": 5, "retired": 0, "superseded": 0}, dict(clean.statuses))
        self.assertEqual(1, clean.drafts)
        self.assertIsNone(clean.ahead)
        vault.write("lessons/new.md", note(v1("lesson")))
        dirty = memory.status(
            self.tmp / "agentbot", home=self.tmp, environ={"AGENTBOT_MEMORY_DIR": str(vault.root)}
        )
        self.assertEqual(1, dirty.changed_paths)

    def test_commands_write_nothing(self) -> None:
        vault = v1_vault(self.tmp / "v")
        vault.commit()
        before = _tree_digest(vault.root)
        self._cli("memory", "status", vault=vault.root)
        self._cli("memory", "validate", "--json", vault=vault.root)
        self.assertEqual(before, _tree_digest(vault.root))

    def test_exit_codes(self) -> None:
        self.assertEqual(2, self._cli("memory", "status", vault=None)[0])
        self.assertEqual(2, self._cli("memory", "validate", vault=None)[0])
        vault = v1_vault(self.tmp / "v")
        self.assertEqual(0, self._cli("memory", "validate", vault=vault.root)[0])
        vault.write("lessons/bad.md", note(v1("decision")))
        rc, stdout = self._cli("memory", "validate", vault=vault.root)
        self.assertEqual(1, rc)
        self.assertIn("lessons/bad.md", stdout)
        self.assertIn("MEMORY_TYPE", stdout)
        self.assertNotIn(SENTINEL, stdout)
        self.assertEqual(0, self._cli("memory", "status", vault=vault.root)[0])
        vault.write(".meta/vault.json", "{}")
        self.assertEqual(1, self._cli("memory", "status", vault=vault.root)[0])
        self.assertEqual(1, self._cli("memory", "validate", vault=vault.root)[0])

    def test_json_output(self) -> None:
        rc, stdout = self._cli("memory", "status", "--json", vault=None)
        self.assertEqual("unconfigured", json.loads(stdout)["state"])
        vault = v1_vault(self.tmp / "v")
        vault.write("lessons/pw.md", note(v1("lesson"), body=FAKE_PASSWORD_LINE + "\n"))
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
