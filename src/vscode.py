"""VS Code host detection, extension reconciliation, and settings merging.

Remote-WSL splits one editor across two hosts, and they are not
interchangeable: extensions are installed per host, the Windows profile holds
the user-scope settings both hosts read, and the WSL server holds machine-scope
settings only the remote reads. Each host also has its own CLI, and picking the
wrong one silently mutates the other host -- `code` on ``PATH`` inside WSL is
the Windows executable reached through interop, so it is right for the Windows
host and wrong for the WSL one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .atomic_io import write_text_atomic
from .command_runner import CommandRunner

SUPPORTED_VERSION = 1
INSTALL_TIMEOUT_SECONDS = 300.0
SETTINGS_DIRECTORY = "vscode"

WINDOWS_MOUNT_ROOT = Path("/mnt/c")

# "publisher.name-1.2.3" and "publisher.name-1.2.3-linux-x64" both identify
# "publisher.name". The version is the first hyphen-separated field that starts
# with a digit, which is what separates an id from a build suffix.
_EXTENSION_DIRECTORY = re.compile(r"^(?P<identifier>.+?)-\d+\.\d+\.\d+.*$")


@dataclass(frozen=True)
class VSCodeHost:
    """One extension host: where its settings live and how to drive its CLI."""

    name: str
    settings_path: Path
    extensions_dir: Path
    cli: Path | None
    detail: str

    @property
    def available(self) -> bool:
        return self.extensions_dir.is_dir()

    @property
    def can_install(self) -> bool:
        return self.available and self.cli is not None


@dataclass
class SettingsPlan:
    """What a settings merge would change, before anything is written."""

    path: Path
    additions: dict[str, object] = field(default_factory=dict)
    changes: dict[str, tuple[object, object]] = field(default_factory=dict)
    unreadable: str | None = None

    @property
    def is_noop(self) -> bool:
        return not self.additions and not self.changes


@dataclass
class ExtensionPlan:
    """Which extensions a host is missing. Never which ones to remove."""

    host: str
    missing: tuple[str, ...] = ()
    unmanaged: tuple[str, ...] = ()
    skipped: str | None = None

    @property
    def is_noop(self) -> bool:
        return not self.missing


def wsl_host(home: Path) -> VSCodeHost:
    """The WSL extension host, served from ~/.vscode-server."""
    server = home / ".vscode-server"
    return VSCodeHost(
        name="wsl",
        settings_path=server / "data" / "Machine" / "settings.json",
        extensions_dir=server / "extensions",
        cli=_newest_remote_cli(server),
        detail=str(server),
    )


def windows_host(mount_root: Path = WINDOWS_MOUNT_ROOT) -> VSCodeHost:
    """The Windows host, reached over the WSL mount.

    The profile is resolved by looking for exactly one VS Code user directory.
    Zero means VS Code is not installed on the Windows side; more than one means
    several Windows accounts, and guessing which one the operator meant would be
    a write into somebody's profile on a coin flip.
    """
    candidates = sorted((mount_root / "Users").glob("*/AppData/Roaming/Code/User"))
    if len(candidates) != 1:
        detail = (
            "no Windows VS Code profile found"
            if not candidates
            else f"{len(candidates)} Windows profiles found; cannot choose one"
        )
        return VSCodeHost(
            name="windows",
            settings_path=mount_root / "Users",
            extensions_dir=mount_root / "Users",
            cli=None,
            detail=detail,
        )
    user_dir = candidates[0]
    profile = user_dir.parents[3]
    return VSCodeHost(
        name="windows",
        settings_path=user_dir / "settings.json",
        extensions_dir=profile / ".vscode" / "extensions",
        cli=_windows_cli(mount_root),
        detail=str(profile),
    )


def _newest_remote_cli(server: Path) -> Path | None:
    """The remote CLI belonging to the most recently used server build.

    Never `code` from PATH: inside WSL that is the Windows executable, and
    using it here would install into the other host.
    """
    candidates = [
        candidate for candidate in server.glob("bin/*/bin/remote-cli/code") if candidate.is_file()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _windows_cli(mount_root: Path) -> Path | None:
    for relative in (
        "Program Files/Microsoft VS Code/bin/code",
        "Program Files (x86)/Microsoft VS Code/bin/code",
    ):
        candidate = mount_root / relative
        if candidate.is_file():
            return candidate
    return None


def installed_extensions(host: VSCodeHost) -> tuple[str, ...]:
    """Extension ids present in a host's extensions directory.

    Read from disk rather than from `code --list-extensions`: the CLI is slow,
    needs the host to be runnable, and on the Windows side runs through interop.
    """
    if not host.available:
        return ()
    identifiers = set()
    for entry in host.extensions_dir.iterdir():
        if not entry.is_dir():
            continue
        matched = _EXTENSION_DIRECTORY.match(entry.name)
        if matched:
            identifiers.add(matched.group("identifier"))
    return tuple(sorted(identifiers))


def plan_extensions(host: VSCodeHost, desired: list[str]) -> ExtensionPlan:
    """Which desired extensions are absent from this host.

    Extensions present but unmanaged are reported, never removed: absence from
    the manifest is not a request to uninstall.
    """
    if not host.available:
        return ExtensionPlan(host=host.name, skipped=f"host unavailable ({host.detail})")
    present = set(installed_extensions(host))
    wanted = {identifier.strip() for identifier in desired if identifier.strip()}
    return ExtensionPlan(
        host=host.name,
        missing=tuple(sorted(wanted - present)),
        unmanaged=tuple(sorted(present - wanted)),
    )


def strip_jsonc(text: str) -> str:
    """JSONC with comments and trailing commas removed, for parsing only.

    The result is never written back; writes edit the original text in place so
    comments and formatting survive.
    """
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character == '"':
            end = _end_of_string(text, index)
            out.append(text[index:end])
            index = end
            continue
        if text.startswith("//", index):
            newline = text.find("\n", index)
            index = length if newline == -1 else newline
            continue
        if text.startswith("/*", index):
            close = text.find("*/", index + 2)
            index = length if close == -1 else close + 2
            continue
        out.append(character)
        index += 1
    without_comments = "".join(out)
    return re.sub(r",(\s*[}\]])", r"\1", without_comments)


def _end_of_string(text: str, start: int) -> int:
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == '"':
            return index + 1
        index += 1
    return len(text)


def read_settings(path: Path) -> tuple[dict, str | None]:
    """Parse a JSONC settings file. Returns (settings, error)."""
    if not path.is_file():
        return {}, None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        return {}, f"cannot read {path}: {error}"
    stripped = strip_jsonc(raw).strip()
    if not stripped:
        return {}, None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as error:
        return {}, f"cannot parse {path}: {error}"
    if not isinstance(parsed, dict):
        return {}, f"{path} is not a JSON object"
    return parsed, None


def plan_settings(path: Path, desired: dict[str, object]) -> SettingsPlan:
    existing, error = read_settings(path)
    if error:
        return SettingsPlan(path=path, unreadable=error)
    plan = SettingsPlan(path=path)
    for key, value in desired.items():
        if key not in existing:
            plan.additions[key] = value
        elif existing[key] != value:
            plan.changes[key] = (existing[key], value)
    return plan


def merge_settings_text(text: str, desired: dict[str, object]) -> str:
    """Apply owned keys to JSONC text, leaving everything else byte-identical.

    Values are replaced in place and new keys appended, rather than the file
    being re-serialised from a parsed object: re-serialising would silently
    delete every comment and reformat settings the operator wrote by hand.
    """
    if not desired:
        return text
    working = text if text.strip() else "{}"
    for key, value in desired.items():
        span = _top_level_value_span(working, key)
        if span is None:
            working = _append_key(working, key, json.dumps(value, indent=2))
            continue
        start, end = span
        if _value_already_matches(working[start:end], value):
            continue
        working = working[:start] + _render_value(working, start, value) + working[end:]
    return working


def _value_already_matches(current: str, value: object) -> bool:
    """Whether the file already says what the manifest declares.

    Re-rendering a value that has not changed is not free: an object value is
    replaced whole, so re-rendering deletes any comment written inside it and
    reflows its indentation. Before this check, changing one scalar key rewrote
    every other declared key too, which silently stripped comments from blocks
    the operator never touched.
    """
    try:
        return json.loads(strip_jsonc(current)) == value
    except ValueError:
        return False


def _render_value(text: str, start: int, value: object) -> str:
    """Serialise a value for the position it is being spliced into.

    json.dumps indents from column zero. A nested value spliced in unchanged
    leaves its body and closing brace hanging at the wrong depth, so continuation
    lines are shifted to the indentation of the key's own line and the file's own
    line ending is kept.
    """
    rendered = json.dumps(value, indent=2)
    if "\n" not in rendered:
        return rendered
    line_start = text.rfind("\n", 0, start) + 1
    indent = text[line_start : len(text[line_start:]) - len(text[line_start:].lstrip()) + line_start]
    newline = "\r\n" if "\r\n" in text else "\n"
    head, *rest = rendered.split("\n")
    return newline.join([head] + [indent + line for line in rest])


def _top_level_value_span(text: str, key: str) -> tuple[int, int] | None:
    """Byte span of a top-level key's value, or None when the key is absent."""
    depth = 0
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character == '"':
            end = _end_of_string(text, index)
            if depth == 1:
                name = json.loads(strip_jsonc(text[index:end]))
                colon = _skip_to_colon(text, end)
                if colon is not None and name == key:
                    value_start = _skip_whitespace_and_comments(text, colon + 1)
                    return value_start, _end_of_value(text, value_start)
            index = end
            continue
        if text.startswith("//", index):
            newline = text.find("\n", index)
            index = length if newline == -1 else newline
            continue
        if text.startswith("/*", index):
            close = text.find("*/", index + 2)
            index = length if close == -1 else close + 2
            continue
        if character in "{[":
            depth += 1
        elif character in "}]":
            depth -= 1
        index += 1
    return None


