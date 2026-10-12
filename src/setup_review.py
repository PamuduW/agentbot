"""Recurring setup review, run by the agent CLI the operator chooses.

Agentbot brings the installed checkouts up to date (fetch, status, a
fast-forward pull of a clean clone), runs the chosen CLI non-interactively in
its read-only mode from the Dotfiles checkout, and captures what it prints as
the report. The report has secrets and personal details replaced, is kept under
private Agentbot state, and is committed to Dotfiles' development branch as
reviews/review_<stamp>_<scope>.md with a line in reviews/review_log.md, from a
throwaway worktree so the live clone never changes. The agent itself writes,
commits and pushes nothing.

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

PROMPT_VERSION = "setup-review/2"
RECURSION_ENV = "AGENTBOT_SETUP_REVIEW"
DEFAULT_TIMEOUT_MINUTES = 45
DEFAULT_SOURCE_BUDGET = 40
CLONE_TIMEOUT_SECONDS = 120

Scope = Literal["full", "dotfiles", "agentbot", "shared", "skills", "cli-config"]
SCOPES: tuple[str, ...] = ("full", "dotfiles", "agentbot", "shared", "skills", "cli-config")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_ACCESS = 3
EXIT_PREFLIGHT = 4
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
    name: str
    path: Path
    state: str = "current"
    detail: str = ""
    branch: str = ""
    head: str = ""
    origin: str = ""


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


def _git_run(path: Path, *args: str, timeout: int = 60) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, "", str(error)
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def _git(path: Path, *args: str) -> str:
    rc, out, _ = _git_run(path, *args, timeout=20)
    return out if rc == 0 else ""


def _strip_userinfo(url: str) -> str:
    return re.sub(r"^(\w+://)[^/@]+@", r"\1", url)


def live_clones(home: Path) -> list[tuple[str, Path]]:
    """The installed checkouts the review reads and keeps current."""
    return [
        ("dotfiles", Path(os.environ.get("DOTFILES_DIR") or home / "dotfiles")),
        ("agentbot", home / "agentbot"),
        ("dotfiles-shared", home / "dotfiles-shared"),
    ]


def sync_clone(name: str, path: Path) -> Checkout:
    """Fetch, then fast-forward a clean clone that is only behind.

    Dirty, ahead or diverged stops the review, the rule `dotfiles fu` applies:
    those are somebody's work, not something to pull over.
    """
    if not (path / ".git").exists():
        return Checkout(name, path, state="missing", detail="not a Git checkout")
    rc, _, err = _git_run(path, "fetch", "--quiet", "origin")
    if rc != 0:
        return Checkout(name, path, state="fetch-failed", detail=(err.splitlines() or ["fetch failed"])[-1])
    if _git(path, "status", "--porcelain"):
        return Checkout(name, path, state="dirty", detail="uncommitted changes")
    counts = _git(path, "rev-list", "--left-right", "--count", "HEAD...@{u}").split()
    if len(counts) != 2:
        return Checkout(name, path, state="no-upstream", detail="branch has no upstream")
    ahead, behind = int(counts[0]), int(counts[1])
    state = "current"
    if ahead and behind:
        return Checkout(name, path, state="diverged", detail=f"{ahead} ahead, {behind} behind")
    if ahead:
        return Checkout(name, path, state="ahead", detail=f"{ahead} unpushed commit(s)")
    if behind:
        rc, _, err = _git_run(path, "pull", "--ff-only", "--quiet")
        if rc != 0:
            return Checkout(name, path, state="pull-failed", detail=(err.splitlines() or ["pull failed"])[-1])
        state = "pulled"
    return Checkout(
        name,
        path,
        state=state,
        detail=f"pulled {behind} commit(s)" if behind else "up to date",
        branch=_git(path, "rev-parse", "--abbrev-ref", "HEAD"),
        head=_git(path, "rev-parse", "--short=12", "HEAD"),
        origin=_strip_userinfo(_git(path, "remote", "get-url", "origin")),
    )


_STOPPING_STATES = frozenset({"fetch-failed", "dirty", "ahead", "diverged", "pull-failed", "no-upstream"})


def _agentbot(paths: AgentbotPaths) -> list[str]:
    """Agentbot's own launcher, which picks its own Python environment.

    `sys.executable` is whatever Python started this process, which need not
    have Agentbot's dependencies: run from another interpreter, `doctor`
    reported a missing module that the installed Agentbot does not lack.
    """
    launcher = paths.root / "bin" / "agentbot"
    if launcher.is_file():
        return [str(launcher)]
    return [sys.executable, "-m", "src.cli", "--root", str(paths.root)]


def vault_status(paths: AgentbotPaths) -> str:
    """One line from `agentbot memory status`; the vault itself is never touched with Git."""
    try:
        completed = subprocess.run(
            [*_agentbot(paths), "memory", "status"],
            cwd=paths.root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"status not available ({error})"
    rows = [
        " ".join(cell.strip() for cell in line.split("|") if cell.strip())
        for line in re.sub(r"\x1b\[[0-9;]*m", "", completed.stdout).splitlines()
        if "|" in line and not line.strip().startswith(("component", "-"))
    ]
    return "; ".join(rows[:4]) or f"`agentbot memory status` exited {completed.returncode}"


_ANSI_CODES = re.compile(r"\x1b\[[0-9;]*m")


def _table_rows(text: str, limit: int = 60) -> str:
    rows = [
        line.rstrip()
        for line in _ANSI_CODES.sub("", text).splitlines()
        if "|" in line and not set(line.strip()) <= set("-+| ")
    ]
    return "\n".join(rows[:limit])


def _command_rows(argv: Sequence[str], *, cwd: Path, timeout: int) -> str:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"(not available: {error})"
    return _table_rows(completed.stdout) or f"(no table; exit {completed.returncode})"


def collect_facts(paths: AgentbotPaths, dotfiles: Path) -> str:
    """Version and health tables, so the agent spends no budget re-deriving them."""
    sections = []
    command = dotfiles / "bin" / "bin" / "dotfiles"
    if command.is_file():
        sections.append(
            "`dotfiles update --dry-run` (installed, available, action; \"latest "
            "unchecked\" means Dotfiles did not look the latest release up, so you "
            "should):\n\n```text\n"
            + _command_rows([str(command), "update", "--dry-run"], cwd=dotfiles, timeout=300)
            + "\n```"
        )
    for name in ("status", "doctor"):
        sections.append(
            f"`agentbot {name}`:\n\n```text\n"
            + _command_rows([*_agentbot(paths), name], cwd=paths.root, timeout=180)
            + "\n```"
        )
    return "\n\n".join(sections)


_LOG_LINE = re.compile(r"^- (review_[\w.-]+\.md) - (created|reviewed)\s*$")
_PREVIOUS_LIMIT = 8000


def previous_reviews(dotfiles: Path) -> str:
    """The review log from Dotfiles' development branch, and the newest unread review."""
    log = _git(dotfiles, "show", f"origin/development:reviews/{REVIEW_LOG}")
    entries = [match.groups() for line in log.splitlines() if (match := _LOG_LINE.match(line))]
    if not entries:
        return "None filed yet."
    listed = "\n".join(f"- {name} ({state})" for name, state in entries)
    unread = [name for name, state in entries if state == "created"]
    if not unread:
        return listed
    newest = unread[-1]
    body = _git(dotfiles, "show", f"origin/development:reviews/{newest}")
    if not body:
        return listed
    if len(body) > _PREVIOUS_LIMIT:
        body = body[:_PREVIOUS_LIMIT] + "\n\n[cut: the rest of this earlier review]"
    return f"{listed}\n\nThe newest one not yet marked reviewed, `{newest}`:\n\n````markdown\n{body}\n````"


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

