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

import copy
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
        nested = sorted(
            f"{key}.{inner}"
            for key, value in parsed.items()
            if isinstance(value, dict)
            for inner, item in value.items()
            if isinstance(item, dict)
        )
        if nested:
            # One table level is merged key by key. Deeper nesting would mean
            # rewriting sections such as [hooks.state] without disturbing the
            # rest of them, which this does not attempt.
            return {}, f"{source}: nested tables are not supported ({', '.join(nested)})"

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
    for key, value in _owned(cli, desired):
        present, existing = _lookup(current, key)
        if not present:
            plan.additions[key] = value
        elif existing != value:
            plan.changes[key] = (existing, value)
    return plan


def _owned(cli: ManagedCli, desired: dict[str, object]) -> list[tuple[str, object]]:
    """Owned keys, with a TOML table's keys named `table.key`."""
    if cli.fmt == "json":
        return list(desired.items())
    owned: list[tuple[str, object]] = []
    for key, value in desired.items():
        if isinstance(value, dict):
            owned.extend((f"{key}.{inner}", item) for inner, item in value.items())
        else:
            owned.append((key, value))
    return owned


def _lookup(current: dict[str, object], key: str) -> tuple[bool, object]:
    if key in current:
        return True, current[key]
    table, _, inner = key.partition(".")
    section = current.get(table)
    if inner and isinstance(section, dict) and inner in section:
        return True, section[inner]
    return False, None


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
    """Set owned keys in TOML text, leaving everything else and comments alone.

    Top-level keys are set only in the region before the first table header,
    because a bare `key = value` after a `[table]` header belongs to that
    table, not to the document root. A declared table's keys are set inside
    that table's own region, and a missing table is appended.

    Only lines that start a statement count as headers or keys: a line inside
    a multiline string or array can look like `[features]` without being one.
    An owned key or table already written in a form this merge does not edit
    (an inline table, a dotted or quoted key) is refused before any change.
    """
    lines = text.splitlines(keepends=True)
    starts = _statement_starts(lines)
    _refuse_unsupported(text, lines, starts, desired)
    first_table = next(
        (i for i, line in enumerate(lines) if starts[i] and line.lstrip().startswith("[")),
        len(lines),
    )
    head, head_starts = lines[:first_table], starts[:first_table]
    tail, tail_starts = lines[first_table:], starts[first_table:]
    scalars = {key: value for key, value in desired.items() if not isinstance(value, dict)}
    _set_keys(head, head_starts, scalars)
    for table, values in desired.items():
        if isinstance(values, dict):
            tail, tail_starts = _set_table(tail, tail_starts, table, values)
    return "".join(head) + "".join(tail)


def _statement_starts(lines: list[str]) -> list[bool]:
    """For each line, whether it starts outside a multiline string, array, or inline table."""
    starts: list[bool] = []
    depth = 0
    multiline: str | None = None
    for line in lines:
        starts.append(depth == 0 and multiline is None)
        index = 0
        while index < len(line):
            if multiline:
                if line.startswith(multiline, index):
                    multiline = None
                    index += 3
                elif multiline == '"""' and line[index] == "\\":
                    index += 2
                else:
                    index += 1
                continue
            character = line[index]
            if character == "#":
                break
            if line.startswith('"""', index) or line.startswith("'''", index):
                multiline = line[index : index + 3]
                index += 3
                continue
            if character in "\"'":
                index += 1
                while index < len(line) and line[index] != character:
                    index += 2 if character == '"' and line[index] == "\\" else 1
                index += 1
                continue
            if character in "[{":
                depth += 1
            elif character in "]}":
                depth = max(0, depth - 1)
            index += 1
    return starts


def _key_line(key: str) -> re.Pattern[str]:
    return re.compile(rf"^\s*{re.escape(key)}\s*=")


def _table_header(table: str) -> re.Pattern[str]:
    return re.compile(rf"^\s*\[\s*{re.escape(table)}\s*\]\s*(#.*)?$")


def _refuse_unsupported(
    text: str, lines: list[str], starts: list[bool], desired: dict[str, object]
) -> None:
    """Stop before editing a key or table written in a form this merge cannot edit safely."""
    current = tomllib.loads(text) if text.strip() else {}
    first_table = next(
        (i for i, line in enumerate(lines) if starts[i] and line.lstrip().startswith("[")),
        len(lines),
    )
    for key, value in desired.items():
        if key not in current:
            continue
        if isinstance(value, dict):
            found = any(
                starts[i] and _table_header(key).match(line.rstrip("\n"))
                for i, line in enumerate(lines)
            )
            form = f"[{key}] is written as an inline table or with dotted keys"
        else:
            found = any(starts[i] and _key_line(key).match(lines[i]) for i in range(first_table))
            form = f"{key} is written as a quoted or dotted key"
        if not found:
            raise ValueError(f"{form}, which Agentbot does not edit; set it by hand")


