"""Self-contained installations for selected upstream skills with external references."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

from .atomic_io import write_text_atomic
from .skill_catalog import skill_name_from_file
from .skills_sources import SkillSourceEntry

_SUPPORT: dict[str, dict[str, tuple[str, ...]]] = {
    "obra/superpowers": {
        "subagent-driven-development": (),
        "executing-plans": (),
        "using-git-worktrees": (),
        "writing-skills": (
            "../using-superpowers/references/codex-tools.md",
            "../using-superpowers/references/gemini-tools.md",
        ),
    },
    "anmolnagpal/devops-skills": dict.fromkeys(
        ("github-actions", "k8s", "owasp"), ("../../_docs/REVIEW-REPORT.md",)
    ),
    "ShaishavMaisuria/research-paper-lifecycle-skills": {"verify-citations": ()},
    "gitlab.com/gitlab-org/ci-cd/gitlab-ci-skill": {"gitlab-ci-skill": ()},
    "chaseai-yt/claudex-loop": {"claudex-loop": ()},
    "nykooi1/vibe-wise": {"reset": ()},
    "jeffallan/claude-skills": {"devops-engineer": ()},
    "tt-a1i/archify": {"archify": ()},
}


_SCRIPT_EDITS = {
    ("gitlab.com/gitlab-org/ci-cd/gitlab-ci-skill", "gitlab-ci-skill"): (
        "scripts/verify.sh",
        'if ! glci show -f "$FILE"; then\n      exit $?\n    fi',
        'glci show -f "$FILE" || exit $?',
    ),
    ("chaseai-yt/claudex-loop", "claudex-loop"): (
        "scripts/runner.py",
        '        if review:\n            for name in mcp_off:',
        '        if review:\n            args += ["-c", "features.hooks=false"]\n            for name in mcp_off:',
    ),
    ("nykooi1/vibe-wise", "reset"): (
        "reset.py",
        'sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks"))',
        'sys.path.insert(0, str(Path(__file__).resolve().parent / "references"))',
    ),
}

_SDD_FINISH = """When the final whole-branch review is clean and its fixes are merged,
delete this plan's workspace (`rm -rf <workspace>`) — the git history is
the record now. Sibling directories belong to other plans; leave them
alone."""
_INLINE_FINISH = """When the final review is clean and its fixes are committed, delete this
plan's workspace directory — the git history is the record now. Sibling
directories belong to other plans; leave them alone."""
_KEEP_WORKSPACE = """Retain the workspace by default. Cleanup needs explicit task authorization.
Remove only known task-created scratch files; never recursively delete a
pre-existing workspace, follow workspace symlinks, or delete user-owned content."""
_SDD_OWNS = '''owns() {
  if [ -e "$1/plan-path" ]; then
    [ "$(cat "$1/plan-path")" = "$plan_id" ]
  else
    mkdir -p "$1"
    printf '%s\\n' "$plan_id" > "$1/plan-path"
  fi
}'''
_SDD_LEGACY = """# plan's parent-directory name, then a counter. A workspace with no marker
# predates the marker scheme and is adopted for the current plan so in-flight
# workspaces keep resolving — which means the first collision on such a
# legacy workspace adopts instead of detecting; acceptable, marker-less
# workspaces age out as plans finish."""
_SAFE_SDD_OWNS = '''owns() {
  if [ -e "$1" ] || [ -L "$1" ]; then
    [ -d "$1" ] && [ ! -L "$1" ] && [ -f "$1/plan-path" ] \\
      && [ ! -L "$1/plan-path" ] && [ "$(cat "$1/plan-path")" = "$plan_id" ]
  else
    mkdir "$1" || exit 2
    printf '%s\\n' "$plan_id" > "$1/plan-path" || exit 2
  fi
}'''
_FILE_EDITS = {
    ("obra/superpowers", "subagent-driven-development"): (
        ("scripts/sdd-workspace", _SDD_LEGACY,
         "# plan's parent-directory name, then a counter. Unmarked existing\n"
         "# directories and symlinks are preserved and never claimed by a plan."),
        ("scripts/sdd-workspace",
         "# marker must name this plan; a missing marker means a new workspace or a\n"
         "# pre-marker legacy one, and either way the plan claims it by writing one.",
         "# marker must name this plan. Only a newly created directory gets a marker;\n"
         "# existing unmarked directories are never adopted."),
        ("scripts/sdd-workspace", 'base="$root/.superpowers/sdd"', '''base="$root/.superpowers/sdd"
if [ -L "$root/.superpowers" ] || [ -L "$base" ] || [ -L "$base/.gitignore" ]; then
  echo "Refusing linked SDD workspace storage" >&2
  exit 2
fi
mkdir -p "$base" || exit 2'''),
        ("scripts/sdd-workspace", _SDD_OWNS, _SAFE_SDD_OWNS),
        ("references/final-review.md", _SDD_FINISH, _KEEP_WORKSPACE),
    ),
    ("obra/superpowers", "executing-plans"): (
        ("SKILL.md", _INLINE_FINISH, _KEEP_WORKSPACE),
    ),
    ("jeffallan/claude-skills", "devops-engineer"): (
        ("references/platform-engineering.md", "# create-service.sh - Golden path for new services\n",
         "# create-service.sh - Golden path for new services\nset -euo pipefail\n"),
        ("references/platform-engineering.md", "SERVICE=$1\nLANG=$2", '''SERVICE=${1:?service name required}
SERVICE_LANGUAGE=${2:?template language required}
case "$SERVICE" in
  ''|*[!a-zA-Z0-9_-]*) echo "Invalid service name" >&2; exit 2 ;;
esac'''),
        ("references/platform-engineering.md",
         'gh repo create "org/$SERVICE" --template "org/template-$LANG"',
         '# Create org/$SERVICE from org/template-$SERVICE_LANGUAGE separately,\n'
         '# only after explicit authorization to create that remote repository.'),
        ("references/platform-engineering.md", 'git clone "git@github.com:org/$SERVICE.git"\ncd "$SERVICE"',
         'git clone -- "git@github.com:org/$SERVICE.git" "$SERVICE" || exit $?\n'
         'cd -- "$SERVICE" || exit $?'),
        ("references/platform-engineering.md", "# Setup CI/CD\ncat > .github/workflows/ci.yml", '''# Preserve existing template configuration for review.
for file in .github/workflows/ci.yml terraform/main.tf; do
  if [[ -e "$file" || -L "$file" ]]; then
    echo "Existing configuration needs review: $file" >&2
    exit 2
  fi
done
mkdir -p .github/workflows terraform
# Setup CI/CD
cat > .github/workflows/ci.yml'''),
        ("references/platform-engineering.md",
         'git add . && git commit -m "Golden path init" && git push',
         '# Review the diff. Stage only intended files; commit and push each need\n'
         '# explicit authorization for this task.'),
        ("references/platform-engineering.md", 'echo "✓ Service created! Merge to main to deploy."',
         'echo "Local $SERVICE ($SERVICE_LANGUAGE) service files prepared for review."'),
    ),
    ("tt-a1i/archify", "archify"): (
        ("SKILL.md",
         "to the global rules file of the current agent (such as `~/.claude/CLAUDE.md`, "
         "`~/.codex/AGENTS.md` or `~/.config/devin/AGENTS.md`) and name the file; "
         "deleting the line turns it off.",
         "to the canonical user-authored rules source for the current agent and name "
         "that source. Inspect ownership first: never edit a generated instruction "
         "adapter directly. In an Agentbot-managed setup, edit the canonical source "
         "and use its documented renderer. If the canonical source or write "
         "authorization is unavailable, report the pending preference without changing "
         "instructions. Removing the preference from its canonical source turns it off."),
    ),
}


