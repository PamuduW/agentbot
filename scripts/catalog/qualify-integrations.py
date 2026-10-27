#!/usr/bin/env python3
"""Exercise real Linux extensions and pinned skill distribution without inference."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.shared_paths import shared_python_path  # noqa: E402 - Bootstrap import path.

sys.path.insert(0, str(shared_python_path(ROOT)))
from catalog_context import load_qualification_context  # noqa: E402 - Shared source path.
from software_catalog import CatalogError, _object, _read  # noqa: E402 - Shared source path.

from src.command_runner import CommandRunner  # noqa: E402 - Bootstrap import path.


class RecordingRunner(CommandRunner):
    def __init__(self):
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append(tuple(argv))
        return super().run(argv, **kwargs)


def private_context(manifest: Path, destination: Path) -> Path:
    snapshot, sources, _ = load_qualification_context(manifest)
    destination.mkdir(mode=0o700)
    catalog = destination / "catalog"
    catalog.mkdir(mode=0o700)
    files = {"software.json", "software.tsv"}
    for entry in snapshot.data["components"].values():
        for artifact in entry["targets"].values():
            if "lock" in artifact:
                files.add(artifact["lock"])
    for name in sorted(files):
        target = catalog / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_read(snapshot.directory / name, 16_777_216))
    context = destination / "context.json"
    context.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "catalog": str(catalog),
                "digest": snapshot.digest,
                "sources": {name: str(path) for name, path in sources.items()},
            }
        )
    )
    load_qualification_context(context)
    return context


def editor_metadata(data: dict) -> dict:
    commit, version, digest = (data.get(key) for key in ("version", "productVersion", "sha256hash"))
    if (
        not isinstance(commit, str)
        or not re.fullmatch(r"[0-9a-f]{40}", commit)
        or not isinstance(version, str)
        or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
        or data.get("name") != version
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        raise CatalogError("unsupported Microsoft editor version/checksum metadata")
    url = data.get("url", "")
    pattern = rf"https://vscode\.download\.prss\.microsoft\.com/dbazure/download/stable/{commit}/code-stable-x64-[0-9]{{1,20}}\.tar\.gz"
    if not isinstance(url, str) or not re.fullmatch(pattern, url):
        raise CatalogError("editor archive differs from its canonical publisher/commit")
    return {"commit": commit, "version": version, "sha256": digest, "url": url}


class MicrosoftRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc
            not in {"update.code.visualstudio.com", "vscode.download.prss.microsoft.com"}
            or parsed.query
            or parsed.fragment
            or parsed.username
        ):
            raise CatalogError("editor prerequisite redirected outside its publisher")
        return super().redirect_request(request, fp, code, message, headers, url)


def prepare_editor(home: Path, work: Path, sources: dict, runner: CommandRunner) -> dict:
    opener = urllib.request.build_opener(MicrosoftRedirect())
    endpoint = "https://update.code.visualstudio.com/api/update/linux-x64/stable/latest"
    with opener.open(endpoint, timeout=30) as response:
        raw = response.read(65_537)
    if len(raw) > 65_536:
        raise CatalogError("editor update metadata exceeds its size limit")
    editor = editor_metadata(json.loads(raw, object_pairs_hook=_object))
    archive = work / "editor.tar.gz"
    digest, size = hashlib.sha256(), 0
    with opener.open(editor["url"], timeout=120) as response, archive.open("xb") as stream:
        for chunk in iter(lambda: response.read(65_536), b""):
            size += len(chunk)
            if size > 536_870_912:
                raise CatalogError("editor archive exceeds its size limit")
            digest.update(chunk)
            stream.write(chunk)
    if digest.hexdigest() != editor["sha256"]:
        raise CatalogError("editor archive checksum differs from Microsoft metadata")
    extracted = work / "editor-extracted"
    extracted.mkdir()
    subprocess.run(
        [
            "bash",
            "-c",
            'source "$1/scripts/lib/software_catalog.sh"; dotfiles_catalog_extract tar.gz "$2" "$3"',
            "bash",
            str(sources["dotfiles"]),
            str(archive),
            str(extracted),
        ],
        check=True,
        timeout=120,
    )
    roots = [path for path in extracted.iterdir() if path.is_dir() and not path.is_symlink()]
    if (
        len(roots) != 1
        or json.loads((roots[0] / "resources/app/package.json").read_text()).get("version")
        != editor["version"]
    ):
        raise CatalogError("editor archive lacks its real matching package metadata")
    destination = home / ".local/share/code"
    destination.parent.mkdir(parents=True, exist_ok=True)
    roots[0].rename(destination)
    (home / ".vscode/extensions").mkdir(parents=True)
    cli = destination / "bin/code"
    version = runner.run([str(cli), "--version"], timeout_seconds=30)
    if not version.ok or not {editor["version"], editor["commit"]} <= set(
        version.stdout.splitlines()
    ):
        raise CatalogError("real editor CLI did not verify its version and commit")
    return {**editor, "bytes": size, "external_test_prerequisite": True}


def extension_state(host, entries, runner):
    from src.catalog_extensions import installed_version, read_ownership
    from src.vscode import read_settings

    wanted = {key: entry["version"] for key, entry in entries.items()}
    actual = {key: installed_version(host.extensions_dir, key) for key in entries}
    owned = read_ownership(host.extensions_dir)
    if (
        actual != wanted
        or set(owned) != set(wanted)
        or any(
            owned[key] != {"version": entry["version"], "digest": entry["digest"]}
            for key, entry in entries.items()
        )
    ):
        raise CatalogError("extension manifests/ownership differ from the frozen closure")
    listing = runner.run(
        [
            str(host.cli),
            "--extensions-dir",
            str(host.extensions_dir),
            "--list-extensions",
            "--show-versions",
        ],
        timeout_seconds=60,
    )
    if not listing.ok:
        raise CatalogError("cannot list real installed extensions")
    versions = {}
    for line in listing.stdout.splitlines():
        if re.fullmatch(r"[a-z0-9._-]+\.[a-z0-9._-]+@[0-9][A-Za-z0-9.+-]*", line):
            key, version = line.rsplit("@", 1)
            versions[key] = version
    if versions != wanted:
        raise CatalogError("editor CLI extension inventory differs from the frozen closure")
    settings, error = read_settings(host.settings_path)
    if error or settings.get("extensions.autoCheckUpdates") is not False:
        raise CatalogError("extension updater checks remain enabled")
    return {"versions": actual, "ownership": owned, "settings": settings}


def extensions_smoke(home, snapshot, runner):
    from src.catalog_extensions import validate_extension_lock
    from src.software_versions import resolve_entry
    from src.vscode import apply, desired_settings, load_manifest, manifest_path, wsl_host

    requested = load_manifest(manifest_path(ROOT)).extensions_for("wsl")
    entries = {}
    for key in requested:
        artifact = resolve_entry(snapshot, "extension." + key.lower(), "linux-amd64")
        closure = validate_extension_lock(
            json.loads((snapshot.directory / artifact["lock"]).read_text()),
            key.lower(),
            artifact["version"],
        )
        for name, entry in closure.items():
            if name in entries and entries[name] != entry:
                raise CatalogError("extension closures disagree")
            entries[name] = entry
    entries = {key: value for key, value in entries.items() if value.get("builtin") is not True}
    if not entries:
        raise CatalogError("empty Linux extension coverage")
    mount = home / "empty-windows-mount"
    report = apply(home, ROOT, runner, mount_root=mount)
    if (
        report.manifest_error
        or report.failures
        or set(report.installed.get("wsl", {})) != set(entries)
    ):
        raise CatalogError("fresh real extension apply was incomplete")
    if set(report.installed["wsl"].values()) != {"installed"}:
        raise CatalogError("a real extension install failed")
    host = wsl_host(home)
    first = extension_state(host, entries, runner)
    desired, error = desired_settings(ROOT, "wsl", editor=host)
    if error or any(first["settings"].get(key) != value for key, value in desired.items()):
        raise CatalogError("selected editor settings were not applied")
    before = len(runner.calls)
    repeat = apply(home, ROOT, runner, mount_root=mount)
    if repeat.manifest_error or repeat.failures or repeat.installed.get("wsl"):
        raise CatalogError("repeat extension apply was incomplete or installed again")
    if any("--install-extension" in call or call[0] == "curl" for call in runner.calls[before:]):
        raise CatalogError("repeat extensions downloaded or installed packages")
    if extension_state(host, entries, runner) != first:
        raise CatalogError("repeat extensions/settings changed")
    return {
        "roots": list(requested),
        "versions": first["versions"],
        "ownership": first["ownership"],
        "settings_verified": True,
        "repeat_no_downloads_or_installs": True,
        "scope": "native-linux-desktop-extension-selection",
        "wsl_certified": False,
    }


def skill_state(paths, expected):
    from src.skills_installer import _skill_folder_hash

    lock = json.loads(paths.global_skill_lock.read_text())
    skills = lock.get("skills", {})
    actual = {
        path.name
        for path in paths.agents_skills_home.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    }
    if set(skills) != set(expected) or actual != set(expected):
        raise CatalogError("installed skill inventory differs from selected source content")
    for name, proof in expected.items():
        entry = skills[name]
        if any(entry.get(key) != value for key, value in proof.items()):
            raise CatalogError(f"skill provenance differs from the selected source: {name}")
        directory = paths.agents_skills_home / name
        if (
            not directory.resolve().is_relative_to(Path.home())
            or _skill_folder_hash(directory) != proof["skillFolderHash"]
        ):
            raise CatalogError(f"installed skill bytes differ from selected source: {name}")
    return {
        "skills": {
            name: {key: value for key, value in entry.items() if key != "updatedAt"}
            for name, entry in skills.items()
        },
        "sources": lock.get("agentbotSources", {}),
    }


def skills_smoke(home, work, runner):
    from src.paths import default_paths
    from src.skill_catalog import skill_name_from_file
    from src.skill_packaging import prepared_checkout
    from src.skills_installer import (
        _ANSI,
        _LISTED_SKILL,
        _clone_pinned_source,
        _skill_folder_hash,
        install_skills,
    )
    from src.skills_sources import load_skills_sources, source_clone_url, source_type
    from src.software_versions import approved_npm_executable, approved_source_revision

    paths = default_paths(ROOT)
    if paths.global_skill_lock.exists() or paths.agents_skills_home.exists():
        raise CatalogError("fresh skills smoke requires empty managed skill stores")
    config = load_skills_sources(paths.skills_sources_file)
    sources = config.active_sources()
    if not sources:
        raise CatalogError("empty skill-source coverage")
    executable = approved_npm_executable(ROOT, "skills_cli", runner)
    checkouts, expected = {}, {}
    for source in sources:
        revision = approved_source_revision(ROOT, source.repo)
        checkout = work / source.id
        _clone_pinned_source(source.repo, revision, checkout, runner=runner)
        checkouts[source.id] = checkout
        with prepared_checkout(source, checkout, revision) as prepared:
            listing = runner.run(
                [str(executable), "add", str(prepared), "--list", "--full-depth"],
                timeout_seconds=120,
            )
            names = {
                match[1]
                for line in _ANSI.sub("", listing.stdout).splitlines()
                if (match := _LISTED_SKILL.match(line))
            }
            chosen = names - set(source.exclude) if source.skills == ["*"] else set(source.skills)
            if not listing.ok or not chosen or not chosen <= names:
                raise CatalogError(
                    f"real Skills CLI cannot discover the selected source: {source.id}"
                )
            files = {}
            for file in sorted(
                prepared.rglob("SKILL.md"),
                key=lambda path: (len(path.relative_to(prepared).parts), str(path)),
            ):
                files.setdefault(skill_name_from_file(file), file)
            for name in chosen:
                if name in expected or name not in files:
                    raise CatalogError("duplicate or missing selected skill content")
                file = files[name]
                expected[name] = {
                    "source": source.repo,
                    "sourceType": source_type(source.repo),
                    "sourceUrl": source_clone_url(source.repo),
                    "sourceRevision": revision,
                    "skillPath": file.relative_to(prepared).as_posix(),
                    "skillFolderHash": _skill_folder_hash(file.parent),
                }
    for phase in ("fresh", "repeat"):
        results = install_skills(paths, checkouts=checkouts, runner=runner)
        if len(results) != len(sources) or any(
            result.returncode or result.skipped for result in results
        ):
            raise CatalogError(f"{phase} skill-source installation failed")
        state = skill_state(paths, expected)
        if phase == "fresh":
            first = state
        elif state != first:
            raise CatalogError("repeat skills changed content, provenance or installed timestamps")
    return {"sources": first["sources"], "skills": expected, "repeat_content_and_provenance": True}


def smoke(manifest: Path):
    snapshot, sources, _ = load_qualification_context(manifest)
    system = platform.freedesktop_os_release()
    if sources["agentbot"] != ROOT or not Path("/.dockerenv").is_file() or os.geteuid() == 0:
        raise CatalogError("integration smoke requires its frozen source in a non-root container")
    if (system.get("ID"), system.get("VERSION_ID"), platform.machine(), sys.version_info[:2]) != (
        "ubuntu",
        "24.04",
        "x86_64",
        (3, 12),
    ):
        raise CatalogError("integration smoke requires Ubuntu 24.04 AMD64 and Python 3.12")
    home = Path.home()
    os.environ["AGENTBOT_HOME"] = str(ROOT)
    os.environ["SKILLS_DISABLE_TELEMETRY"] = "1"
    from src.software_versions import qualification_manifest_operation

    work = Path(tempfile.mkdtemp(prefix="catalog-integrations-", dir=home))
    private = private_context(manifest, work / "snapshot")
    runner = RecordingRunner()
    with qualification_manifest_operation(private, ROOT) as active:
        if active.digest != snapshot.digest:
            raise CatalogError("integration candidate changed during handoff")
        editor = prepare_editor(home, work, sources, runner)
        extensions = extensions_smoke(home, active, runner)
        skills = skills_smoke(home, work, runner)
    load_qualification_context(manifest)
    return {
        "schema_version": 1,
        "scope": "integration-smoke-only",
        "approved": False,
        "catalog_digest": snapshot.digest,
        "sources": dict(snapshot.data["sources"]),
        "editor": editor,
        "extensions": extensions,
        "skills": skills,
        "fresh": "success",
        "repeat": "success",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("context", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.output.exists() or args.output.is_symlink():
            raise CatalogError("preserving an existing integration evidence file")
        result = smoke(args.context.resolve())
        with args.output.open("x") as stream:
            json.dump(result, stream, indent=2)
            stream.write("\n")
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"Integration qualification held: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
