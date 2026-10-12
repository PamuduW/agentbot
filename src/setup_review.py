"""Recurring setup review, run by the agent CLI the operator chooses.

Two phases. The review phase runs the chosen CLI non-interactively in its
read-only mode, captures what it prints, and saves that as the report under
private Agentbot state. The update phase is an ordinary interactive session of
the same CLI, started only after the operator says yes at a terminal.

Every CLI is resolved from the location Dotfiles installs it to, never from
PATH: inside WSL, `cursor` on PATH is the Windows editor, and the Windows
Antigravity editor can put an `agy` there the same way. None of them is ever
given a flag that skips its permission prompts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .atomic_io import write_text_atomic
from .cli_config import _CREDENTIAL_KEYS, _SECRET_NAME
from .memory import SECRET_RULES
from .paths import AgentbotPaths
from .skills_sources import load_skills_sources, source_clone_url

PROMPT_VERSION = "setup-review/1"
RECURSION_ENV = "AGENTBOT_SETUP_REVIEW"
DEFAULT_TIMEOUT_MINUTES = 45
DEFAULT_SOURCE_BUDGET = 40
CLONE_TIMEOUT_SECONDS = 120

Scope = Literal["setup", "cli-config"]

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_ACCESS = 3
EXIT_TIMEOUT = 124
EXIT_CANCELLED = 130


@dataclass(frozen=True)
class CliSpec:
    key: str
    label: str
    #: Where Dotfiles installs it, relative to the home folder, in order.
    locations: tuple[str, ...]
    #: Read-only command that exits non-zero when the CLI is not signed in.
    auth_check: tuple[str, ...] | None
    login_hint: str
    #: Flags that skip permission prompts. Never passed; a guard checks.
    bypass_flags: frozenset[str]


CLIS: dict[str, CliSpec] = {
    "claude": CliSpec(
        "claude",
        "Claude Code",
        (".local/bin/claude",),
        ("auth", "status"),
        "claude auth login",
        frozenset({"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"}),
    ),
    "codex": CliSpec(
        "codex",
        "Codex",
        (".local/bin/codex",),
        ("login", "status"),
        "codex login",
        frozenset(
            {"--dangerously-bypass-approvals-and-sandbox", "--yolo", "--full-auto"}
        ),
    ),
    "cursor": CliSpec(
        "cursor",
        "Cursor CLI",
        (".local/bin/cursor-agent", ".local/bin/agent"),
        ("status",),
        "cursor-agent login",
        frozenset({"--force", "-f", "--yolo", "--approve-mcps"}),
    ),
    "agy": CliSpec(
        "agy",
        "Antigravity CLI",
        (".local/bin/agy",),
        None,
        "agy (sign in from the interactive session)",
        frozenset({"--dangerously-skip-permissions"}),
    ),
}

_BYPASS_VALUES = frozenset({"bypassPermissions", "danger-full-access", "never"})

_ACCESS_PATTERNS = (
    ("limit", re.compile(r"quota|rate.?limit|usage limit|RESOURCE_EXHAUSTED|\b429\b|upgrade (?:your|plans?)|plan can only", re.I)),
    ("auth", re.compile(r"not logged in|log ?in required|please log ?in|unauthori[sz]ed|\b401\b|authenticat\w* (?:failed|required)|sign in", re.I)),
)


@dataclass(frozen=True)
class ResolvedCli:
    spec: CliSpec
    executable: Path | None
    reason: str = ""

    @property
    def available(self) -> bool:
        return self.executable is not None


@dataclass(frozen=True)
class Checkout:
    role: Literal["installed", "development"]
    name: str
    path: Path
    branch: str = ""
    head: str = ""
    dirty: int | None = None
    origin: str = ""
    missing: bool = False


@dataclass(frozen=True)
class SourceCopy:
    id: str
    repo: str
    selection: str
    exclude: tuple[str, ...]
    path: Path | None
    error: str = ""


@dataclass
class RunOutcome:
    status: Literal["ok", "failed", "access", "timeout", "cancelled"]
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    access_kind: str = ""


@dataclass
class ReviewRun:
    run_id: str
    run_dir: Path
    scope: Scope
    cli: ResolvedCli
    argv: list[str]
    prompt: str
    checkouts: list[Checkout] = field(default_factory=list)
    sources: list[SourceCopy] = field(default_factory=list)


# --- Resolution ---------------------------------------------------------------


def resolve_cli(spec: CliSpec, home: Path) -> ResolvedCli:
    """The CLI at its Dotfiles location, or why it is not usable. Never PATH."""
    reasons: list[str] = []
    for location in spec.locations:
        candidate = home / location
        if not candidate.exists():
            continue
        real = candidate.resolve()
        if str(real).startswith("/mnt/"):
            reasons.append(f"{candidate} points into Windows ({real})")
            continue
        if not real.is_file() or not os.access(real, os.X_OK):
            reasons.append(f"{candidate} is not an executable file")
            continue
        return ResolvedCli(spec, real)
    if not reasons:
        reasons.append(f"not installed at ~/{' or ~/'.join(spec.locations)}")
    return ResolvedCli(spec, None, "; ".join(reasons))


def resolve_all(home: Path) -> dict[str, ResolvedCli]:
    return {key: resolve_cli(spec, home) for key, spec in CLIS.items()}


def state_file(paths: AgentbotPaths) -> Path:
    return paths.config_home / "setup-review.json"


def reviews_home(paths: AgentbotPaths) -> Path:
    return paths.config_home / "setup-reviews"


def last_cli(paths: AgentbotPaths) -> str | None:
    try:
        value = json.loads(state_file(paths).read_text(encoding="utf-8")).get("cli")
    except (OSError, ValueError, AttributeError):
        return None
    return value if value in CLIS else None


def remember_cli(paths: AgentbotPaths, key: str) -> None:
    paths.config_home.mkdir(parents=True, exist_ok=True)
    write_text_atomic(state_file(paths), json.dumps({"cli": key}, indent=2) + "\n")


def choose_cli(
    paths: AgentbotPaths,
    resolved: Mapping[str, ResolvedCli],
    requested: str | None,
    *,
    interactive: bool,
    ask: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
) -> ResolvedCli | None:
    """The explicit choice, else the remembered one, else a question.

    An unavailable CLI is reported, never swapped for another.
    """
    if requested:
        chosen = resolved.get(requested)
        if chosen is None:
            out(f"  [err] unknown CLI {requested!r}; choose one of {', '.join(CLIS)}")
            return None
        if not chosen.available:
            out(f"  [err] {chosen.spec.label} is not available: {chosen.reason}")
            _print_available(resolved, out)
            return None
        return chosen
    remembered = last_cli(paths)
    if remembered and resolved[remembered].available:
        out(f"  Using {resolved[remembered].spec.label} (last used; --cli to change).")
        return resolved[remembered]
    if remembered:
        out(f"  {resolved[remembered].spec.label} was used last but is not available: "
            f"{resolved[remembered].reason}")
    available = [item for item in resolved.values() if item.available]
    if not available:
        out("  [err] no supported agent CLI is installed")
        _print_available(resolved, out)
        return None
    if not interactive:
        out("  [err] choose a CLI with --cli; no terminal to ask on")
        _print_available(resolved, out)
        return None
    _print_available(resolved, out)
    keys = [item.spec.key for item in available]
    answer = ask(f"  Review with which CLI? [{'/'.join(keys)}] ").strip().lower()
    if answer not in keys:
        out("  No CLI chosen; nothing was run.")
        return None
    return resolved[answer]


def _print_available(resolved: Mapping[str, ResolvedCli], out: Callable[[str], None]) -> None:
    for item in resolved.values():
        state = f"available at {item.executable}" if item.available else item.reason
        out(f"    {item.spec.key:<7} {item.spec.label}: {state}")


# --- Evidence -----------------------------------------------------------------


def _git(path: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _strip_userinfo(url: str) -> str:
    return re.sub(r"^(\w+://)[^/@]+@", r"\1", url)


def describe_checkout(role: Literal["installed", "development"], name: str, path: Path) -> Checkout:
    if not (path / ".git").exists():
        return Checkout(role, name, path, missing=True)
    status = _git(path, "status", "--porcelain")
    return Checkout(
        role,
        name,
        path,
        branch=_git(path, "rev-parse", "--abbrev-ref", "HEAD"),
        head=_git(path, "rev-parse", "--short=12", "HEAD"),
        dirty=len(status.splitlines()) if status else 0,
        origin=_strip_userinfo(_git(path, "remote", "get-url", "origin")),
    )


_PRODUCTS = ("agentbot", "dotfiles", "dotfiles-shared")


def find_workspace(start: Path) -> Path | None:
    """The development workspace holding the product submodules, if `start` is in one."""
    top = _git(start, "rev-parse", "--show-toplevel")
    for candidate in [Path(top)] if top else []:
        while True:
            if all((candidate / name).is_dir() for name in ("agentbot", "dotfiles")):
                return candidate
            outer = _git(candidate.parent, "rev-parse", "--show-toplevel")
            if not outer or Path(outer) == candidate:
                return None
            candidate = Path(outer)
    return None


def gather_checkouts(home: Path, workspace: Path | None) -> list[Checkout]:
    dotfiles_home = Path(os.environ.get("DOTFILES_DIR") or home / "dotfiles")
    installed = {
        "agentbot": home / "agentbot",
        "dotfiles": dotfiles_home,
        "dotfiles-shared": home / "dotfiles-shared",
    }
    checkouts = [describe_checkout("installed", name, path) for name, path in installed.items()]
    if workspace is not None:
        for name in (*_PRODUCTS, "agentbot_skills"):
            path = workspace / name
            if path.resolve() in {p.resolve() for p in installed.values()}:
                continue
            checkouts.append(describe_checkout("development", name, path))
    return checkouts


def prepare_sources(paths: AgentbotPaths, destination: Path) -> list[SourceCopy]:
    """A read-only shallow copy of every enabled source, for the agent to read."""
    config = load_skills_sources(paths.skills_sources_file)
    copies: list[SourceCopy] = []
    for source in config.active_sources():
        repo = source.repo or ""
        selection = "all" if source.skills == ["*"] else ", ".join(source.skills)
        exclude = tuple(source.exclude)
        url = source_clone_url(repo)
        if url is None:
            copies.append(SourceCopy(source.id, repo, selection, exclude, None, "unsupported repo"))
            continue
        target = destination / source.id
        try:
            completed = subprocess.run(
                ["git", "clone", "--depth=1", "--quiet", url, str(target)],
                capture_output=True,
                text=True,
                timeout=CLONE_TIMEOUT_SECONDS,
                check=False,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            copies.append(SourceCopy(source.id, repo, selection, exclude, None, str(error)))
            continue
        if completed.returncode != 0:
            detail = completed.stderr.strip().splitlines()[-1:] or ["clone failed"]
            copies.append(SourceCopy(source.id, repo, selection, exclude, None, detail[0]))
            continue
        _make_read_only(target)
        copies.append(SourceCopy(source.id, repo, selection, exclude, target))
    return copies


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        path.chmod(mode & ~0o222)
    root.chmod(root.stat().st_mode & ~0o222)


# --- Prompt -------------------------------------------------------------------


def _template(paths: AgentbotPaths, name: str) -> str:
    return (paths.root / "review" / name).read_text(encoding="utf-8")


def _fill(template: str, values: Mapping[str, str]) -> str:
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    leftover = re.findall(r"\{\{(\w+)\}\}", template)
    if leftover:
        raise ValueError(f"setup-review prompt has unfilled fields: {', '.join(leftover)}")
    return template


def _format_checkouts(checkouts: Sequence[Checkout]) -> str:
    lines = []
    for item in checkouts:
        if item.missing:
            lines.append(f"- {item.role}: {item.name} at `{item.path}` (not a Git checkout)")
            continue
        dirty = "clean" if not item.dirty else f"{item.dirty} uncommitted path(s)"
        lines.append(
            f"- {item.role}: {item.name} at `{item.path}`, branch `{item.branch}`, "
            f"HEAD `{item.head}`, {dirty}, origin `{item.origin}`"
        )
    return "\n".join(lines) or "- (none found)"


def _format_sources(sources: Sequence[SourceCopy]) -> str:
    lines = []
    for item in sources:
        exclude = f"; exclude: {', '.join(item.exclude)}" if item.exclude else ""
        where = f"local copy `{item.path}`" if item.path else f"no local copy ({item.error})"
        lines.append(f"- `{item.id}`: `{item.repo}`, skills: {item.selection}{exclude}; {where}")
    return "\n".join(lines) or "- (no enabled sources)"


def cli_config_files(paths: AgentbotPaths) -> list[Path]:
    return sorted(path for path in (paths.root / "cli").iterdir() if path.is_file())


def build_prompt(
    paths: AgentbotPaths,
    scope: Scope,
    *,
    run_id: str,
    date: str,
    checkouts: Sequence[Checkout],
    sources: Sequence[SourceCopy],
    budget_minutes: int,
    budget_sources: int,
) -> str:
    common = {
        "prompt_version": PROMPT_VERSION,
        "run_id": run_id,
        "date": date,
        "budget_minutes": str(budget_minutes),
        "budget_sources": str(budget_sources),
    }
    if scope == "cli-config":
        files = "\n".join(f"- `{path}`" for path in cli_config_files(paths))
        return _fill(_template(paths, "cli-config-review.md"), {**common, "cli_files": files})
    return _fill(
        _template(paths, "setup-review.md"),
        {**common, "checkouts": _format_checkouts(checkouts), "sources": _format_sources(sources)},
    )


def build_update_prompt(
    paths: AgentbotPaths, *, run_id: str, report: Path, checkouts: Sequence[Checkout]
) -> str:
    development = [item for item in checkouts if item.role == "development"]
    listed = _format_checkouts(development) if development else (
        "- (no development checkout was found; propose fixes only, and tell the operator "
        "to rerun from the development workspace to make them)"
    )
    return _fill(
        _template(paths, "update.md"),
        {
            "prompt_version": PROMPT_VERSION,
            "run_id": run_id,
            "report_path": str(report),
            "checkouts": listed,
        },
    )


# --- Commands -----------------------------------------------------------------


def review_argv(
    cli: ResolvedCli, prompt: str, *, read_dirs: Sequence[Path], timeout_seconds: int
) -> list[str]:
    """The non-interactive, read-only command for the review phase.

    The prompt is always the last element, after an end-of-options marker
    where the CLI has one, so nothing in it is read as a flag.
    """
    exe = str(cli.executable)
    dirs = [str(path) for path in read_dirs]
    key = cli.spec.key
    if key == "claude":
        argv = [exe, "-p", "--permission-mode", "plan"]
        if dirs:
            argv += ["--add-dir", *dirs]
        argv += ["--allowedTools", "WebSearch", "WebFetch", "--", prompt]
    elif key == "codex":
        argv = [exe, "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--search",
                "--", prompt]
    elif key == "cursor":
        # --trust answers Cursor's workspace-trust question for the working
        # directory alone, which is this run's own private folder; it is not
        # the permission bypass that --force/--yolo are. Plan mode is read-only.
        argv = [exe, "-p", "--mode", "plan", "--trust", "--output-format", "text"]
        for directory in dirs:
            argv += ["--add-dir", directory]
        argv += ["--", prompt]
    elif key == "agy":
        argv = [exe, "--mode", "plan", "--print-timeout", f"{timeout_seconds}s"]
        for directory in dirs:
            argv += ["--add-dir", directory]
        argv += ["-p", prompt]
    else:  # pragma: no cover - CLIS is closed
        raise ValueError(key)
    assert_no_bypass(cli.spec, argv)
    return argv


def update_argv(cli: ResolvedCli, prompt: str, *, read_dirs: Sequence[Path]) -> list[str]:
    """An interactive session in the CLI's normal permission mode."""
    exe = str(cli.executable)
    dirs = [str(path) for path in read_dirs]
    key = cli.spec.key
    if key == "claude":
        argv = [exe]
        if dirs:
            argv += ["--add-dir", *dirs]
        argv += ["--", prompt]
    elif key == "codex":
        argv = [exe, "--", prompt]
    elif key == "cursor":
        argv = [exe]
        for directory in dirs:
            argv += ["--add-dir", directory]
        argv += ["--", prompt]
    elif key == "agy":
        argv = [exe]
        for directory in dirs:
            argv += ["--add-dir", directory]
        argv += ["--prompt-interactive", prompt]
    else:  # pragma: no cover - CLIS is closed
        raise ValueError(key)
    assert_no_bypass(cli.spec, argv)
    return argv


