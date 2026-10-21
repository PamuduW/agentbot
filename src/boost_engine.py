"""Manual Boost lessons from one selected summary, with no live-history ingestion."""

from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .atomic_io import write_text_atomic
from .boost import BOOST_FEATURE_POLICY, BoostIntegration, tomllib
from .boost_engine_worker import DISABLED_FEATURES, settings
from .memory import scan_secrets
from .paths import AgentbotPaths

MAX_SUMMARY_BYTES = 8192


@dataclass(frozen=True)
class EngineAnalysis:
    confirmed: bool
    summary_bytes: int
    proposal: str = ""
    usage: tuple[tuple[str, int], ...] = ()


def _checked_text(data: bytes, *, maximum: int) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise ValueError("Engine input/output must be UTF-8 text") from None
    if not text.strip() or len(data) > maximum or "\x00" in text:
        raise ValueError("Engine input/output is empty, binary or exceeds its size limit")
    if scan_secrets("selected-summary", text):
        raise ValueError("Engine input/output has credential findings; redact it before use")
    return text


def _summary(path: Path) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("Engine summary must be a regular file")
            data = handle.read(MAX_SUMMARY_BYTES + 1)
    except OSError:
        raise ValueError("Engine summary must be a readable regular file, not a symlink") from None
    return _checked_text(data, maximum=MAX_SUMMARY_BYTES)