_SCOPE_TEXT = {
    "full": "the whole setup: Dotfiles, Agentbot, the shared library both load, "
    "and every enabled skill source.",
    "dotfiles": "Dotfiles only. Read Agentbot and the shared library only where "
    "they meet Dotfiles.",
    "agentbot": "Agentbot only. Read Dotfiles and the shared library only where "
    "they meet Agentbot.",
    "shared": "the shared library (dotfiles-shared) and how Dotfiles and Agentbot "
    "use it.",
    "skills": "the curated skill sources Agentbot installs, and nothing else.",
}
_SCOPE_CHECKLISTS = {
    "full": ("dotfiles", "agentbot", "shared", "across"),
    "dotfiles": ("dotfiles",),
    "agentbot": ("agentbot",),
    "shared": ("shared",),
    "skills": (),
}
_SCOPE_REPOS = {
    "full": "dotfiles, agentbot, dotfiles-shared",
    "dotfiles": "dotfiles",
    "agentbot": "agentbot",
    "shared": "dotfiles-shared",
    "skills": "agentbot (its skills.sources.yaml)",
}

_SKILL_SECTION = """
## Skill-source review

Agentbot installs skills from the sources in `skills.sources.yaml`. Treat
that file as the list under review, not as permission to install
everything a source ships. The enabled sources, with a read-only shallow
copy of each prepared by Agentbot:

{sources}

For every enabled source, open its local copy and list every skill it
currently contains (each directory with a `SKILL.md`). For each skill,
decide one of: keep, exclude, replace, or drop-source. Base the decision on
the skill's own `SKILL.md` and references, not on the repository README,
and record what it actually does; overlap with skills already selected from
other sources, including the first-party `agentbot_skills` source; whether
it is model-listed (its description costs context in every session) or
manual or path-scoped; and quality, maintenance, license, secret handling,
network or install side effects, and whether test fixtures or stubs would
be swept in.

A source declared `skills: all` installs everything it ships without a
per-skill decision. For each such source, propose an explicit keep list to
replace `all`, and say whether that named list is safer. Do this for the
first-party source too when a subset is enough. A new skill you find
upstream is a candidate only: name it, do not install it.
"""

