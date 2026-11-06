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
    write_text_atomic(target / "SKILL.md", text)
    write_text_atomic(
        target / ".agentbot-packaging.json",
        json.dumps({"version": 2, "upstream": source.repo, "revision": revision, "inputs": inputs}, indent=2) + "\n",
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