def _skip_to_colon(text: str, index: int) -> int | None:
    position = _skip_whitespace_and_comments(text, index)
    if position < len(text) and text[position] == ":":
        return position
    return None


def _skip_whitespace_and_comments(text: str, index: int) -> int:
    length = len(text)
    while index < length:
        if text[index].isspace():
            index += 1
            continue
        if text.startswith("//", index):
            newline = text.find("\n", index)
            index = length if newline == -1 else newline
            continue
        if text.startswith("/*", index):
            close = text.find("*/", index + 2)
            index = length if close == -1 else close + 2
            continue
        break
    return index


def _end_of_value(text: str, start: int) -> int:
    """Byte after the last character of the value itself.

    The span deliberately excludes the whitespace and any trailing comment that
    follow the value. Including them meant replacing a value also deleted the
    operator's end-of-line note and the newline before the closing brace, so a
    file lost a comment and gained a joined line on every apply.
    """
    if start >= len(text):
        return start
    if text[start] == '"':
        return _end_of_string(text, start)
    depth = 0
    index = start
    length = len(text)
    while index < length:
        character = text[index]
        if character == '"':
            index = _end_of_string(text, index)
            continue
        if text.startswith("//", index):
            if depth == 0:
                return _trim_trailing_space(text, start, index)
            newline = text.find("\n", index)
            index = length if newline == -1 else newline
            continue
        if text.startswith("/*", index):
            if depth == 0:
                return _trim_trailing_space(text, start, index)
            close = text.find("*/", index + 2)
            index = length if close == -1 else close + 2
            continue
        if character in "{[":
            depth += 1
        elif character in "}]":
            if depth == 0:
                return _trim_trailing_space(text, start, index)
            depth -= 1
            if depth == 0:
                return index + 1
        elif character == "," and depth == 0:
            return _trim_trailing_space(text, start, index)
        index += 1
    return _trim_trailing_space(text, start, length)


