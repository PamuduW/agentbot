import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.skill_packaging import prepared_checkout
from src.skills_installer import InstallResult, _skill_folder_hash, install_source
from src.skills_sources import SkillSourceEntry

WRITING_RULE = """This applies to NEW skills AND EDITS to existing skills.

Write skill before testing? Delete it. Start over.
Edit skill without testing? Same violation.

**No exceptions:**
- Not for "simple additions"
- Not for "just adding a section"
- Not for "documentation updates"
- Don't keep untested changes as "reference"
- Don't "adapt" while running tests
- Delete means delete
"""


class SkillPackagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.checkout = self.root / "source"
        self.checkout.mkdir()
        self.lock = self.root / ".agents" / ".skill-lock.json"

    def write(self, relative, text):
        path = self.checkout / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def install(self, repo, names):
        source = SkillSourceEntry(id="source", repo=repo, skills=names)

        def skills_cli(argv, **kwargs):
            checkout = Path(argv[4])
            for name in names:
                shutil.copytree(checkout / "skills" / name, self.lock.parent / "skills" / name)
            return InstallResult("source", argv, 0, "", "")

        with (
            patch("src.skills_installer._checkout_revision", return_value="a" * 40),
            patch("src.skills_installer.run_install_command", side_effect=skills_cli),
        ):
            install_source(source, agents=["codex"], checkout=self.checkout, global_lock_file=self.lock)
        return self.lock.parent / "skills"

    def test_writing_skill_installs_its_support_without_the_meta_policy(self):
        for name in ("codex-tools.md", "gemini-tools.md"):
            self.write(f"skills/using-superpowers/references/{name}", f"# {name}\n")
        self.write("skills/using-superpowers/SKILL.md", "# excluded policy\n")
        original = "---\nname: writing-skills\n---\n" + "\n".join(
            f"[{name}](../using-superpowers/references/{name})"
            for name in ("codex-tools.md", "gemini-tools.md")
        )
        self.write("skills/writing-skills/SKILL.md", original)

        installed = self.install("obra/superpowers", ["writing-skills"])

        skill = installed / "writing-skills"
        for name in ("codex-tools.md", "gemini-tools.md"):
            self.assertEqual(f"# {name}\n", (skill / "references" / name).read_text())
            self.assertIn(f"](references/{name})", (skill / "SKILL.md").read_text())
        self.assertFalse((installed / "using-superpowers").exists())
        self.assertEqual(original, (self.checkout / "skills/writing-skills/SKILL.md").read_text())
        receipt = json.loads(self.lock.read_text())["skills"]["writing-skills"]
        self.assertEqual(_skill_folder_hash(skill), receipt["skillFolderHash"])
        self.assertEqual("a" * 40, receipt["sourceRevision"])
        provenance = json.loads((skill / ".agentbot-packaging.json").read_text())
        self.assertEqual("obra/superpowers", provenance["upstream"])
        self.assertEqual("a" * 40, provenance["revision"])

    def test_packaged_skill_safety_preserves_source_and_provenance(self):
        originals = {
            "writing-skills": WRITING_RULE + "\n- [ ] Commit skill to git and push to your fork (if configured)\n",
            "using-git-worktrees": (
                "**If NOT ignored:** Add to .gitignore, commit the change, then proceed.\n"
                "| Directory not ignored | Add to .gitignore + commit |\n"
            ),
        }
        for name, original in originals.items():
            self.write(f"skills/{name}/SKILL.md", original)
        installed = self.install("obra/superpowers", list(originals))
        for name, original in originals.items():
            skill = installed / name
            self.assertEqual(original, (self.checkout / "skills" / name / "SKILL.md").read_text())
            text = (skill / "SKILL.md").read_text()
            self.assertIn("authorization", text)
            self.assertNotIn("Delete means delete", text)
            self.assertNotIn("Add to .gitignore + commit", text)
            provenance = json.loads((skill / ".agentbot-packaging.json").read_text())
            self.assertEqual("a" * 40, provenance["revision"])
            self.assertIn(f"skills/{name}/SKILL.md", provenance["inputs"])

    def test_changed_unsafe_skill_rule_stops_before_install(self):
        self.write("skills/writing-skills/SKILL.md", WRITING_RULE.replace("Start over.", "Restart now."))
        with self.assertRaisesRegex(ValueError, "safety instructions changed"):
            self.install("obra/superpowers", ["writing-skills"])
        self.assertFalse(self.lock.exists())

    def test_gitlab_root_skill_preserves_failed_verification_exit(self):
        self.write("SKILL.md", "---\nname: gitlab-ci-skill\n---\n# Verify\n")
        self.write("scripts/verify.sh", '#!/bin/bash\nif ! glci show -f "$FILE"; then\n      exit $?\n    fi\necho should-not-run\n')
        fake = self.root / "bin"
        fake.mkdir()
        (fake / "glci").write_text("#!/bin/sh\nexit 37\n")
        (fake / "glci").chmod(0o755)
        source = SkillSourceEntry(id="gitlab", repo="gitlab.com/gitlab-org/ci-cd/gitlab-ci-skill", skills=["gitlab-ci-skill"])
        with prepared_checkout(source, self.checkout, "a" * 40) as prepared:
            run = subprocess.run(["bash", str(prepared / "scripts/verify.sh")], capture_output=True,
                                 env={**os.environ, "PATH": f"{fake}:/usr/bin:/bin"})
            self.assertEqual(37, run.returncode)
            self.assertEqual(b"", run.stdout)
            provenance = json.loads((prepared / ".agentbot-packaging.json").read_text())
            self.assertIn("scripts/verify.sh", provenance["inputs"])

    def test_claudex_review_commands_disable_native_hooks(self):
        self.write("skills/claudex-loop/SKILL.md", "---\nname: claudex-loop\n---\n# Review\n")
        self.write("skills/claudex-loop/scripts/runner.py", 'def command(review=True, mcp_off=()):\n    args = []\n    if True:\n        if review:\n            for name in mcp_off:\n                args.append(name)\n    return args\n')
        source = SkillSourceEntry(id="claudex", repo="chaseai-yt/claudex-loop", skills=["claudex-loop"])
        with prepared_checkout(source, self.checkout, "a" * 40) as prepared:
            namespace = {}
            exec((prepared / "skills/claudex-loop/scripts/runner.py").read_text(), namespace)
            self.assertIn("features.hooks=false", namespace["command"]())
            self.assertEqual([], namespace["command"](review=False))

    def test_each_devops_skill_has_its_own_report_format(self):
        names = ["github-actions", "k8s", "owasp"]
        self.write("_docs/REVIEW-REPORT.md", "# Report format\n")
        for name in names:
            self.write(f"skills/{name}/SKILL.md", f"---\nname: {name}\n---\n[format](../../_docs/REVIEW-REPORT.md)\n")

        installed = self.install("anmolnagpal/devops-skills", names)

        for name in names:
            skill = installed / name
            self.assertEqual("# Report format\n", (skill / "references/REVIEW-REPORT.md").read_text())
            self.assertIn("](references/REVIEW-REPORT.md)", (skill / "SKILL.md").read_text())

    def test_citation_verifier_is_stateless_and_keeps_other_sections(self):
        self.write("skills/verify-citations/SKILL.md", "---\nname: verify-citations\n---\n# Verify\nRun scripts/verify.py\n\n## Memory\nRead .paper-memory and ../paper-profile/references/paper-memory-convention.md\nRun reflect-and-improve reflect_log.py\n\n## References\nKeep this section.\n")
        self.write("skills/verify-citations/scripts/verify.py", "print('verify')\n")

        installed = self.install("ShaishavMaisuria/research-paper-lifecycle-skills", ["verify-citations"])

        skill = installed / "verify-citations"
        text = (skill / "SKILL.md").read_text()
        self.assertNotIn(".paper-memory", text)
        self.assertNotIn("paper-profile", text)
        self.assertNotIn("reflect-and-improve", text)
        self.assertIn("stateless", text)
        self.assertIn("Keep this section.", text)
        self.assertTrue((skill / "scripts/verify.py").is_file())

    def test_missing_support_fails_before_installing(self):
        self.write("skills/writing-skills/SKILL.md", "---\nname: writing-skills\n---\n[tools](../using-superpowers/references/codex-tools.md)\n")
        with self.assertRaisesRegex(ValueError, "codex-tools.md"):
            self.install("obra/superpowers", ["writing-skills"])
        self.assertFalse(self.lock.exists())

    def test_readonly_checkouts_can_be_packaged_without_changing_permissions(self):
        self.write("skills/writing-skills/SKILL.md", "---\nname: writing-skills\n---\n[tools](../using-superpowers/references/codex-tools.md)\n")
        self.write("skills/using-superpowers/references/codex-tools.md", "# Tools\n")
        directories = [self.checkout, *(p for p in self.checkout.rglob("*") if p.is_dir())]
        try:
            for directory in directories:
                directory.chmod(0o555)
            installed = self.install("obra/superpowers", ["writing-skills"])
            self.assertTrue((installed / "writing-skills/references/codex-tools.md").is_file())
            self.assertTrue(all(p.stat().st_mode & 0o777 == 0o555 for p in directories))
        finally:
            for directory in directories:
                directory.chmod(0o755)
