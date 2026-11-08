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

RESET_IMPORT = '''sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks"))
from session_start import state_directory
'''

WORKTREE_CHECK = "git check-ignore -q .worktrees 2>/dev/null || git check-ignore -q worktrees 2>/dev/null"

SDD_OWNS = '''owns() {
  if [ -e "$1/plan-path" ]; then
    [ "$(cat "$1/plan-path")" = "$plan_id" ]
  else
    mkdir -p "$1"
    printf '%s\\n' "$plan_id" > "$1/plan-path"
  fi
}'''

SDD_FINISH = '''When the final whole-branch review is clean and its fixes are merged,
delete this plan's workspace (`rm -rf <workspace>`) — the git history is
the record now. Sibling directories belong to other plans; leave them
alone.'''

SDD_LEGACY = """# plan's parent-directory name, then a counter. A workspace with no marker
# predates the marker scheme and is adopted for the current plan so in-flight
# workspaces keep resolving — which means the first collision on such a
# legacy workspace adopts instead of detecting; acceptable, marker-less
# workspaces age out as plans finish.
# marker must name this plan; a missing marker means a new workspace or a
# pre-marker legacy one, and either way the plan claims it by writing one.
"""

GOLDEN_PATH = '''#!/bin/bash
# create-service.sh - Golden path for new services

SERVICE=$1
LANG=$2

# Create from template
gh repo create "org/$SERVICE" --template "org/template-$LANG"
git clone "git@github.com:org/$SERVICE.git"
cd "$SERVICE"

# Setup CI/CD
cat > .github/workflows/ci.yml <<EOF
name: CI/CD
on: [push]
jobs:
  pipeline:
    uses: org/workflows/.github/workflows/standard.yml@v1
    with:
      service_name: $SERVICE
EOF

# Create infrastructure
cat > terraform/main.tf <<EOF
module "service" {
  source = "git::https://github.com/org/terraform//service"
  name   = "$SERVICE"
}
EOF

git add . && git commit -m "Golden path init" && git push

echo "✓ Service created! Merge to main to deploy."
'''


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
                + "```bash\n" + WORKTREE_CHECK + "\n```\n"
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

    def test_worktree_ignore_check_uses_only_the_selected_directory(self):
        self.write("skills/using-git-worktrees/SKILL.md",
                   "---\nname: using-git-worktrees\n---\n```bash\n" + WORKTREE_CHECK + "\n```\n")
        installed = self.install("obra/superpowers", ["using-git-worktrees"])
        text = (installed / "using-git-worktrees/SKILL.md").read_text()
        script = text.split("```bash\n", 1)[1].split("```", 1)[0]
        project = self.root / "project"
        project.mkdir()
        (project / ".worktrees").mkdir()
        (project / "worktrees").mkdir()
        (project / ".gitignore").write_text("worktrees/\n")
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        subprocess.run(["git", "init", "--quiet", str(project)], check=True, env=env)
        for location, expected in ((".worktrees", 1), ("worktrees", 0), ("", 1)):
            with self.subTest(location=location):
                run = subprocess.run(["bash", "-c", script], cwd=project, capture_output=True,
                                     env={**env, "LOCATION": location})
                self.assertEqual(expected == 0, run.returncode == 0, run.stderr)

    def test_changed_worktree_ignore_check_stops_before_install(self):
        self.write("skills/using-git-worktrees/SKILL.md",
                   "---\nname: using-git-worktrees\n---\n" + WORKTREE_CHECK.replace("2>/dev/null", ""))
        with self.assertRaisesRegex(ValueError, "ignore check changed"):
            self.install("obra/superpowers", ["using-git-worktrees"])
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

    def test_reset_works_without_the_upstream_plugin_tree(self):
        self.write("skills/reset/SKILL.md", "---\nname: reset\n---\nRun reset.py\n")
        self.write("skills/reset/reset.py", "import sys\nfrom pathlib import Path\n" + RESET_IMPORT
                   + "print(state_directory(Path(sys.argv[1])))\n")
        hook = Path(__file__).resolve().parents[1] / "global/claude/hooks/vibe-wise-session-start.py"
        self.write("hooks/session_start.py", hook.read_text())
        project = self.root / "project"
        (project / ".vibe-wise").mkdir(parents=True)
        installed = self.install("nykooi1/vibe-wise", ["reset"])
        self.checkout.rename(self.root / "detached-source")
        run = subprocess.run(["python3", str(installed / "reset/reset.py"), str(project)],
                             capture_output=True, text=True,
                             env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertEqual(str(project / ".vibe-wise"), run.stdout.strip())
        provenance = json.loads((installed / "reset/.agentbot-packaging.json").read_text())
        self.assertIn("hooks/session_start.py", provenance["inputs"])
        self.assertIn("skills/reset/reset.py", provenance["inputs"])

    def workspace_fixture(self):
        self.write("skills/subagent-driven-development/SKILL.md",
                   "---\nname: subagent-driven-development\n---\nWorkspace\n"
                   + "Final review clean: delete this plan's workspace\n" * 3)
        script = ('#!/bin/bash\nset -euo pipefail\nroot=$PWD\n'
                  'base="$root/.superpowers/sdd"\nslug=plan\nplan_id=plan.md\n'
                  + SDD_LEGACY + SDD_OWNS + '\ndir="$base/$slug"\n'
                  'if ! owns "$dir"; then\n  dir="$base/$slug-project"\n  owns "$dir"\nfi\n'
                  '[ -e "$base/.gitignore" ] || printf "*\\n" > "$base/.gitignore"\n'
                  'cd "$dir"\npwd\n')
        self.write("skills/subagent-driven-development/scripts/sdd-workspace", script)
        self.write("skills/subagent-driven-development/references/final-review.md", SDD_FINISH)
        return SkillSourceEntry(id="sdd", repo="obra/superpowers",
                                skills=["subagent-driven-development"])

    def test_workspace_rejects_linked_roots_without_external_writes(self):
        source = self.workspace_fixture()
        outside = self.root / "user-data"
        outside.mkdir()
        with prepared_checkout(source, self.checkout, "a" * 40) as prepared:
            for linked in (".superpowers", ".superpowers/sdd", ".superpowers/sdd/.gitignore"):
                with self.subTest(linked=linked):
                    project = self.root / linked.replace("/", "-")
                    project.mkdir()
                    link = project / linked
                    link.parent.mkdir(parents=True, exist_ok=True)
                    link.symlink_to(outside, target_is_directory=True)
                    run = subprocess.run(["bash", str(prepared / "skills/subagent-driven-development/scripts/sdd-workspace")],
                                         cwd=project, capture_output=True)
                    self.assertNotEqual(0, run.returncode)
                    self.assertEqual([], list(outside.iterdir()))

    def test_workspace_does_not_claim_unmarked_or_linked_plan_directories(self):
        source = self.workspace_fixture()
        with prepared_checkout(source, self.checkout, "a" * 40) as prepared:
            for linked in (False, True):
                with self.subTest(linked=linked):
                    project = self.root / f"project-{linked}"
                    base = project / ".superpowers/sdd"
                    base.mkdir(parents=True)
                    previous = self.root / f"existing-{linked}"
                    previous.mkdir()
                    (previous / "sentinel").write_text("user-owned")
                    if linked:
                        (base / "plan").symlink_to(previous, target_is_directory=True)
                    else:
                        previous.rename(base / "plan")
                        previous = base / "plan"
                    run = subprocess.run(["bash", str(prepared / "skills/subagent-driven-development/scripts/sdd-workspace")],
                                         cwd=project, capture_output=True, text=True)
                    self.assertEqual(0, run.returncode, run.stderr)
                    self.assertEqual(str(base / "plan-project"), run.stdout.strip())
                    self.assertEqual(["sentinel"], [p.name for p in previous.iterdir()])

    def test_golden_path_clone_failure_preserves_the_callers_files(self):
        self.write("skills/devops-engineer/SKILL.md", "---\nname: devops-engineer\n---\nPlatform guide\n")
        self.write("skills/devops-engineer/references/platform-engineering.md", "```bash\n" + GOLDEN_PATH + "```\n")
        source = SkillSourceEntry(id="devops", repo="jeffallan/claude-skills", skills=["devops-engineer"])
        project = self.root / "project"
        for relative in (".github/workflows/ci.yml", "terraform/main.tf"):
            path = project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("user-owned")
        with prepared_checkout(source, self.checkout, "a" * 40) as prepared:
            guide = (prepared / "skills/devops-engineer/references/platform-engineering.md").read_text()
            script = guide.split("```bash\n", 1)[1].split("```", 1)[0]
            run = subprocess.run(["bash", "-s", "--", "missing-service", "python"],
                                 input='gh() { return 9; }; git() { return 9; };\n' + script,
                                 cwd=project, capture_output=True, text=True)
            self.assertNotEqual(0, run.returncode)
        for relative in (".github/workflows/ci.yml", "terraform/main.tf"):
            self.assertEqual("user-owned", (project / relative).read_text())

    def test_golden_path_preparation_never_creates_commits_or_remote_repositories(self):
        self.write("skills/devops-engineer/SKILL.md", "---\nname: devops-engineer\n---\nPlatform guide\n")
        self.write("skills/devops-engineer/references/platform-engineering.md", "```bash\n" + GOLDEN_PATH + "```\n")
        source = SkillSourceEntry(id="devops", repo="jeffallan/claude-skills", skills=["devops-engineer"])
        with prepared_checkout(source, self.checkout, "a" * 40) as prepared:
            guide = (prepared / "skills/devops-engineer/references/platform-engineering.md").read_text()
            script = guide.split("```bash\n", 1)[1].split("```", 1)[0]
            prelude = '''gh() { echo remote-create >> "$EFFECTS"; }
git() {
  if [[ "$1" == clone ]]; then
    mkdir -p service/.github/workflows service/terraform
  else
    echo "$*" >> "$EFFECTS"
  fi
}
'''
            effects = self.root / "effects"
            run = subprocess.run(["bash", "-s", "--", "service", "python"],
                                 input=prelude + script, cwd=self.root, capture_output=True, text=True,
                                 env={**os.environ, "EFFECTS": str(effects)})
            self.assertEqual(0, run.returncode, run.stderr)
            self.assertTrue((self.root / "service/.github/workflows/ci.yml").is_file())
            self.assertFalse(effects.exists())

    def test_changed_workspace_safety_block_stops_before_install(self):
        self.workspace_fixture()
        path = self.checkout / "skills/subagent-driven-development/scripts/sdd-workspace"
        path.write_text(path.read_text().replace('mkdir -p "$1"', 'mkdir -- "$1"'))
        with self.assertRaisesRegex(ValueError, "safety support changed"):
            self.install("obra/superpowers", ["subagent-driven-development"])
        self.assertFalse(self.lock.exists())

    def test_changed_sdd_cleanup_diagram_stops_before_install(self):
        self.workspace_fixture()
        path = self.checkout / "skills/subagent-driven-development/SKILL.md"
        path.write_text(path.read_text().replace("delete this plan's workspace", "discard workspace", 1))
        with self.assertRaisesRegex(ValueError, "cleanup instructions changed"):
            self.install("obra/superpowers", ["subagent-driven-development"])
        self.assertFalse(self.lock.exists())

    def test_missing_reset_helper_stops_before_install(self):
        self.write("skills/reset/SKILL.md", "---\nname: reset\n---\nRun reset.py\n")
        self.write("skills/reset/reset.py", RESET_IMPORT)
        with self.assertRaisesRegex(ValueError, "reset support file"):
            self.install("nykooi1/vibe-wise", ["reset"])
        self.assertFalse(self.lock.exists())

    def test_changed_golden_path_stops_before_install(self):
        self.write("skills/devops-engineer/SKILL.md", "---\nname: devops-engineer\n---\nPlatform guide\n")
        self.write("skills/devops-engineer/references/platform-engineering.md",
                   GOLDEN_PATH.replace('cd "$SERVICE"', 'cd "${SERVICE}"'))
        with self.assertRaisesRegex(ValueError, "safety support changed"):
            self.install("jeffallan/claude-skills", ["devops-engineer"])
        self.assertFalse(self.lock.exists())

    def test_changed_archify_instruction_target_stops_before_install(self):
        self.write("SKILL.md", "---\nname: archify\n---\nIf accepted, edit global instruction files.\n")
        source = SkillSourceEntry(id="archify", repo="tt-a1i/archify", skills=["archify"])
        with self.assertRaisesRegex(ValueError, "safety support changed"), prepared_checkout(
            source, self.checkout, "a" * 40,
        ):
            self.fail("changed global instruction guidance must not reach installation")

    def test_changed_inline_cleanup_rule_stops_before_install(self):
        self.write("skills/executing-plans/SKILL.md",
                   "---\nname: executing-plans\n---\nDelete this plan's workspace.\n")
        with self.assertRaisesRegex(ValueError, "cleanup instructions changed"):
            self.install("obra/superpowers", ["executing-plans"])
        self.assertFalse(self.lock.exists())