def _set_keys(region: list[str], starts: list[bool], desired: dict[str, object]) -> None:
    """Set keys within one region: the document root or one table's body."""
    try:
        current = tomllib.loads("".join(region))
    except tomllib.TOMLDecodeError:
        current = {}
    for key, value in desired.items():
        pattern = _key_line(key)
        for index, line in enumerate(region):
            if not starts[index] or not pattern.match(line):
                continue
            # A key already holding the declared value is left alone: rewriting
            # it would drop the operator's note for no change in meaning.
            if key in current and current[key] == value:
                break
            region[index] = f"{key} = {_render_toml_scalar(value)}{_trailing_comment(line)}\n"
            break
        else:
            # Appending to a file whose last line has no newline produced
            # `model = "gpt-5"effort = "low"`, which is not TOML at all. The
            # merge was rejected by the verify step, so apply failed outright on
            # any config that did not end in a newline.
            if region and not region[-1].endswith("\n"):
                region[-1] += "\n"
            region.append(f"{key} = {_render_toml_scalar(value)}\n")
            starts.append(True)


def _set_table(
    lines: list[str], starts: list[bool], table: str, values: dict[str, object]
) -> tuple[list[str], list[bool]]:
    header = _table_header(table)
    start = next(
        (i for i, line in enumerate(lines) if starts[i] and header.match(line.rstrip("\n"))),
        None,
    )
    if start is None:
        if lines and not lines[-1].endswith("\n"):
            lines = [*lines[:-1], lines[-1] + "\n"]
        body: list[str] = []
        body_starts: list[bool] = []
        _set_keys(body, body_starts, values)
        separator = ["\n"] if lines and lines[-1].strip() else []
        added = [*separator, f"[{table}]\n", *body]
        return [*lines, *added], [*starts, *([True] * len(added))]
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if starts[i] and lines[i].lstrip().startswith("[")
        ),
        len(lines),
    )
    region = lines[start + 1 : end]
    region_starts = starts[start + 1 : end]
    # Blank lines before the next header stay after any key appended here.
    trailing = 0
    while trailing < len(region) and not region[len(region) - 1 - trailing].strip():
        trailing += 1
    body = region[: len(region) - trailing]
    body_starts = region_starts[: len(region) - trailing]
    _set_keys(body, body_starts, values)
    kept = region[len(region) - trailing :]
    merged = [*lines[: start + 1], *body, *kept, *lines[end:]]
    merged_starts = [
        *starts[: start + 1],
        *body_starts,
        *region_starts[len(region) - trailing :],
        *starts[end:],
    ]
    return merged, merged_starts


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


def _parse(cli: ManagedCli, text: str) -> dict[str, Any]:
    if cli.fmt == "json":
        return dict(json.loads(strip_jsonc(text or "{}")))
    return tomllib.loads(text)


def _verify(cli: ManagedCli, text: str, desired: dict[str, object], original: str = "") -> None:
    """Re-parse what we produced: the owned keys landed, and nothing else changed.

    The input parsed a moment ago, so an unparseable result, or any other
    value that moved, is this code's fault, not the operator's file.
    """
    parsed = _parse(cli, text)
    owned = _owned(cli, desired)
    for key, value in owned:
        present, landed = _lookup(parsed, key)
        if not present or landed != value:
            raise ValueError(f"{cli.name}: {key} did not survive the merge")
    keys = [key for key, _ in owned]
    if _without(parsed, keys) != _without(_parse(cli, original), keys):
        raise ValueError(f"{cli.name}: the merge would change settings Agentbot does not own")


def _without(document: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    """A copy without the owned keys; a table left empty by that goes too."""
    rest = copy.deepcopy(document)
    for key in keys:
        if key in rest:
            del rest[key]
            continue
        table, _, inner = key.partition(".")
        section = rest.get(table)
        if inner and isinstance(section, dict):
            section.pop(inner, None)
            if not section:
                del rest[table]
    return rest


@dataclass
class CliConfigReport:
    plans: dict[str, CliConfigPlan] = field(default_factory=dict)
    applied: bool = False
    rolled_back: tuple[str, ...] = ()

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(f"{plan.cli}: {plan.error}" for plan in self.plans.values() if plan.error)

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
            _verify(cli, merged, desired, original or "")
            # Recorded before the write: the replacement can succeed and the
            # directory sync after it fail, and that target must still be
            # rolled back. One that was never changed is skipped below.
            originals[cli.config_path] = original
            write_text_atomic(cli.config_path, merged, backup=cli.config_path.is_file())
    except (ValueError, OSError, json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
        restored = []
        for path, original in originals.items():
            now = path.read_text(encoding="utf-8") if path.is_file() else None
            if now == original:
                continue  # never replaced: nothing to undo
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
