"""Preview-first ownership of the supported CLI configuration files.

Three CLIs, three formats, one model: a desired-state file per CLI holding only
the keys Agentbot owns, merged key by key into the operator's config. Everything
not named in the desired state is left exactly as it was.

The desired state is authored in each CLI's own format -- JSON for Claude and
Cursor, TOML for Codex -- rather than a common one. A shared format has to be
translated, and translation is where values quietly change shape: this is the
same reason the VS Code settings in `vscode/` are JSON and not YAML.

Not covered here: installing the CLIs (Dotfiles owns that), MCP server lists,
and VS Code settings.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .atomic_io import write_text_atomic
from .paths import AgentbotPaths
from .vscode import merge_settings_text, read_settings, strip_jsonc

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on older interpreters only
    import tomli as tomllib

SOURCE_DIRECTORY = "cli"

# Key names whose value is almost certainly a credential. The desired-state
# files are committed to a repository, so a literal here would be a secret in
# version control. Env var names are fine; values are not.
_SECRET_NAME = re.compile(r"(token|secret|password|passwd|apikey|api_key|credential)", re.I)

# Keys that hold a credential whatever they are called. The name heuristic above
# cannot see Cursor's: "authInfo" contains none of those words, and its value is
# an object rather than a string, so it failed both conditions and would have
# been accepted into a committed desired-state file.
#
# Matched exactly, not as substrings: "authSource" names where a secret lives
# and is deliberately allowed.
_CREDENTIAL_KEYS = frozenset({"authinfo", "auth", "credentials"})


@dataclass(frozen=True)
class ManagedCli:
    name: str
    config_path: Path
    fmt: str
    source_name: str


@dataclass
class CliConfigPlan:
    """What one CLI's merge would change, before anything is written."""

    cli: str
    path: Path
    additions: dict[str, object] = field(default_factory=dict)
    changes: dict[str, tuple[object, object]] = field(default_factory=dict)
    skipped: str | None = None
    error: str | None = None

    @property
    def is_noop(self) -> bool:
        return not self.additions and not self.changes


def managed_clis(paths: AgentbotPaths) -> tuple[ManagedCli, ...]:
    """The CLIs Agentbot is approved to configure, with verified paths."""
    return (
        ManagedCli("claude", paths.claude_home / "settings.json", "json", "claude.settings.json"),
        ManagedCli("codex", paths.codex_home / "config.toml", "toml", "codex.config.toml"),
        ManagedCli(
            "cursor", paths.cursor_home / "cli-config.json", "json", "cursor.cli-config.json"
        ),
    )


def desired_source(root: Path, cli: ManagedCli) -> Path:
    return root / SOURCE_DIRECTORY / cli.source_name


def _secret_keys(desired: dict[str, object], prefix: str = "") -> tuple[str, ...]:
    """Credential-shaped keys anywhere in a desired-state file.

    Nested objects are searched too: a secret one level down is still a secret
    in version control, and the flat check could not see one.
    """
    found: list[str] = []
    for key, value in sorted(desired.items()):
        path = f"{prefix}{key}"
        if key.lower() in _CREDENTIAL_KEYS:
            found.append(path)
        elif _SECRET_NAME.search(key) and isinstance(value, str) and value:
            found.append(path)
        elif isinstance(value, dict):
            found.extend(_secret_keys(value, f"{path}."))
    return tuple(found)


def read_desired(root: Path, cli: ManagedCli) -> tuple[dict[str, object], str | None]:
    """Parse a CLI's desired-state file. A missing file means nothing is owned."""
    source = desired_source(root, cli)
    if not source.is_file():
        return {}, None
    text = source.read_text(encoding="utf-8")
    if cli.fmt == "json":
        parsed, error = read_settings(source)
        if error:
            return {}, error
    else:
        try:
            parsed = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            return {}, f"cannot parse {source}: {error}"
        nested = sorted(key for key, value in parsed.items() if isinstance(value, dict))
        if nested:
            # Merging a TOML table means rewriting a whole section without
            # disturbing the rest of it, which this does not attempt. Refusing
            # is better than half-writing somebody's [hooks] block.
            return {}, f"{source}: tables are not supported yet ({', '.join(nested)})"

    secrets = _secret_keys(parsed)
    if secrets:
        return {}, (
            f"{source}: refusing to manage credential-shaped keys ({', '.join(secrets)}). "
            "Reference an environment variable name instead of a value."
        )
    return parsed, None


def read_current(cli: ManagedCli) -> tuple[dict[str, object], str | None]:
    if not cli.config_path.is_file():
        return {}, None
    if cli.fmt == "json":
        return read_settings(cli.config_path)
    try:
        return tomllib.loads(cli.config_path.read_text(encoding="utf-8")), None
    except tomllib.TOMLDecodeError as error:
        return {}, f"cannot parse {cli.config_path}: {error}"
    except OSError as error:
        return {}, f"cannot read {cli.config_path}: {error}"


def plan_cli(root: Path, cli: ManagedCli) -> CliConfigPlan:
    plan = CliConfigPlan(cli=cli.name, path=cli.config_path)
    desired, error = read_desired(root, cli)
    if error:
        plan.error = error
        return plan
    if not desired:
        plan.skipped = "nothing declared"
        return plan
    if not cli.config_path.parent.is_dir():
        # A missing config directory means the CLI is not installed here.
        plan.skipped = f"{cli.config_path.parent} does not exist"
        return plan

    current, error = read_current(cli)
    if error:
        plan.error = error
        return plan
    for key, value in desired.items():
        if key not in current:
            plan.additions[key] = value
        elif current[key] != value:
            plan.changes[key] = (current[key], value)
    return plan