_WRITING_RULE = """This applies to NEW skills AND EDITS to existing skills.

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


def _preserve_operator_authority(name: str, text: str) -> str:
    if name == "writing-skills":
        if "Delete means delete" in text:
            if text.count(_WRITING_RULE) != 1:
                raise ValueError("writing-skills safety instructions changed; review its packaging")
            text = text.replace(_WRITING_RULE, """Apply test-first work to new skills and edits to existing skills.

Existing skills and user-owned edits must be preserved. If tests were missed,
add a behavior test around the existing content, observe the failure, then
make the smallest correction. Set aside only drafts written in the current
task; never delete existing user work to restart the process. Destructive
actions require task-specific authorization.
""")
        text = text.replace(
            "Commit skill to git and push to your fork (if configured)",
            "Commit or push only with explicit authorization for this task",
        )
    elif name == "using-git-worktrees":
        check = "git check-ignore -q .worktrees 2>/dev/null || git check-ignore -q worktrees 2>/dev/null"
        if text.count(check) != 1:
            raise ValueError("using-git-worktrees ignore check changed; review its packaging")
        text = text.replace(check, '# Set LOCATION to the exact directory selected above.\n'
                            'git check-ignore -q -- "${LOCATION:?Set LOCATION to the selected worktree directory}"')
        rule = "**If NOT ignored:** Add to .gitignore, commit the change, then proceed."
        if "Add to .gitignore" in text and rule not in text:
            raise ValueError("using-git-worktrees safety instructions changed; review its packaging")
        text = text.replace(rule, """**If NOT ignored:** Prefer an authorized external worktree directory. If a