def _trim_trailing_space(text: str, start: int, end: int) -> int:
    while end > start and text[end - 1].isspace():
        end -= 1
    return end


def _append_key(text: str, key: str, rendered: str) -> str:
    close = text.rfind("}")
    if close == -1:
        return json.dumps({key: json.loads(rendered)}, indent=2) + "\n"
    # Match the file's own line ending. Settings written on the Windows side
    # are CRLF, and appending LF would leave one mixed line in an otherwise
    # consistent file the operator maintains by hand.
    newline = "\r\n" if "\r\n" in text else "\n"
    head = text[:close].rstrip()
    # A trailing comma before the closing brace is legal JSONC and common in
    # hand-written settings. A second one is legal nowhere, and appending it
    # broke every real file that had one.
    separator = "" if head.endswith(("{", ",")) else ","
    # Continuation lines carry the appended key's own indentation, for the same
    # reason a replaced value does: json.dumps indents from column zero, which
    # would leave every nested body and closing brace hanging at the margin.
    first, *rest = rendered.split("\n")
    body = newline.join([first] + ["  " + line for line in rest])
    entry = f"{separator}{newline}  {json.dumps(key)}: {body}{newline}"
    return head + entry + text[close:]


def apply_settings(path: Path, desired: dict[str, object]) -> SettingsPlan:
    """Merge owned keys into a settings file, backing the original up first."""
    plan = plan_settings(path, desired)
    if plan.unreadable or plan.is_noop:
        return plan
    original = path.read_text(encoding="utf-8") if path.is_file() else "{}\n"
    merged = merge_settings_text(original, desired)
    write_text_atomic(path, merged, backup=True)
    return plan