_SKILL_REPORT = (
    "- `## Skill-source review`: one table per source (skill, decision, reason,\n"
    "  model-listed or not), then the proposed keep list for each `skills: all`\n"
    "  source.\n"
)


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
        if item.state == "missing":
            lines.append(f"- {item.name} at `{item.path}`: not installed here")
            continue
        lines.append(
            f"- {item.name} at `{item.path}`, branch `{item.branch}`, HEAD `{item.head}` "
            f"({item.detail}), origin `{item.origin}`"
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


def reviews_skills(scope: Scope) -> bool:
    return scope in {"full", "skills"}


def _checklist(paths: AgentbotPaths, scope: Scope) -> str:
    names = _SCOPE_CHECKLISTS.get(scope, ())
    if not names:
        return ""
    parts = [
        (paths.root / "review" / "checklists" / f"{name}.md").read_text(encoding="utf-8").strip()
        for name in names
    ]
    return "## Checklist\n\n" + "\n\n".join(parts) + "\n"


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
    vault: str = "",
    facts: str = "",
    previous: str = "",
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
    skills = reviews_skills(scope)
    return _fill(
        _template(paths, "setup-review.md"),
        {
            **common,
            "scope_text": _SCOPE_TEXT[scope],
            "repos": _SCOPE_REPOS[scope],
            "checkouts": _format_checkouts(checkouts),
            "vault": vault or "(not checked)",
            "facts": facts or "(none collected)",
            "previous": previous or "None filed yet.",
            "checklist": _checklist(paths, scope),
            "skill_section": _SKILL_SECTION.format(sources=_format_sources(sources))
            if skills
            else "",
            "skill_report": _SKILL_REPORT if skills else "",
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
        # --search is an option of `codex` itself; `codex exec` rejects it.
        argv = [exe, "--search", "exec", "--sandbox", "read-only", "--skip-git-repo-check",
                "--", prompt]
    elif key == "cursor":
        # --trust answers Cursor's workspace-trust question for the working
        # directory alone, the Dotfiles checkout the review reads; it is not
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


_EMAIL = re.compile(r"\b(?!git@)[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")


@dataclass(frozen=True)
class Identity:
    """What makes a report personal: replaced before it is filed anywhere."""

    home: Path
    user: str
    host: str

    @classmethod
    def current(cls, home: Path) -> Identity:
        import getpass
        import socket

        try:
            user = getpass.getuser()
        except (OSError, KeyError):
            user = home.name
        return cls(home=home, user=user, host=socket.gethostname())


def depersonalise(text: str, identity: Identity) -> tuple[str, dict[str, int]]:
    """Home folder, user name, host name and email addresses become placeholders."""
    counts: dict[str, int] = {}

    def swap(pattern: str, replacement: str, label: str, flags: int = 0) -> None:
        nonlocal text
        text, found = re.subn(pattern, replacement, text, flags=flags)
        if found:
            counts[label] = counts.get(label, 0) + found

    text, found = _EMAIL.subn("{email}", text)
    if found:
        counts["EMAIL"] = found
    swap(re.escape(str(identity.home)), "/home/{user}", "HOME_PATH")
    if len(identity.user) >= 3:
        swap(rf"(?<![\w.-]){re.escape(identity.user)}(?![\w-])", "{user}", "USER_NAME")
    if len(identity.host) >= 3:
        swap(rf"(?<![\w.-]){re.escape(identity.host)}(?![\w-])", "{host}", "HOST_NAME")
    return text, counts


# --- Filing -------------------------------------------------------------------

REVIEW_LOG = "review_log.md"
_LOG_HEADER = """# Setup reviews

One line per review `agentbot setup-review` filed. After reading a review,
delete its file and change its line from `created` to `reviewed`.

"""


def review_file_name(now: datetime, scope: Scope) -> str:
    return f"review_{now.strftime('%Y%m%d_%H%M%S')}_{scope}.md"


def publish_review(dotfiles: Path, name: str, text: str) -> tuple[str, str]:
    """Commit the review to Dotfiles' development branch from a throwaway worktree.

    The live clone is the running install: it never changes branch and never
    gets a stray file. Returns (state, detail); state is "published",
    "push-failed", "skipped" or "failed".
    """
    import shutil
    import tempfile

    if not (dotfiles / ".git").exists():
        return "skipped", f"no Dotfiles checkout at {dotfiles}"
    rc, _, err = _git_run(dotfiles, "fetch", "--quiet", "origin", "development")
    if rc != 0:
        return "skipped", "Dotfiles has no development branch to file it on"
    worktree = Path(tempfile.mkdtemp(prefix="agentbot-review-"))
    try:
        rc, _, err = _git_run(
            dotfiles, "worktree", "add", "--quiet", "--detach", str(worktree), "origin/development"
        )
        if rc != 0:
            return "failed", (err.splitlines() or ["worktree add failed"])[-1]
        reviews = worktree / "reviews"
        reviews.mkdir(exist_ok=True)
        write_text_atomic(reviews / name, text)
        log = reviews / REVIEW_LOG
        current = log.read_text(encoding="utf-8") if log.is_file() else _LOG_HEADER
        if not current.endswith("\n"):
            current += "\n"
        write_text_atomic(log, f"{current}- {name} - created\n")
        for args in (
            ("add", "--", f"reviews/{name}", f"reviews/{REVIEW_LOG}"),
            ("commit", "--quiet", "-m", f"docs(reviews): add {name}", "--", "reviews"),
        ):
            rc, _, err = _git_run(worktree, *args)
            if rc != 0:
                return "failed", (err.splitlines() or [f"git {args[0]} failed"])[-1]
        rc, _, err = _git_run(worktree, "push", "--quiet", "origin", "HEAD:refs/heads/development")
        if rc != 0:
            return "push-failed", (err.splitlines() or ["push failed"])[-1]
        return "published", _git(worktree, "rev-parse", "--short=12", "HEAD")
    finally:
        _git_run(dotfiles, "worktree", "remove", "--force", str(worktree))
        shutil.rmtree(worktree, ignore_errors=True)
        _git_run(dotfiles, "worktree", "prune")


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
    identity: Identity,
) -> tuple[Path, dict[str, int], str]:
    body, counts = redact_report(outcome.stdout)
    body, personal = depersonalise(body, identity)
    counts = {**counts, **personal}
    report = run.run_dir / "report.md"
    header = [
        f"<!-- agentbot setup-review {PROMPT_VERSION} run {run.run_id} -->",
        f"<!-- cli {run.cli.spec.key} {version}; status {outcome.status}; "
        f"{started.isoformat()} to {finished.isoformat()} -->",
        "",
    ]
    text = "\n".join(header) + body
    _write_private(report, text)
    _write_private(run.run_dir / "prompt.md", run.prompt)
    stderr_tail, stderr_counts = redact_report("\n".join(outcome.stderr.splitlines()[-40:]))
    stderr_tail, _ = depersonalise(stderr_tail, identity)
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
                "name": item.name,
                "path": str(item.path),
                "state": item.state,
                "detail": item.detail,
                "branch": item.branch,
                "head": item.head,
                "origin": item.origin,
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
    return report, counts, text


# --- Entry point --------------------------------------------------------------


@dataclass(frozen=True)
class ReviewOptions:
    cli: str | None = None
    scope: Scope = "full"
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES
    source_budget: int = DEFAULT_SOURCE_BUDGET
    #: Already confirmed (the menu asks first, or --yes).
    confirmed: bool = False


def run(
    paths: AgentbotPaths,
    options: ReviewOptions,
    *,
    home: Path | None = None,
    interactive: bool | None = None,
    ask: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    identity: Identity | None = None,
) -> int:
    home = home or Path.home()
    identity = identity or Identity.current(home)
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
    if not options.confirmed:
        if not interactive:
            out("  [err] pass --yes to run the review without a terminal to confirm on")
            return EXIT_USAGE
        answer = ask(f"  Run a {options.scope} review with {cli.spec.label}? [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            out("  Nothing was run.")
            return EXIT_OK
    remember_cli(paths, cli.spec.key)

    signed_in, detail = check_auth(cli)
    if signed_in is False:
        out(f"  [err] {cli.spec.label}: {detail}. Nothing was run.")
        return EXIT_ACCESS
    if signed_in is None:
        out(f"  [info] {cli.spec.label}: {detail}")

    out("  Bringing the installed checkouts up to date (fetch, status, pull --ff-only)...")
    checkouts = [sync_clone(name, path) for name, path in live_clones(home)]
    for item in checkouts:
        out(f"    {item.name:<16} {item.state}: {item.detail}")
    stopped = [item for item in checkouts if item.state in _STOPPING_STATES]
    if stopped:
        out("  [err] review stopped: settle the checkouts above first (nothing was changed).")
        return EXIT_PREFLIGHT
    dotfiles = next(item.path for item in checkouts if item.name == "dotfiles")

    started = now()
    run_id, run_dir = new_run(paths, cli, options.scope, started)
    timeout_seconds = options.timeout_minutes * 60
    sources: list[SourceCopy] = []
    if options.scope != "cli-config":
        out("  Collecting versions, health and earlier reviews...")
    if reviews_skills(options.scope):
        out("  Copying the enabled skill sources (read-only, shallow)...")
        sources = prepare_sources(paths, run_dir / "sources")
    if options.scope == "cli-config":
        read_dirs = [paths.root / "cli"]
    else:
        read_dirs = [item.path for item in checkouts if item.state != "missing"]
        if sources:
            read_dirs.append(run_dir / "sources")
    prompt = build_prompt(
        paths,
        options.scope,
        run_id=run_id,
        date=started.date().isoformat(),
        checkouts=checkouts,
        sources=sources,
        budget_minutes=max(1, options.timeout_minutes - 5),
        budget_sources=options.source_budget,
        vault="" if options.scope == "cli-config" else vault_status(paths),
        facts="" if options.scope == "cli-config" else collect_facts(paths, dotfiles),
        previous="" if options.scope == "cli-config" else previous_reviews(dotfiles),
    )
    argv = review_argv(cli, prompt, read_dirs=read_dirs, timeout_seconds=timeout_seconds)
    review = ReviewRun(run_id, run_dir, options.scope, cli, argv, prompt, checkouts, sources)
    version = cli_version(cli)
    cwd = dotfiles if (dotfiles / ".git").exists() else run_dir

    out(
        f"  Reviewing with {cli.spec.label} ({options.scope}, read-only, "
        f"up to {options.timeout_minutes} min). Ctrl-C cancels."
    )
    outcome = run_review_process(argv, cwd=cwd, env=child_env(run_id), timeout_seconds=timeout_seconds)
    report, counts, text = write_report(
        review, outcome, started=started, finished=now(), version=version,
        timeout_seconds=timeout_seconds, identity=identity,
    )
    if counts:
        summary = ", ".join(f"{rule} x{count}" for rule, count in sorted(counts.items()))
        out(f"  [info] replaced in the report: {summary}")

    if outcome.status != "ok":
        return _report_failure(cli, outcome, report, options.timeout_minutes, out)
    if not outcome.stdout.strip():
        out(f"  [err] {cli.spec.label} finished but printed no report. Run folder: {run_dir}")
        return EXIT_FAILED

    name = review_file_name(datetime.now(), options.scope)
    state, detail = publish_review(dotfiles, name, text)
    if state == "published":
        out(f"  Filed reviews/{name} on Dotfiles' development branch ({detail}).")
    else:
        out(f"  [warn] not filed in Dotfiles ({state}: {detail}).")
    out(f"  Local copy: {report}")
    return EXIT_OK


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
