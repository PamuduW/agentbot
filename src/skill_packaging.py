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
from .skills_sources import SkillSourceEntry

_SUPPORT: dict[str, dict[str, tuple[str, ...]]] = {
    "obra/superpowers": {
        "writing-skills": (
            "../using-superpowers/references/codex-tools.md",
            "../using-superpowers/references/gemini-tools.md",
        ),
    },
    "anmolnagpal/devops-skills": dict.fromkeys(
        ("github-actions", "k8s", "owasp"), ("../../_docs/REVIEW-REPORT.md",)
    ),
    "ShaishavMaisuria/research-paper-lifecycle-skills": {"verify-citations": ()},
}


def _prepare_skill(source: SkillSourceEntry, checkout: Path, target: Path, revision: str | None) -> None:
    original = checkout / "skills" / target.name / "SKILL.md"
    text = original.read_text(encoding="utf-8")
    inputs = {str(original.relative_to(checkout)): sha256(original.read_bytes()).hexdigest()}
    for reference in _SUPPORT[source.repo or ""][target.name]:
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
    if target.name == "verify-citations":
        text = re.sub(
            r"\n## Memory\n.*?(?=\n## |\Z)",
            "\n## State\n\nThis verifier is stateless. Report findings in the requested output; "
            "retain no lessons or author profile between runs.\n",
            text, flags=re.DOTALL,
        )
        if any(name in text for name in (".paper-memory", "paper-profile", "reflect-and-improve")):
            raise ValueError("citation verifier memory instructions changed; review its packaging")
    write_text_atomic(target / "SKILL.md", text)
    write_text_atomic(
        target / ".agentbot-packaging.json",
        json.dumps({"version": 1, "upstream": source.repo, "revision": revision, "inputs": inputs}, indent=2) + "\n",
    )


@contextmanager
def prepared_checkout(
    source: SkillSourceEntry, checkout: Path, revision: str | None,
) -> Iterator[Path]:
    recipes = _SUPPORT.get(source.repo or "", {})
    targets = [
        name for name in recipes
        if (source.skills == ["*"] or name in source.skills)
        and not source.excludes(name)
        and (checkout / "skills" / name / "SKILL.md").is_file()
    ]
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
        for name in targets:
            _prepare_skill(source, checkout, prepared / "skills" / name, revision)
        yield prepared