class VSCodeManifestError(ValueError):
    """Raised when vscode.yaml is present but unusable."""


@dataclass(frozen=True)
class VSCodeManifest:
    """Desired state, kept per host because the hosts are not interchangeable.

    Only extensions live here. Settings are authored as JSON under `vscode/`,
    because YAML's implicit typing corrupts real VS Code values: `files.autoSave:
    off` is the string "off" to VS Code and the boolean false to YAML.
    """

    extensions: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def extensions_for(self, host: str) -> tuple[str, ...]:
        return self.extensions.get(host, ())

    @property
    def is_empty(self) -> bool:
        return not any(self.extensions.values())


def manifest_path(root: Path) -> Path:
    return root / "vscode.yaml"


def load_manifest(path: Path) -> VSCodeManifest:
    """Read vscode.yaml. A missing file is an empty manifest, not an error."""
    if not path.is_file():
        return VSCodeManifest()
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise VSCodeManifestError(f"cannot parse {path}: {error}") from error
    if not isinstance(document, dict):
        raise VSCodeManifestError(f"{path} must be a mapping")
    version = document.get("version", SUPPORTED_VERSION)
    if version != SUPPORTED_VERSION:
        raise VSCodeManifestError(f"{path} has unsupported version {version!r}")

    extensions: dict[str, tuple[str, ...]] = {}
    for host, values in (document.get("extensions") or {}).items():
        if not isinstance(values, list):
            raise VSCodeManifestError(f"{path}: extensions.{host} must be a list")
        extensions[str(host)] = tuple(str(value) for value in values)

    if document.get("settings"):
        raise VSCodeManifestError(
            f"{path}: settings moved to {SETTINGS_DIRECTORY}/settings.<scope>.json. "
            "YAML turns VS Code values like `files.autoSave: off` into booleans."
        )

    return VSCodeManifest(extensions=extensions)


def seed_manifest(path: Path, hosts: dict[str, VSCodeHost]) -> VSCodeManifest:
    """Write the currently installed extensions into the manifest.

    Settings are never seeded: copying a whole settings file into the desired
    state would claim ownership of every key in it, and ownership is the one
    thing this manifest is supposed to make explicit.
    """
    existing = load_manifest(path)
    extensions = dict(existing.extensions)
    for name, host in hosts.items():
        if host.available:
            extensions[name] = installed_extensions(host)
    seeded = VSCodeManifest(extensions=extensions)
    write_text_atomic(path, render_manifest(seeded), backup=path.is_file())
    return seeded


def render_manifest(manifest: VSCodeManifest) -> str:
    document: dict[str, object] = {"version": SUPPORTED_VERSION}
    if manifest.extensions:
        document["extensions"] = {
            host: list(values) for host, values in sorted(manifest.extensions.items())
        }
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)


def install_extensions(
    host: VSCodeHost,
    identifiers: tuple[str, ...],
    runner: CommandRunner,
) -> dict[str, str]:
    """Install each identifier through this host's own CLI.

    One invocation per extension: a single call with several `--install-extension`
    flags reports one exit status for the batch, so a partial failure would be
    recorded against all of them.
    """
    results: dict[str, str] = {}
    if not identifiers:
        return results
    if host.cli is None:
        return dict.fromkeys(identifiers, "no CLI for this host")
    for identifier in identifiers:
        outcome = runner.run(
            [str(host.cli), "--install-extension", identifier, "--force"],
            timeout_seconds=INSTALL_TIMEOUT_SECONDS,
        )
        results[identifier] = "installed" if outcome.returncode == 0 else outcome.detail()
    return results


@dataclass
class VSCodeReport:
    """One preview or one applied run, across every resolved host."""

    hosts: dict[str, VSCodeHost] = field(default_factory=dict)
    extensions: dict[str, ExtensionPlan] = field(default_factory=dict)
    settings: dict[str, SettingsPlan] = field(default_factory=dict)
    installed: dict[str, dict[str, str]] = field(default_factory=dict)
    applied: bool = False
    manifest_error: str | None = None

    @property
    def has_work(self) -> bool:
        return any(not plan.is_noop for plan in self.extensions.values()) or any(
            not plan.is_noop for plan in self.settings.values()
        )

    @property
    def failures(self) -> tuple[str, ...]:
        problems = [
            f"{scope}: {plan.unreadable}"
            for scope, plan in sorted(self.settings.items())
            if plan.unreadable
        ]
        for host, outcomes in sorted(self.installed.items()):
            problems.extend(
                f"{host}: {identifier} ({detail})"
                for identifier, detail in sorted(outcomes.items())
                if detail != "installed"
            )
        return tuple(problems)