def assert_no_bypass(spec: CliSpec, argv: Sequence[str]) -> None:
    flags = argv[:-1]  # the prompt is data, whatever it says
    for arg in flags:
        name = arg.split("=", 1)[0]
        if name in spec.bypass_flags or arg in _BYPASS_VALUES:
            raise ValueError(f"refusing to run {spec.label} with {arg}")


def child_env(run_id: str) -> dict[str, str]:
    return {**os.environ, RECURSION_ENV: run_id}


# --- Running ------------------------------------------------------------------


def check_auth(cli: ResolvedCli) -> tuple[bool | None, str]:
    """(True, "") signed in; (False, hint) not; (None, why) when it cannot tell."""
    if cli.spec.auth_check is None:
        return None, "this CLI has no sign-in check; a failure will show when it runs"
    try:
        completed = subprocess.run(
            [str(cli.executable), *cli.spec.auth_check],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, f"sign-in check did not finish: {error}"
    if completed.returncode != 0:
        return False, f"not signed in; run `{cli.spec.login_hint}`"
    if cli.spec.key == "claude":
        try:
            if json.loads(completed.stdout).get("loggedIn") is False:
                return False, f"not signed in; run `{cli.spec.login_hint}`"
        except (ValueError, AttributeError):
            pass
    return True, ""


def classify_access(text: str) -> str:
    for kind, pattern in _ACCESS_PATTERNS:
        if pattern.search(text):
            return kind
    return ""


def run_review_process(
    argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout_seconds: int
) -> RunOutcome:
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        stdout, stderr = _stop(process)
        return RunOutcome("timeout", None, stdout, stderr)
    except KeyboardInterrupt:
        stdout, stderr = _stop(process)
        return RunOutcome("cancelled", None, stdout, stderr)
    if process.returncode != 0:
        kind = classify_access(f"{stdout}\n{stderr}")
        return RunOutcome("access" if kind else "failed", process.returncode, stdout, stderr, kind)
    return RunOutcome("ok", 0, stdout, stderr)


def _stop(process: subprocess.Popen[str]) -> tuple[str, str]:
    for sig, grace in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            break
        try:
            return process.communicate(timeout=grace)
        except subprocess.TimeoutExpired:
            continue
    try:
        return process.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        return "", ""


# --- Report -------------------------------------------------------------------

_SETTING_LINE = re.compile(
    r"""^(?P<lead>\s*(?:[-*|]\s*)?[`"']?)(?P<key>[A-Za-z_][\w.-]*)(?P<mid>[`"']?\s*[:=]\s*)(?P<value>\S.*)$"""
)


def redact_report(text: str) -> tuple[str, dict[str, int]]:
    """Mask secret-shaped values and refuse credential-shaped settings.

    Returns the cleaned text and a count per rule; never the matched values.
    """
    counts: dict[str, int] = {}
    for rule in SECRET_RULES:
        text, found = rule.pattern.subn(f"[redacted:{rule.id}]", text)
        if found:
            counts[rule.id] = counts.get(rule.id, 0) + found
    lines = []
    for line in text.splitlines():
        match = _SETTING_LINE.match(line)
        if match:
            leaf = match.group("key").rsplit(".", 1)[-1]
            if _SECRET_NAME.search(leaf) or leaf.lower() in _CREDENTIAL_KEYS:
                line = (
                    f"{match.group('lead')}{match.group('key')}{match.group('mid')}"
                    "[refused: credential-shaped setting; Agentbot never manages one]"
                )
                counts["REFUSED_CREDENTIAL_SETTING"] = counts.get("REFUSED_CREDENTIAL_SETTING", 0) + 1
        lines.append(line)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), counts


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _write_private(path: Path, text: str) -> None:
    write_text_atomic(path, text)
    path.chmod(0o600)


