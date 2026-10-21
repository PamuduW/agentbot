"""Private Codex bridge for a single selected Boost analysis; not a public CLI."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

DISABLED_FEATURES = (
    "hooks",
    "shell_tool",
    "apps",
    "plugins",
    "remote_plugin",
    "multi_agent",
    "view_image",
    "image_generation",
    "sleep_tool",
    "tool_suggest",
)
MODEL = "gpt-6.1-sol"
MAX_PROMPT_BYTES = 16384


def settings() -> list[str]:
    values = [
        f'model="{MODEL}"',
        'model_reasoning_effort="low"',
        'approval_policy="never"',
        'sandbox_mode="read-only"',
        'web_search="disabled"',
        'cli_auth_credentials_store="file"',
    ]
    values.extend(f"features.{name}=false" for name in DISABLED_FEATURES)
    return [argument for value in values for argument in ("-c", value)]


def _vendor_request(argv: list[str]) -> tuple[str, Path]:
    if not argv or argv[0] != "exec":
        raise ValueError("Unsupported Engine request")
    from src.memory import scan_secrets

    prompt = argv[-1]
    if not prompt.strip() or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError("Engine prompt exceeds the admission limit")
    if scan_secrets("engine-prompt", prompt):
        raise ValueError("Engine prompt has credential findings")
    output: Path | None = None
    position = 1
    while position < len(argv) - 1:
        argument = argv[position]
        if argument in {"--json", "--skip-git-repo-check"}:
            position += 1
        elif argument == "-o" and position + 1 < len(argv) - 1:
            output = Path(argv[position + 1])
            position += 2
        elif argument == "-c" and position + 1 < len(argv) - 1:
            if argv[position + 1] not in {
                "features.hooks=false",
                "features.shell=false",
                "features.shell_tool=false",
                'sandbox_mode="read-only"',
            }:
                raise ValueError("Unreviewed Engine configuration override")
            position += 2
        else:
            raise ValueError("Unreviewed Engine argument")
    if output is None or not output.is_absolute() or output.exists():
        raise ValueError("Invalid Engine output destination")
    if any(path.is_symlink() for path in (output, *output.parents)):
        raise ValueError("Invalid Engine output destination")
    parent = output.parent.stat()
    if (
        not output.parent.name.startswith("boost-filtergen-")
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
    ):
        raise ValueError("Engine output directory is not private")
    return prompt, output


def run(codex: Path, state: Path, argv: list[str]) -> int:
    from src.atomic_io import write_text_atomic

    if argv in (["--version"], ["version"]):
        return subprocess.run([str(codex), "--version"], check=False, timeout=10).returncode
    if argv == ["exec", "--help"] or argv == ["--help"]:
        print("  Usage: codex exec --json")
        return 0
    prompt, destination = _vendor_request(argv)
    if not destination.is_relative_to(state.parent / "tmp"):
        raise ValueError("Engine output escaped the isolated profile")
    guard = state.with_suffix(".once")
    descriptor = os.open(guard, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    answer = state.with_suffix(".answer")
    command = [
        str(codex),
        "exec",
        "--ignore-user-config",
        "--ephemeral",
        "--json",
        "--skip-git-repo-check",
        "--color",
        "never",
        "-o",
        str(answer),
        *settings(),
        prompt,
    ]
    result = subprocess.run(
        command, stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=90
    )
    usage: dict[str, int] = {}
    unexpected_tool = False
    if len(result.stdout) > 1048576:
        return 78
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "turn.completed":
            for key, value in event.get("usage", {}).items():
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    usage[key] = usage.get(key, 0) + value
        if event.get("type") in {"item.started", "item.completed"}:
            unexpected_tool |= event.get("item", {}).get("type") not in {
                "agent_message",
                "reasoning",
            }
    write_text_atomic(
        state,
        json.dumps({"exit": result.returncode, "usage": usage, "unexpected_tool": unexpected_tool}),
        encoding="utf-8",
    )
    if result.returncode or unexpected_tool or not usage or not answer.is_file():
        return 78
    data = answer.read_bytes()
    if len(data) > 65536:
        return 78
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    sys.stdout.buffer.write(result.stdout)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        raise SystemExit(run(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:]))
    except (ValueError, OSError, subprocess.SubprocessError):
        # Vendor/client diagnostics can include input or auth data. The parent reports only state.
        raise SystemExit(78) from None
