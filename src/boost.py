from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import stat
import tempfile
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

from .command_runner import CommandRunner
from .paths import AgentbotPaths


@dataclass(frozen=True)
class BoostStatus:
    state: str
    cli_path: Path | None
    cli_version: str | None
    config_path: Path
    upload_disabled: bool
    auto_update_disabled: bool
    claude_state: str
    codex_state: str
    cursor_state: str
    graph_state: str
    message: str
    shadowing_configs: tuple[Path, ...] = ()
    stale_artifacts: tuple[Path, ...] = ()
    user_flags: tuple[tuple[str, bool], ...] = ()
    diverged_flags: tuple[str, ...] = ()


DEFAULT_BOOST_TIMEOUT_SECONDS = 300
BOOST_TIMEOUT_ENV = "AGENTBOT_BOOST_TIMEOUT_SECONDS"
CONFIG_LOCK_TIMEOUT_SECONDS = 5.0
_CONFIG_LOCK_POLL_SECONDS = 0.05
# Boost stamps the release that wrote each hook and awareness file into a
# comment. Upstream owns the marker, so an unstamped file is treated as
# unknown rather than stale.
_ARTIFACT_VERSION_RE = re.compile(
    r"^#\s*boost-(?:hook|skill)-version:\s*(v?[0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]*)\s*$",
    re.MULTILINE,
)
_RELEASE_TAG_RE = re.compile(r"v?([0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]*)")
# Declared Boost feature-flag policy. Boost's report UI writes `user = <bool>`
# under `[feature_flags."name"]`, and a user value beats JFrog's remote default,
# so this is where the intended behaviour of both agents is pinned.
#
# `agentbot boost setup` writes every entry, which means a toggle made in
# Boost's UI is reverted on the next run. That is the point -- one command puts
# the machine in a known state -- but it does mean the UI is not the place to
# change these. Edit this map instead.
#
# Leaving a flag unpinned is not neutral: its effective value falls back to a
# remote default JFrog can change without warning.
BOOST_FEATURE_POLICY: dict[str, bool] = {
    # Scrubs secrets and abbreviates paths in what the agent sees, not just in
    # the local database. Enforces the global "never expose secrets in command
    # output" rule mechanically rather than by trusting the agent.
    "boost-agent-facing-redaction": True,
    # Boost's background self-updater. Dotfiles owns this binary: it installs a
    # digest-verified release asset and records the tag in
    # `~/.local/share/dotfiles/boost-cli.version`. A background update replaces
    # the binary without touching that stamp, so ownership state starts lying.
    # `[update] auto_update = false` below already opts out; the flag is pinned
    # too because a remote default of true is the thing that turns the updater
    # back on.
    "boost-auto-update": False,
    # Adds a Boost tokens-saved segment to the Claude Code status line.
    # Agentbot owns `global/claude/statusline-command.sh` and writes the
    # `statusLine` block wholesale, so two owners would overwrite each other --
    # the same collision that keeps BoostGraph off.
    "boost-claude-status-line": False,
    # The product: rewrite supported CLI commands and filter their output. On
    # by remote default, pinned because the whole integration is pointless
    # without it and a remote flip would disable it silently.
    "boost-cli-filtering": True,
    # Aimed at article and paper prose. This is a code workspace: negligible
    # gain, and it makes what the agent reads a lossy abbreviation of what the
    # tool printed.
    "boost-english-abbreviation": False,
    # Converts allowlisted document reads and shrinks large images in a local
    # temporary copy. Source files are unchanged, unsupported inputs fail open,
    # and ordinary code reads are outside this feature's scope.
    "boost-files-optimization": True,
    # BoostGraph. Excluded: it writes MCP config and BOOSTGRAPH marker blocks
    # into CLAUDE.md and AGENTS.md, which Agentbot rewrites wholesale, so the
    # two would overwrite each other silently. This pin is now the guard that
    # does the work: `--no-boostgraph` was passed on every call until boost
    # v0.13 removed the flag, and passing it broke every install outright.
    "boost-graph-integration": False,
    # Runs HTML through Readability and hands the agent article Markdown for
    # `curl`/`wget` and HTML file reads. Prose pages survive that; the HTML
    # this workspace actually reads is fixtures, coverage output and rendered
    # reports, where the markup is the thing being inspected and Readability
    # discards it. Same objection as english-abbreviation: the agent stops
    # seeing what the tool printed. Flip to True if doc-page fetching ever
    # outweighs that.
    "boost-html-article": False,
    # Re-encodes MCP JSON responses as TOON with values copied across untouched
    # -- a lossless reformat that costs nothing when no MCP tool is called.
    "boost-mcp-toon-format": True,
    # Posts and updates a GitHub PR comment with per-model token usage, driven
    # by an authenticated `gh`. An outward-facing write to someone else's
    # repository is never an incidental side effect of a local savings tool.
    "boost-pr-usage-comments": False,
    # Offers a Settings opt-in to send scrubbed CLI outputs to JFrog so they
    # can improve their filters. Redaction is best-effort and the outputs come
    # from private repositories; `[tracing] upload = false` already refuses the
    # trace upload, and this is the same decision for command text.
    "boost-share-cli-outputs": False,
    # Lets the remote flag payload name the version the auto-updater should
    # move to -- today it names v0.13.5, older than what Dotfiles installed.
    # Pointless with the updater off, and actively wrong if it is ever on.
    "boost-target-version": False,
}
GRAPH_FEATURE_FLAG = "boost-graph-integration"
# Scanned in the dry-run plan as defence in depth. It is NOT the BoostGraph
# guard and cannot be: boost omits BoostGraph from the plan even when
# `--boostgraph` is passed explicitly, so there is no text here to match. The
# guards that work are the `boost-graph-integration` pin in BOOST_FEATURE_POLICY
# and `_forbidden_graph_evidence`, which inspects what landed on disk. A third,
# the `--no-boostgraph` flag, was passed on every call until boost v0.13 removed
# it. Kept because a later version may start disclosing BoostGraph in the plan,
# and because it still catches a disclosed repository `.boost/` write.
_FORBIDDEN_PLAN_RE = re.compile(
    r"boost[ -]?graph|\bmcp\b|background (?:index|watch)|(?:^|[\s/])\.boost(?:/|$)",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class _BoostHost:
    flag: str
    label: str
    plan_token: str
    cli_names: tuple[str, ...]
    config: Path
    hook_dir: Path
    awareness: Path
    extra_graph: tuple[Path, ...]


class BoostIntegration:
    """Configure and inspect Boost's Claude/Codex/Cursor shell-output integration."""

    def __init__(self, paths: AgentbotPaths, *, runner: CommandRunner | None = None) -> None:
        self.paths = paths
        self._runner = runner or CommandRunner()

    @property
    def config_path(self) -> Path:
        return self.paths.codex_home.parent / ".boost" / "config.toml"

    @staticmethod
    def _timeout_seconds() -> int:
        raw = os.environ.get(BOOST_TIMEOUT_ENV, str(DEFAULT_BOOST_TIMEOUT_SECONDS))
        try:
            timeout = int(raw)
        except ValueError as error:
            raise ValueError(f"{BOOST_TIMEOUT_ENV} must be a positive integer") from error
        if timeout <= 0:
            raise ValueError(f"{BOOST_TIMEOUT_ENV} must be a positive integer")
        return timeout

    def status(self) -> BoostStatus:
        cli_path = self._find_cli()
        cli_version = self._cli_version(cli_path)
        upload_disabled, auto_update_disabled = self._config_flags()
        claude_state = self._claude_state()
        codex_state = self._codex_state()
        cursor_state = self._cursor_state()
        host_states = (
            ("Claude", claude_state),
            ("Codex", codex_state),
            ("Cursor", cursor_state),
        )
        graph_state = "forbidden" if self._forbidden_graph_evidence() else "absent"
        shadowing = self._shadowing_configs()
        stale = self._stale_artifacts(cli_version)
        user_flags = self._user_feature_flags()
        pinned = dict(user_flags)
        diverged = tuple(
            flag
            for flag, value in sorted(BOOST_FEATURE_POLICY.items())
            if pinned.get(flag) is not value
        )

        if cli_path is None:
            state = "not-installed"
            message = "Boost CLI is not installed."
        elif graph_state == "forbidden":
            state = "forbidden"
            message = "Forbidden BoostGraph or MCP configuration is present."
        elif not upload_disabled or not auto_update_disabled:
            state = "unsafe-config"
            message = "Boost privacy or update pinning is not safely configured."
        elif shadowing:
            state = "unsafe-config"
            message = (
                "Boost reads the first config it finds and does not merge, so these "
                "repository configs replace the safe global one and leave tracing "
                f"upload enabled inside them: {', '.join(str(path) for path in shadowing)}"
            )
        elif all(state == "skipped" for _, state in host_states) and not any(
            state == "orphaned" for _, state in host_states
        ):
            state = "cli-only"
            message = "Boost CLI is installed; no supported agent CLI is present."
        elif "unregistered" in (claude_state, codex_state, cursor_state):
            hosts = self._join_host_names(
                name for name, host_state in host_states if host_state == "unregistered"
            )
            state = "partial"
            message = (
                f"Boost hook files are installed for {hosts} but no hook is registered, "
                "so nothing is filtered. Rerun 'agentbot boost setup'."
            )
        elif any(
            host_state in {"missing", "partial", "orphaned"}
            for _, host_state in host_states
        ):
            hosts = self._join_host_names(
                name
                for name, host_state in host_states
                if host_state in {"missing", "partial", "orphaned"}
            )
            state = "partial"
            message = f"Boost integration is incomplete for {hosts}."
        elif stale:
            state = "stale"
            message = (
                f"Boost hook and awareness files predate the installed CLI "
                f"({cli_version}). Upgrading the binary does not rewrite them: "
                f"{', '.join(str(path) for path in stale)}"
            )
        else:
            state = "ready"
            ready_hosts = self._join_host_names(
                name for name, host_state in host_states if host_state == "ready"
            )
            message = (
                f"Boost shell-output integration is ready for {ready_hosts}."
                if ready_hosts
                else "Boost shell-output integration is ready."
            )

        return BoostStatus(
            state,
            cli_path,
            cli_version,
            self.config_path,
            upload_disabled,
            auto_update_disabled,
            claude_state,
            codex_state,
            cursor_state,
            graph_state,
            message,
            shadowing,
            stale,
            user_flags,
            diverged,
        )

    @contextmanager
    def _config_lock(self) -> Iterator[None]:
        """Hold Boost's own config lock across the read-modify-write.

        Boost rewrites config.toml on its own schedule to refresh remote feature
        flags, and guards it with this zero-byte lock file. Writing without the
        lock means whichever process calls os.replace last wins, silently
        dropping either our safety keys or Boost's flags.
        """
        lock_path = self.config_path.with_name(f"{self.config_path.name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + CONFIG_LOCK_TIMEOUT_SECONDS
        with open(lock_path, "a+", encoding="utf-8") as handle:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ValueError(
                            f"Could not acquire the Boost config lock at {lock_path}; "
                            "another Boost process is holding it. Retry once it exits."
                        ) from None
                    time.sleep(_CONFIG_LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def ensure_safe_config(self) -> None:
        with self._config_lock():
            self._ensure_safe_config_locked()

    def _ensure_safe_config_locked(self) -> None:
        path = self.config_path
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise ValueError(f"Boost config is not a regular file: {path}")
        if existing:
            try:
                tomllib.loads(existing)
            except tomllib.TOMLDecodeError as error:
                raise ValueError(f"Boost config is invalid TOML: {error}") from error

        updated = self._set_section_bool(existing, "tracing", "upload", False)
        updated = self._set_section_bool(updated, "update", "auto_update", False)
        for flag, value in sorted(BOOST_FEATURE_POLICY.items()):
            # The quoted subtable name is the section, so the existing writer
            # handles these without a second TOML path. Only `user` is written;
            # `remote` stays JFrog's to set.
            updated = self._set_section_bool(
                updated, f'feature_flags."{flag}"', "user", value
            )
        try:
            parsed = tomllib.loads(updated)
        except tomllib.TOMLDecodeError as error:
            # The input parsed a moment ago, so an unparseable result is our
            # rendering at fault. Say so rather than blaming the user's file.
            raise ValueError(
                f"Agentbot produced an invalid Boost config while pinning safety keys: {error}"
            ) from error
        if parsed.get("tracing", {}).get("upload") is not False:
            raise ValueError("Boost tracing.upload could not be disabled")
        if parsed.get("update", {}).get("auto_update") is not False:
            raise ValueError("Boost update.auto_update could not be disabled")
        written = parsed.get("feature_flags", {})
        for flag, value in BOOST_FEATURE_POLICY.items():
            if written.get(flag, {}).get("user") is not value:
                raise ValueError(f"Boost feature flag {flag} could not be set to {value}")
        if updated == existing:
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        original_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, prefix=".config.toml.", delete=False
            ) as handle:
                temporary_name = handle.name
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_name, original_mode)
            os.replace(temporary_name, path)
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)

    def setup(self) -> BoostStatus:
        current = self.status()
        if current.cli_path is None:
            return replace(
                current,
                message=(
                    "Boost CLI is not installed. Select Boost CLI in Dotfiles setup, "
                    "then rerun agentbot install."
                ),
            )
        if current.state == "forbidden":
            return current
        try:
            self.ensure_safe_config()
        except ValueError as error:
            # An unusable config must not abort the caller. Lifecycle.install()
            # calls this between skills and managed outputs, and an exception
            # here used to leave the rest of the install unrun.
            return replace(
                self.status(),
                state="broken",
                message=f"Boost config could not be made safe: {error}",
            )
        selected = [host for host in self._hosts() if self._cli_present(host.cli_names)]
        if not selected:
            return self.status()
        base = [
            str(current.cli_path),
            "init",
            # Boost asks for the Online Preview Agreement and Privacy Notice on
            # first run. Unanswered it sat for the full command timeout and
            # then failed the install, which on an unattended bootstrap is five
            # minutes of silence for a question nobody saw. Accepting it is a
            # deliberate, recorded decision -- see docs/skills.md -- not a
            # prompt Agentbot suppressed to save time.
            "--accept-terms",
            *(host.flag for host in selected),
        ]
        dry_run = self._runner.run(
            [*base[:2], "--dry-run", *base[2:]],
            timeout_seconds=self._timeout_seconds(),
        )
        if dry_run.returncode != 0:
            return replace(
                self.status(),
                state="broken",
                message=f"Boost dry run failed: {dry_run.detail()}",
            )
        plan = f"{dry_run.stdout}\n{dry_run.stderr}"
        if _FORBIDDEN_PLAN_RE.search(plan):
            return replace(
                self.status(),
                state="broken",
                message="Boost dry run contains forbidden BoostGraph, MCP, or indexing behavior.",
            )
        missing_targets = [
            host.label for host in selected if host.plan_token not in plan
        ]
        if missing_targets:
            return replace(
                self.status(),
                state="broken",
                message=(
                    "Boost dry run did not include the requested "
                    + self._join_host_names(missing_targets)
                    + " targets."
                ),
            )
        # Captured, not interactive. `--accept-terms` is passed precisely so
        # there is no prompt to keep a terminal for, and the dry run above
        # already proves the non-interactive path works. Two things follow:
        # boost's success banner stops landing at column zero in the middle of
        # an install's prefixed lines, and a failure can finally say why --
        # run_interactive returns the exit code and nothing else, so this
        # reported "Boost setup failed: exit code 1" with no reason attached.
        result = self._runner.run(
            base,
            timeout_seconds=self._timeout_seconds(),
        )
        if result.returncode != 0:
            return replace(
                self.status(),
                state="broken",
                message=f"Boost setup failed: {result.detail()}",
            )
        return self.status()

    def setup_if_cli_available(self) -> BoostStatus:
        current = self.status()
        return current if current.cli_path is None else self.setup()

    def off(self) -> BoostStatus:
        current = self.status()
        if current.cli_path is None:
            return current
        # Asymmetric with setup: `init` accepts several targets at once, but
        # `init --uninstall` rejects more than one ("specify only one target to
        # uninstall"). Remove them one at a time.
        #
        # There is deliberately no dry run here, unlike setup. In v0.12.6
        # `--dry-run` is honoured for install but NOT for uninstall: running
        # `init --dry-run --uninstall --claude` deletes the hooks, empties the
        # `hooks` object in settings.json, and prints the plan as though it had
        # changed nothing. A gate that performs the removal it claims to
        # preview is worse than no gate, and the real invocation returns the
        # same exit code the gate was reading. Recheck on each version bump.
        #
        # Boost v0.12.6 also resolves `--uninstall --claude` paths relative to
        # the working directory even though install is always global, so running
        # it from a repository deletes `<repo>/.claude/...` and leaves the real
        # `~/.claude` integration in place. `--codex` is unaffected. Pin cwd to
        # the home directory so the rollback hits what setup actually wrote.
        home = self.paths.codex_home.parent
        for host in self._hosts():
            if not self._host_has_artifacts(host):
                continue
            result = self._runner.run_interactive(
                [
                    str(current.cli_path),
                    "init",
                    "--uninstall",
                    host.flag,
                ],
                timeout_seconds=self._timeout_seconds(),
                cwd=home,
            )
            if result.returncode != 0:
                return replace(
                    self.status(),
                    state="broken",
                    message=(
                        f"Boost integration removal failed for {host.flag}: {result.detail()}"
                    ),
                )
        return self.status()

    def _find_cli(self) -> Path | None:
        command = shutil.which("boost")
        if command:
            return Path(command)
        fallback = self.paths.codex_home.parent / ".local" / "bin" / "boost"
        return fallback if fallback.is_file() and os.access(fallback, os.X_OK) else None

    def _cli_version(self, cli_path: Path | None) -> str | None:
        if cli_path is None:
            return None
        result = self._runner.run(
            [str(cli_path), "version"],
            timeout_seconds=self._timeout_seconds(),
        )
        if result.returncode != 0:
            return None
        output = (result.stdout or result.stderr).strip()
        return output.splitlines()[0] if output else None

    def _config_flags(self) -> tuple[bool, bool]:
        try:
            parsed = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return False, False
        return (
            parsed.get("tracing", {}).get("upload") is False,
            parsed.get("update", {}).get("auto_update") is False,
        )

    def _hosts(self) -> tuple[_BoostHost, ...]:
        claude = self.paths.claude_home
        codex = self.paths.codex_home
        cursor = self.paths.cursor_home
        return (
            _BoostHost(
                "--claude",
                "Claude",
                "Claude",
                ("claude",),
                claude / "settings.json",
                claude / "hooks",
                claude / "rules" / "boost-awareness.md",
                (claude.parent / ".claude.json",),
            ),
            _BoostHost(
                "--codex",
                "Codex",
                "Codex",
                ("codex",),
                codex / "hooks.json",
                codex / "hooks",
                codex / "BOOST.md",
                (codex / "config.toml",),
            ),
            _BoostHost(
                "--cursor",
                "Cursor",
                "Cursor",
                ("agent", "cursor"),
                cursor / "hooks.json",
                cursor / "hooks",
                cursor / "rules" / "boost-awareness.mdc",
                (cursor / "mcp.json",),
            ),
        )

    def _cli_present(self, names: tuple[str, ...]) -> bool:
        home_bin = self.paths.codex_home.parent / ".local" / "bin"
        for name in names:
            if shutil.which(name):
                return True
            fallback = home_bin / name
            if fallback.is_file() and os.access(fallback, os.X_OK):
                return True
        return False

    def _host_has_artifacts(self, host: _BoostHost) -> bool:
        if host.awareness.is_file():
            return True
        if host.hook_dir.is_dir() and any(host.hook_dir.glob("boost-*")):
            return True
        return bool(self._registered_hook_names(host.config))

    def _host_state(self, host: _BoostHost) -> str:
        integration = self._integration_state(
            config=host.config,
            hook_dir=host.hook_dir,
            awareness=host.awareness,
        )
        if self._cli_present(host.cli_names):
            return integration
        return "orphaned" if integration != "missing" else "skipped"

    def _claude_state(self) -> str:
        return self._host_state(self._hosts()[0])

    def _codex_state(self) -> str:
        return self._host_state(self._hosts()[1])

    def _cursor_state(self) -> str:
        return self._host_state(self._hosts()[2])

    @staticmethod
    def _join_host_names(names: Iterable[str]) -> str:
        labels = [name for name in names if name]
        if not labels:
            return ""
        if len(labels) == 1:
            return labels[0]
        if len(labels) == 2:
            return f"{labels[0]} and {labels[1]}"
        return f"{', '.join(labels[:-1])}, and {labels[-1]}"

    def _integration_state(self, *, config: Path, hook_dir: Path, awareness: Path) -> str:
        """Judge a host by what its config actually registers.

        Hook files existing is not the same as the host running them: Boost's
        rewrite filter only takes effect once a hook is registered. Going the
        other way, whatever is registered has to be on disk, or the filter is
        registered against nothing. Both hosts are checked the same way -- the
        Codex half used to pass on file existence alone, so an inert install
        reported "ready".

        Registered commands are compared by file name rather than by path.
        Boost writes absolute paths under the real home, which no test or
        relocated home would resolve, and the name is what identifies the
        script either way.
        """
        registered = self._registered_hook_names(config)
        if not registered:
            installed = any(hook_dir.glob("boost-*")) if hook_dir.is_dir() else False
            return "unregistered" if installed or awareness.is_file() else "missing"
        if not awareness.is_file():
            return "partial"
        if any(not (hook_dir / name).is_file() for name in registered):
            return "partial"
        return "ready"

    @staticmethod
    def _registered_hook_names(config: Path) -> frozenset[str]:
        """Names of the Boost hook scripts a host config registers.

        Both hosts nest hooks as event -> matchers -> hooks -> command, and the
        shape has changed before, so walk the subtree for command strings
        instead of hard-coding a traversal a schema change would break.
        """
        try:
            hooks = json.loads(config.read_text(encoding="utf-8")).get("hooks")
        except (OSError, json.JSONDecodeError, AttributeError):
            return frozenset()
        if not isinstance(hooks, dict) or not hooks:
            return frozenset()
        names: set[str] = set()

        def walk(node: object) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "command" and isinstance(value, str):
                        name = PurePosixPath(value.strip()).name
                        if name.startswith("boost-"):
                            names.add(name)
                    else:
                        walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(hooks)
        return frozenset(names)

    def _user_feature_flags(self) -> tuple[tuple[str, bool], ...]:
        """Feature flags this machine has pinned, whatever the remote default.

        Only flags carrying an explicit `user` value are returned. A remote-only
        entry is JFrog's default rather than local drift, and reporting those
        would bury the handful that someone actually chose.
        """
        try:
            parsed = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return ()
        flags = parsed.get("feature_flags")
        if not isinstance(flags, dict):
            return ()
        chosen = [
            (name, entry["user"])
            for name, entry in sorted(flags.items())
            if isinstance(entry, dict) and isinstance(entry.get("user"), bool)
        ]
        return tuple(chosen)

    def _stale_artifacts(self, cli_version: str | None) -> tuple[Path, ...]:
        """Hook and awareness files left behind by a Boost binary upgrade.

        Dotfiles owns the binary and Agentbot owns the integration, and neither
        triggers the other. `dotfiles full-update` closes the gap in passing --
        it runs `agentbot install` after the upgrade, and setup re-runs `boost
        init`, which rewrites these files -- but `dotfiles update` alone does
        not, and neither does a setup that silently skipped. Nothing else
        catches it: every file is present and registered, just stale.

        Files carrying no version marker are ignored. Upstream owns that
        comment and may drop it in any of its every-day-or-two releases, and a
        standing false `stale` row would train the check to be ignored.
        """
        expected = self._release_tag(cli_version)
        if expected is None:
            return ()
        stale: list[Path] = []
        for path in self._installed_artifacts():
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            found = {self._release_tag(value) for value in _ARTIFACT_VERSION_RE.findall(content)}
            if found and expected not in found:
                stale.append(path)
        return tuple(stale)

    def _installed_artifacts(self) -> tuple[Path, ...]:
        """Every hook script a host registers, plus its awareness file."""
        artifacts: list[Path] = []
        for host in self._hosts():
            artifacts.extend(
                host.hook_dir / name
                for name in sorted(self._registered_hook_names(host.config))
                if (host.hook_dir / name).is_file()
            )
            if host.awareness.is_file():
                artifacts.append(host.awareness)
        return tuple(artifacts)

    @staticmethod
    def _release_tag(value: str | None) -> str | None:
        """Normalize `boost v0.12.6` or `0.12.6` to a comparable `v0.12.6`."""
        if not value:
            return None
        match = _RELEASE_TAG_RE.search(value)
        return f"v{match.group(1)}" if match else None

    def _shadowing_configs(self) -> tuple[Path, ...]:
        """Repository configs that replace the safe global one.

        Boost resolves `.boost/config.toml` from the working directory, then
        the git root, then the home directory, and reads only the first match.
        A repository config therefore drops the global `tracing.upload = false`
        for every command run inside it. Only registered workspaces can be
        checked -- Agentbot cannot enumerate every directory an agent might run
        in -- and a repository config that disables upload itself is fine.
        """
        from .workspace_state import WorkspaceStore

        try:
            records = WorkspaceStore(self.paths.workspace_state_file).load()
        except (OSError, ValueError):
            return ()
        shadowing: list[Path] = []
        for record in records:
            candidate = Path(record.path) / ".boost" / "config.toml"
            if not candidate.is_file():
                continue
            try:
                parsed = tomllib.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                shadowing.append(candidate)
                continue
            if parsed.get("tracing", {}).get("upload") is not False:
                shadowing.append(candidate)
        return tuple(shadowing)

    def _forbidden_graph_evidence(self) -> bool:
        candidates: list[Path] = []
        for host in self._hosts():
            candidates.append(host.config)
            candidates.extend(host.extra_graph)
        for path in candidates:
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if re.search(r"boost[ -]?graph|boostgraph_explore", content, re.IGNORECASE):
                return True
        return any(self._repository_graph_indexes())

    def _repository_graph_indexes(self) -> tuple[Path, ...]:
        """Registered workspaces carrying a BoostGraph index.

        `boostgraph init` builds its index into a repository `.boost/`, which
        the plan forbids outright. A `.boost/` holding nothing but a config file
        is a different problem -- see `_shadowing_configs` -- so only other
        content counts as graph evidence.
        """
        from .workspace_state import WorkspaceStore

        try:
            records = WorkspaceStore(self.paths.workspace_state_file).load()
        except (OSError, ValueError):
            return ()
        config_names = {"config.toml", "config.toml.lock"}
        indexes: list[Path] = []
        for record in records:
            candidate = Path(record.path) / ".boost"
            if not candidate.is_dir():
                continue
            try:
                entries = {entry.name for entry in candidate.iterdir()}
            except OSError:
                continue
            if entries - config_names:
                indexes.append(candidate)
        return tuple(indexes)

    @staticmethod
    def _set_section_bool(content: str, section: str, key: str, value: bool) -> str:
        rendered = "true" if value else "false"
        lines = content.splitlines(keepends=True)
        section_start: int | None = None
        section_end = len(lines)
        header_re = re.compile(r"^\s*\[([^]]+)]\s*(?:#.*)?(?:\r?\n)?$")
        key_re = re.compile(rf"^(\s*){re.escape(key)}\s*=.*?(\r?\n)?$")
        for index, line in enumerate(lines):
            match = header_re.match(line)
            if not match:
                continue
            if section_start is not None:
                section_end = index
                break
            if match.group(1).strip() == section:
                section_start = index
        if section_start is None:
            prefix = content
            if prefix and not prefix.endswith("\n"):
                prefix += "\n"
            if prefix and not prefix.endswith("\n\n"):
                prefix += "\n"
            return f"{prefix}[{section}]\n{key} = {rendered}\n"
        for index in range(section_start + 1, section_end):
            match = key_re.match(lines[index])
            if match:
                newline = match.group(2) or ""
                lines[index] = f"{match.group(1)}{key} = {rendered}{newline}"
                return "".join(lines)
        # A final line with no newline would otherwise absorb the inserted key
        # and produce `something = 1upload = false`. The absent-section branch
        # above already normalizes this; the insert path has to as well.
        if section_end > 0 and not lines[section_end - 1].endswith("\n"):
            lines[section_end - 1] += "\n"
        lines.insert(section_end, f"{key} = {rendered}\n")
        return "".join(lines)