def resolve_hosts(home: Path, mount_root: Path = WINDOWS_MOUNT_ROOT) -> dict[str, VSCodeHost]:
    return {"wsl": wsl_host(home), "windows": windows_host(mount_root)}


UNIVERSAL_SCOPE = "universal"


def settings_source(root: Path, scope: str) -> Path:
    """Where a scope's desired settings are authored.

    JSON, not a YAML block: these files are pasted from and compared against
    real settings.json, and YAML's implicit typing silently rewrites VS Code
    values -- `files.autoSave: off` is the string "off" to VS Code and the
    boolean false to YAML.
    """
    return root / SETTINGS_DIRECTORY / f"settings.{scope}.json"


def desired_settings(root: Path, host: str) -> tuple[dict[str, object], str | None]:
    """Universal keys with this host's overrides on top, plus any read error."""
    merged: dict[str, object] = {}
    for scope in (UNIVERSAL_SCOPE, host):
        values, error = read_settings(settings_source(root, scope))
        if error:
            return {}, error
        merged.update(values)
    return merged, None


def preview(home: Path, root: Path, mount_root: Path = WINDOWS_MOUNT_ROOT) -> VSCodeReport:
    """What a run would change. Writes nothing."""
    report = VSCodeReport(hosts=resolve_hosts(home, mount_root))
    try:
        manifest = load_manifest(manifest_path(root))
    except VSCodeManifestError as error:
        report.manifest_error = str(error)
        return report

    for name, host in report.hosts.items():
        report.extensions[name] = plan_extensions(host, list(manifest.extensions_for(name)))

    for name, host in report.hosts.items():
        desired, source_error = desired_settings(root, name)
        if source_error:
            report.settings[name] = SettingsPlan(path=host.settings_path, unreadable=source_error)
            continue
        if not desired:
            continue
        if not host.available:
            report.settings[name] = SettingsPlan(
                path=host.settings_path,
                unreadable=f"host unavailable ({host.detail})",
            )
            continue
        report.settings[name] = plan_settings(host.settings_path, desired)
    return report


def doctor_vscode(home: Path, root: Path) -> list[tuple[str, str, str]]:
    """Doctor rows: one per host, plus one for an unusable manifest.

    VS Code was the only one of the three platform features with no Doctor
    coverage at all, so drift in it was invisible to every lifecycle command.
    """
    report = preview(home, root)
    if report.manifest_error:
        return [("vscode manifest", report.manifest_error, "check")]
    rows: list[tuple[str, str, str]] = []
    for host in sorted(set(report.extensions) | set(report.settings)):
        extensions = report.extensions.get(host)
        settings = report.settings.get(host)
        if extensions is not None and extensions.skipped:
            rows.append((f"vscode {host}", extensions.skipped, "skipped"))
            continue
        if settings is not None and settings.unreadable:
            rows.append((f"vscode {host}", settings.unreadable, "check"))
            continue
        pending = []
        if extensions is not None and not extensions.is_noop:
            pending.append(f"{len(extensions.missing)} extension(s) to install")
        if settings is not None and not settings.is_noop:
            pending.append(f"{len(settings.additions)} to add, {len(settings.changes)} to change")
        if pending:
            rows.append((f"vscode {host}", "; ".join(pending), "check"))
        else:
            rows.append((f"vscode {host}", "current", "ok"))
    return rows


def apply(
    home: Path,
    root: Path,
    runner: CommandRunner,
    mount_root: Path = WINDOWS_MOUNT_ROOT,
) -> VSCodeReport:
    """Install missing extensions and merge owned settings keys.

    Settings are merged before extensions: a settings file that cannot be read
    stops that scope, and finding that out after a five-minute extension install
    wastes the operator's time for no gain.
    """
    report = preview(home, root, mount_root)
    if report.manifest_error:
        return report
    for name, host in report.hosts.items():
        settings_plan = report.settings.get(name)
        if settings_plan is None or settings_plan.unreadable or settings_plan.is_noop:
            continue
        desired, source_error = desired_settings(root, name)
        if source_error:
            continue
        report.settings[name] = apply_settings(host.settings_path, desired)

    for name, host in report.hosts.items():
        extension_plan = report.extensions.get(name)
        if extension_plan is None or extension_plan.is_noop:
            continue
        report.installed[name] = install_extensions(host, extension_plan.missing, runner)

    report.applied = True
    return report