repository-local directory is required, use a local Git exclude or an in-scope
.gitignore edit. Preserve existing content. Commit only with explicit task
authorization; creating a worktree does not grant commit authorization.""")
        text = text.replace(
            "| Directory not ignored | Add to .gitignore + commit |",
            "| Directory not ignored | Use external storage or local exclusion; commit needs authorization |",
        )
    elif name in ("executing-plans", "subagent-driven-development"):
        instruction = "Final review clean: delete this plan's workspace"
        if text.count(instruction) != 3:
            raise ValueError(f"{name} cleanup instructions changed; review its packaging")
        text = text.replace(instruction, "Final review clean: retain the workspace")
    return text


def _prepare_skill(
    source: SkillSourceEntry, checkout: Path, original: Path, target: Path, revision: str | None,
) -> None:
    name = skill_name_from_file(original)
    text = original.read_text(encoding="utf-8")
    inputs = {str(original.relative_to(checkout)): sha256(original.read_bytes()).hexdigest()}
    for reference in _SUPPORT[source.repo or ""][name]:
        if reference not in text:
            continue
        support = (original.parent / reference).resolve()
        if not support.is_relative_to(checkout.resolve()) or not support.is_file():
            raise ValueError(f"missing or unsafe skill support file: {reference}")
        destination = target / "references" / support.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(support, destination)
        text = text.replace(reference, f"references/{support.name}")
        inputs[str(support.relative_to(checkout.resolve()))] = sha256(support.read_bytes()).hexdigest()
    if name == "verify-citations":
        text = re.sub(
            r"\n## Memory\n.*?(?=\n## |\Z)",
            "\n## State\n\nThis verifier is stateless. Report findings in the requested output; "
            "retain no lessons or author profile between runs.\n",
            text, flags=re.DOTALL,
        )
        if any(name in text for name in (".paper-memory", "paper-profile", "reflect-and-improve")):
            raise ValueError("citation verifier memory instructions changed; review its packaging")
    if source.repo == "obra/superpowers":
        text = _preserve_operator_authority(name, text)
    if source.repo == "nykooi1/vibe-wise" and name == "reset":
        support = (original.parent / "../../hooks/session_start.py").resolve()
        if not support.is_relative_to(checkout.resolve()) or not support.is_file():
            raise ValueError("missing or unsafe reset support file: hooks/session_start.py")
        destination = target / "references/session_start.py"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(support, destination)
        inputs[str(support.relative_to(checkout.resolve()))] = sha256(support.read_bytes()).hexdigest()
    edit = _SCRIPT_EDITS.get((source.repo or "", name))
    if edit is not None:
        relative, old, new = edit
        support = original.parent / relative
        if not support.is_file() or support.is_symlink():
            raise ValueError(f"missing or unsafe skill script: {relative}")
        script = support.read_text(encoding="utf-8")
        if script.count(old) != 1:
            raise ValueError(f"{name} safety script changed; review its packaging")
        write_text_atomic(target / relative, script.replace(old, new))
        inputs[str(support.relative_to(checkout))] = sha256(support.read_bytes()).hexdigest()
    contents: dict[str, str] = {}
    for relative, old, new in _FILE_EDITS.get((source.repo or "", name), ()):
        support = original.parent / relative
        if relative not in contents:
            if not support.is_file() or support.is_symlink():
                raise ValueError(f"missing or unsafe skill support: {relative}")
            contents[relative] = text if relative == "SKILL.md" else support.read_text(encoding="utf-8")
            inputs[str(support.relative_to(checkout))] = sha256(support.read_bytes()).hexdigest()
        if contents[relative].count(old) != 1:
            raise ValueError(f"{name} safety support changed: {relative}; review its packaging")
        contents[relative] = contents[relative].replace(old, new)
    for relative, content in contents.items():
        if relative == "SKILL.md":
            text = content
        else:
            write_text_atomic(target / relative, content)
    write_text_atomic(target / "SKILL.md", text)
    write_text_atomic(
        target / ".agentbot-packaging.json",
        json.dumps({"version": 3, "upstream": source.repo, "revision": revision, "inputs": inputs}, indent=2) + "\n",
    )


@contextmanager
def prepared_checkout(
    source: SkillSourceEntry, checkout: Path, revision: str | None,
) -> Iterator[Path]:
    recipes = _SUPPORT.get(source.repo or "", {})
    targets: dict[str, Path] = {}
    for path in sorted(checkout.rglob("SKILL.md"), key=lambda p: (len(p.parts), str(p))):
        name = skill_name_from_file(path)
        if name in recipes and (source.skills == ["*"] or name in source.skills) and not source.excludes(name):
            targets.setdefault(name, path)
    if not targets:
        yield checkout
        return
    with tempfile.TemporaryDirectory(prefix="agentbot-packaged-skills-") as temporary:
        prepared = Path(temporary)
        # Copy all source content so the CLI's discovery and exclusion rules
        # stay identical. Never modify the verified upstream checkout.
        for path in checkout.rglob("*"):
            if ".git" not in path.relative_to(checkout).parts and path.is_symlink():
                if not path.resolve().is_relative_to(checkout.resolve()):
                    raise ValueError(f"skill source contains an external symlink: {path.name}")
        shutil.copytree(checkout, prepared, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
        for directory in (prepared, *(path for path in prepared.rglob("*") if path.is_dir())):
            directory.chmod(directory.stat().st_mode | 0o700)
        for original in targets.values():
            target = prepared / original.parent.relative_to(checkout)
            _prepare_skill(source, checkout, original, target, revision)
        yield prepared