def _terminate(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


@contextmanager
def _termination_cleanup() -> Iterator[None]:
    previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _run(
    argv: list[str], home: Path, env: dict[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    previous = signal.signal(signal.SIGTERM, _terminate)
    child: subprocess.Popen[str] | None = None
    try:
        child = subprocess.Popen(
            argv,
            cwd=home,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        out, err = child.communicate(timeout=timeout)
        return subprocess.CompletedProcess(argv, child.returncode, out, err)
    except subprocess.TimeoutExpired:
        raise ValueError("Boost Engine timed out; no automatic retry was attempted") from None
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            if child is not None:
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(child.pid, sig)
                    except ProcessLookupError:
                        pass
                child.wait()
                if child.stdout is not None:
                    child.stdout.close()
                if child.stderr is not None:
                    child.stderr.close()
        finally:
            signal.signal(signal.SIGTERM, previous)


class BoostEngine:
    def __init__(
        self,
        paths: AgentbotPaths,
        *,
        boost: Path | None = None,
        codex: Path | None = None,
        timeout: float = 90,
    ) -> None:
        self.paths = paths
        self.boost = boost
        self.codex = codex
        self.timeout = min(timeout, 90)

    def analyze(self, summary: Path, *, confirmed: bool = False) -> EngineAnalysis:
        text = _summary(summary)
        if not confirmed:
            return EngineAnalysis(False, len(text.encode("utf-8")))
        integration = BoostIntegration(self.paths)
        integration.ensure_private_storage()
        grant, terms = self._authorization(integration)
        boost = self.boost or integration._find_cli()
        native = self.codex or (Path(found) if (found := shutil.which("codex")) else None)
        if boost is None or native is None:
            raise ValueError("Boost and Codex must already be installed")
        auth = self.paths.codex_home / "auth.json"
        if not auth.is_file() or auth.is_symlink() or auth.stat().st_uid != os.getuid():
            raise ValueError(
                "The isolated Engine requires an existing native file-based ChatGPT login"
            )
        with (
            _termination_cleanup(),
            tempfile.TemporaryDirectory(prefix="agentbot-boost-engine-") as temporary,
        ):
            home = Path(temporary)
            env, state = self._profile(home, native.resolve(), auth, terms)
            self._preflight(native.resolve(), home, env)
            ready = _run([str(boost), "insights", "status"], home, env, 15)
            database = home / "data/boost/history.db"
            if ready.returncode or not database.is_file():
                raise ValueError("Boost could not initialize the isolated Engine profile")
            self._seed(database, grant, text)
            result = _run(
                [str(boost), "insights", "digest", "--days", "0"], home, env, self.timeout
            )
            if result.returncode or not state.is_file():
                raise ValueError("Boost Engine failed; no automatic retry was attempted")
            metadata = json.loads(state.read_text(encoding="utf-8"))
            if metadata["exit"] or metadata["unexpected_tool"]:
                raise ValueError("Boost Engine did not complete within the approved tool boundary")
            proposal = _checked_text(result.stdout.encode("utf-8"), maximum=65536)
            return EngineAnalysis(
                True, len(text.encode("utf-8")), proposal, tuple(sorted(metadata["usage"].items()))
            )

    @staticmethod
    def _authorization(integration: BoostIntegration) -> tuple[str, dict]:
        try:
            config = tomllib.loads(integration.config_path.read_text(encoding="utf-8"))
            if not config.get("accept_terms") or config.get("tracking", {}).get("database_path"):
                raise ValueError
            database = integration.data_home / "history.db"
            with closing(
                sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)
            ) as connection:
                row = connection.execute(
                    "SELECT value FROM tracker_state WHERE key='boost_pro_unlock'"
                ).fetchone()
            if row is None or not isinstance(row[0], str) or len(row[0]) > 8192:
                raise ValueError
            return row[0], {
                key: config[key]
                for key in ("accept_terms", "accept_terms_at", "terms_version")
                if key in config
            }
        except (OSError, ValueError, sqlite3.Error):
            raise ValueError(
                "Existing Boost terms acceptance and a local Pro grant are required; use Boost's normal interface first"
            ) from None

    @staticmethod
    def _profile(home: Path, codex: Path, auth: Path, terms: dict) -> tuple[dict[str, str], Path]:
        for name in (".boost", ".codex", "bin", "tmp"):
            (home / name).mkdir(mode=0o700)
        (home / ".codex/auth.json").symlink_to(auth)
        config = "".join(f"{key}={json.dumps(value)}\n" for key, value in terms.items())
        config += '[tracing]\ntelemetry=false\nupload=false\n[update]\nauto_update=false\n[llm]\nengine="codex"\n'
        config += "".join(
            f'[feature_flags."{name}"]\nuser={str(value).lower()}\n'
            for name, value in BOOST_FEATURE_POLICY.items()
        )
        write_text_atomic(home / ".boost/config.toml", config)
        (home / ".boost/config.toml").chmod(0o600)
        state = home / "native-result.json"
        worker = Path(__file__).with_name("boost_engine_worker.py")
        shim = home / "bin/codex"
        write_text_atomic(
            shim,
            f"#!{sys.executable}\nimport os,sys\nos.execv({str(sys.executable)!r}, [{str(sys.executable)!r}, {str(worker)!r}, {str(codex)!r}, {str(state)!r}, *sys.argv[1:]])\n",
            encoding="utf-8",
        )
        shim.chmod(0o700)
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PATH": f"{home}/bin:/usr/bin:/bin",
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_CACHE_HOME": str(home / "cache"),
            "BOOST_FEATURE_FLAGS_DISABLE": "1",
            "NO_COLOR": "1",
            "TMPDIR": str(home / "tmp"),
            "TMP": str(home / "tmp"),
            "TEMP": str(home / "tmp"),
        }
        return env, state

    @staticmethod
    def _preflight(codex: Path, home: Path, env: dict[str, str]) -> None:
        features = _run([str(codex), "features", "list", *settings()], home, env, 15)
        flags = {
            parts[0]: parts[-1]
            for line in features.stdout.splitlines()
            if len(parts := line.split()) >= 3
        }
        if features.returncode or any(flags.get(name) != "false" for name in DISABLED_FEATURES):
            raise ValueError("Codex cannot confirm the Engine tools-disabled profile")
        auth = _run([str(codex), "login", "status"], home, env, 15)
        if auth.returncode or "chatgpt" not in (auth.stdout + auth.stderr).lower():
            raise ValueError("The isolated Engine requires an existing ChatGPT login")

    @staticmethod
    def _seed(database: Path, grant: str, text: str) -> None:
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO tracker_state(key,value) VALUES('boost_pro_unlock',?)",
                    (grant,),
                )
                connection.execute(
                    "INSERT INTO conversations(conversation_id,title,first_seen,last_seen,tool,boost_mode,ended_at,status,transcript_path) VALUES('agentbot-selected','Manually selected summary',?,?,'codex','on',?,'completed','selected-summary')",
                    (stamp, stamp, stamp),
                )
                connection.execute(
                    "INSERT INTO agent_turns(conversation_id,turn_id,user_text,assistant_text) VALUES('agentbot-selected','1','Analyze only the selected summary',?)",
                    (text,),
                )
                connection.execute(
                    "INSERT INTO session_insight_summaries(conversation_id,status,summary,model,generated_at,source_last_seen) VALUES('agentbot-selected','done',?,'user-selected',?,?)",
                    (text, stamp, stamp),
                )
                connection.commit()
        except sqlite3.Error:
            raise ValueError(
                "The installed Boost schema is unsupported; no model was called"
            ) from None