def new_run(paths: AgentbotPaths, cli: ResolvedCli, scope: Scope, now: datetime) -> tuple[str, Path]:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}-{scope}-{cli.spec.key}"
    home = reviews_home(paths)
    _private_dir(home)
    run_dir = home / run_id
    _private_dir(run_dir)
    return run_id, run_dir


def cli_version(cli: ResolvedCli) -> str:
    try:
        completed = subprocess.run(
            [str(cli.executable), "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (completed.stdout.strip().splitlines() or [""])[0][:120]


def write_report(
    run: ReviewRun,
    outcome: RunOutcome,
    *,
    started: datetime,
    finished: datetime,
    version: str,
    timeout_seconds: int,
) -> tuple[Path, dict[str, int]]:
    body, counts = redact_report(outcome.stdout)
    report = run.run_dir / "report.md"
    header = [
        f"<!-- agentbot setup-review {PROMPT_VERSION} run {run.run_id} -->",
        f"<!-- cli {run.cli.spec.key} {version}; status {outcome.status}; "
        f"{started.isoformat()} to {finished.isoformat()} -->",
        "",
    ]
    _write_private(report, "\n".join(header) + body)
    _write_private(run.run_dir / "prompt.md", run.prompt)
    stderr_tail, stderr_counts = redact_report("\n".join(outcome.stderr.splitlines()[-40:]))
    if stderr_tail.strip():
        _write_private(run.run_dir / "cli-stderr.txt", stderr_tail + "\n")
    provenance = {
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(run.prompt.encode("utf-8")).hexdigest(),
        "run_id": run.run_id,
        "scope": run.scope,
        "cli": run.cli.spec.key,
        "executable": str(run.cli.executable),
        "cli_version": version,
        # The prompt is in prompt.md; argv keeps a placeholder in its place.
        "argv": [*run.argv[:-1], "<prompt.md>"],
        "started": started.isoformat(),
        "finished": finished.isoformat(),
        "timeout_seconds": timeout_seconds,
        "status": outcome.status,
        "returncode": outcome.returncode,
        "access": outcome.access_kind,
        "redactions": counts,
        "stderr_redactions": stderr_counts,
        "checkouts": [
            {
                "role": item.role,
                "name": item.name,
                "path": str(item.path),
                "branch": item.branch,
                "head": item.head,
                "dirty": item.dirty,
                "origin": item.origin,
                "missing": item.missing,
            }
            for item in run.checkouts
        ],
        "sources": [
            {"id": s.id, "repo": s.repo, "skills": s.selection, "copied": s.path is not None,
             "error": s.error}
            for s in run.sources
        ],
    }
    _write_private(run.run_dir / "run.json", json.dumps(provenance, indent=2) + "\n")
    return report, counts


# --- Entry point --------------------------------------------------------------


@dataclass(frozen=True)
class ReviewOptions:
    cli: str | None = None
    scope: Scope = "setup"
    workspace: Path | None = None
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES
    source_budget: int = DEFAULT_SOURCE_BUDGET
    update: bool | None = None  # None: ask at a terminal; False: never


def run(
    paths: AgentbotPaths,
    options: ReviewOptions,
    *,
    home: Path | None = None,
    interactive: bool | None = None,
    ask: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    caller_dir: Path | None = None,
) -> int:
    home = home or Path.home()
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if os.environ.get(RECURSION_ENV):
        out(
            f"  [err] already inside setup-review run {os.environ[RECURSION_ENV]}; "
            "a review cannot start another review"
        )
        return EXIT_USAGE
    if options.timeout_minutes < 1 or options.source_budget < 1:
        out("  [err] --timeout and --source-budget must be at least 1")
        return EXIT_USAGE

    resolved = resolve_all(home)
    cli = choose_cli(paths, resolved, options.cli, interactive=interactive, ask=ask, out=out)
    if cli is None:
        return EXIT_USAGE
    remember_cli(paths, cli.spec.key)

    signed_in, detail = check_auth(cli)
    if signed_in is False:
        out(f"  [err] {cli.spec.label}: {detail}. Nothing was run.")
        return EXIT_ACCESS
    if signed_in is None:
        out(f"  [info] {cli.spec.label}: {detail}")

    started = now()
    run_id, run_dir = new_run(paths, cli, options.scope, started)
    timeout_seconds = options.timeout_minutes * 60
    checkouts: list[Checkout] = []
    sources: list[SourceCopy] = []
    read_dirs: list[Path] = []
    if options.scope == "setup":
        workspace = options.workspace or find_workspace(caller_dir or Path.cwd())
        checkouts = gather_checkouts(home, workspace)
        out("  Copying the enabled skill sources (read-only, shallow)...")
        sources = prepare_sources(paths, run_dir / "sources")
        read_dirs = [item.path for item in checkouts if not item.missing]
    else:
        read_dirs = [paths.root / "cli"]
    prompt = build_prompt(
        paths,
        options.scope,
        run_id=run_id,
        date=started.date().isoformat(),
        checkouts=checkouts,
        sources=sources,
        budget_minutes=max(1, options.timeout_minutes - 5),
        budget_sources=options.source_budget,
    )
    argv = review_argv(cli, prompt, read_dirs=read_dirs, timeout_seconds=timeout_seconds)
    review = ReviewRun(run_id, run_dir, options.scope, cli, argv, prompt, checkouts, sources)
    version = cli_version(cli)

    out(
        f"  Reviewing with {cli.spec.label} ({options.scope}, read-only, "
        f"up to {options.timeout_minutes} min). Ctrl-C cancels."
    )
    outcome = run_review_process(
        argv, cwd=run_dir, env=child_env(run_id), timeout_seconds=timeout_seconds
    )
    report, counts = write_report(
        review, outcome, started=started, finished=now(), version=version,
        timeout_seconds=timeout_seconds,
    )
    if counts:
        summary = ", ".join(f"{rule} x{count}" for rule, count in sorted(counts.items()))
        out(f"  [warn] masked in the report: {summary}")

    if outcome.status != "ok":
        return _report_failure(cli, outcome, report, options.timeout_minutes, out)
    if not outcome.stdout.strip():
        out(f"  [err] {cli.spec.label} finished but printed no report. Run folder: {run_dir}")
        return EXIT_FAILED
    out(f"  Report saved: {report}")

    if options.update is False or not interactive:
        if options.update and not interactive:
            out("  The update phase needs a terminal; it was not started.")
        return EXIT_OK
    answer = ask(
        f"  Start an interactive {cli.spec.label} session to plan fixes from this report? [y/N] "
    )
    if answer.strip().lower() not in {"y", "yes"}:
        out("  Update phase skipped. Nothing was changed.")
        return EXIT_OK
    update_prompt = build_update_prompt(paths, run_id=run_id, report=report, checkouts=checkouts)
    development = [item.path for item in checkouts if item.role == "development" and not item.missing]
    cwd = (options.workspace or (development[0].parent if development else run_dir))
    session = update_argv(cli, update_prompt, read_dirs=[run_dir, *read_dirs])
    try:
        completed = subprocess.run(session, cwd=cwd, env=child_env(run_id), check=False)
    except KeyboardInterrupt:
        out("  Update session interrupted.")
        return EXIT_CANCELLED
    return EXIT_OK if completed.returncode == 0 else EXIT_FAILED


def _report_failure(
    cli: ResolvedCli, outcome: RunOutcome, report: Path, minutes: int, out: Callable[[str], None]
) -> int:
    folder = report.parent
    if outcome.status == "timeout":
        out(f"  [err] {cli.spec.label} did not finish within {minutes} min and was stopped. "
            f"Partial output: {report}")
        return EXIT_TIMEOUT
    if outcome.status == "cancelled":
        out(f"  Cancelled; {cli.spec.label} was stopped. Partial output: {report}")
        return EXIT_CANCELLED
    if outcome.status == "access":
        what = "a usage or plan limit" if outcome.access_kind == "limit" else "sign-in"
        out(f"  [err] {cli.spec.label} stopped on {what} (exit {outcome.returncode}). "
            f"Details: {folder / 'cli-stderr.txt'}")
        if outcome.access_kind == "auth":
            out(f"        Sign in with `{cli.spec.login_hint}`, then rerun.")
        return EXIT_ACCESS
    out(f"  [err] {cli.spec.label} exited {outcome.returncode}. Details: {folder}")
    return EXIT_FAILED