def _render_toml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_render_toml_scalar(item) for item in value) + "]"
    raise ValueError(f"unsupported TOML value: {value!r}")


def merge_toml_text(text: str, desired: dict[str, object]) -> str:
    """Set top-level keys in TOML text, leaving tables and comments alone.

    Only the region before the first table header is rewritten, because a bare
    `key = value` after a `[table]` header belongs to that table, not to the
    document root.
    """
    lines = text.splitlines(keepends=True)
    first_table = len(lines)
    for index, line in enumerate(lines):
        if line.lstrip().startswith("["):
            first_table = index
            break

    head = lines[:first_table]
    tail = lines[first_table:]
    try:
        current = tomllib.loads("".join(head))
    except tomllib.TOMLDecodeError:
        current = {}
    for key, value in desired.items():
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        for index, line in enumerate(head):
            if not pattern.match(line):
                continue
            # A key already holding the declared value is left alone: rewriting
            # it would drop the operator's note for no change in meaning.
            if key in current and current[key] == value:
                break
            head[index] = f"{key} = {_render_toml_scalar(value)}{_trailing_comment(line)}\n"
            break
        else:
            # Appending to a file whose last line has no newline produced
            # `model = "gpt-5"effort = "low"`, which is not TOML at all. The
            # merge was rejected by the verify step, so apply failed outright on
            # any config that did not end in a newline.
            if head and not head[-1].endswith("\n"):
                head[-1] += "\n"
            head.append(f"{key} = {_render_toml_scalar(value)}\n")
    return "".join(head) + "".join(tail)


def _trailing_comment(line: str) -> str:
    """The ` # ...` a key line ends with, or an empty string.

    Replacing a value used to truncate the line at the value, so a note the
    operator wrote beside a setting disappeared the first time its value moved.
    """
    quote = ""
    for index, character in enumerate(line):
        if quote:
            if character == quote:
                quote = ""
            continue
        if character in "\"'":
            quote = character
        elif character == "#":
            return " " + line[index:].strip()
    return ""


def _merged_text(cli: ManagedCli, original: str, desired: dict[str, object]) -> str:
    if cli.fmt == "json":
        return merge_settings_text(original or "{}", desired)
    return merge_toml_text(original, desired)


def _verify(cli: ManagedCli, text: str, desired: dict[str, object]) -> None:
    """Re-parse what we produced and confirm the owned keys actually landed.

    The input parsed a moment ago, so an unparseable result is this code's
    fault, not the operator's file.
    """
    if cli.fmt == "json":
        parsed = json.loads(strip_jsonc(text))
    else:
        parsed = tomllib.loads(text)
    for key, value in desired.items():
        if parsed.get(key) != value:
            raise ValueError(f"{cli.name}: {key} did not survive the merge")


@dataclass
class CliConfigReport:
    plans: dict[str, CliConfigPlan] = field(default_factory=dict)
    applied: bool = False
    rolled_back: tuple[str, ...] = ()

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(
            f"{plan.cli}: {plan.error}" for plan in self.plans.values() if plan.error
        )

    @property
    def has_work(self) -> bool:
        return any(not plan.is_noop and not plan.error for plan in self.plans.values())


def preview(paths: AgentbotPaths) -> CliConfigReport:
    report = CliConfigReport()
    for cli in managed_clis(paths):
        report.plans[cli.name] = plan_cli(paths.root, cli)
    return report


def apply(paths: AgentbotPaths) -> CliConfigReport:
    """Merge every CLI's owned keys, rolling the whole set back on any failure.

    A half-applied run across three agents is worse than none: the operator
    would have to work out which two of three now disagree with the desired
    state. Originals are captured before the first write and restored if any
    target fails.
    """
    report = preview(paths)
    if report.failures:
        return report

    originals: dict[Path, str | None] = {}
    try:
        for cli in managed_clis(paths):
            plan = report.plans[cli.name]
            if plan.error or plan.skipped or plan.is_noop:
                continue
            desired, _ = read_desired(paths.root, cli)
            original = (
                cli.config_path.read_text(encoding="utf-8") if cli.config_path.is_file() else None
            )
            merged = _merged_text(cli, original or "", desired)
            _verify(cli, merged, desired)
            write_text_atomic(cli.config_path, merged, backup=cli.config_path.is_file())
            # Recorded only after the write succeeds: a target that was never
            # written needs no undoing, and trying to undo it is how a rollback
            # turns one failure into two.
            originals[cli.config_path] = original
    except (ValueError, OSError, json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
        restored = []
        for path, original in originals.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                write_text_atomic(path, original)
            restored.append(str(path))
        report.rolled_back = tuple(sorted(restored))
        for plan in report.plans.values():
            plan.error = plan.error or f"rolled back: {error}"
        return report

    report.applied = True
    return report


def doctor_cli_configs(paths: AgentbotPaths) -> list[tuple[str, str, str]]:
    """Doctor rows: one per managed CLI."""
    rows: list[tuple[str, str, str]] = []
    for name, plan in sorted(preview(paths).plans.items()):
        if plan.error:
            rows.append((f"{name} config", plan.error, "check"))
        elif plan.skipped:
            rows.append((f"{name} config", plan.skipped, "skipped"))
        elif plan.is_noop:
            rows.append((f"{name} config", "current", "ok"))
        else:
            summary = f"{len(plan.additions)} to add, {len(plan.changes)} to change"
            rows.append((f"{name} config", summary, "check"))
    return rows
