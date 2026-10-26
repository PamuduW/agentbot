"""Enumerate setup-owned component identities without inspecting a live machine."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

_CONFIGURATION = frozenset({"git_identity", "git_credential", "wsl_conf", "dotfiles"})
_GROUPS = frozenset({"system_packages", "python", "docker"})
_VENDOR_APT = frozenset({"powershell"})


def _managed_rows(managed: str, name: str) -> list[list[str]]:
    match = re.search(r"^" + re.escape(name) + r"=\((.*?)\)", managed, re.MULTILINE | re.DOTALL)
    if match is None:
        raise ValueError(f"missing managed tool table: {name}")
    return [row.split("|") for row in re.findall(r"'([^'\n]+)'", match[1])]


def build_inventory(dotfiles: Path, agentbot: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}

    def add(key: str, owner: str, kind: str, source: str) -> None:
        value = {"owner": owner, "kind": kind, "source": source}
        if key in result and result[key] != value:
            raise ValueError(f"duplicate software identity: {key}")
        result[key] = value

    registry = (dotfiles / "scripts/lib/components/registry.sh").read_text()
    components = re.findall(r"^comp_define ([a-z][a-z0-9_]*)\s", registry, re.MULTILINE)
    if not components or len(components) != len(set(components)):
        raise ValueError("component registry is empty or has duplicate identities")
    for key in components:
        kind = (
            "external"
            if key == "cursor_cli"
            else "configuration"
            if key in _CONFIGURATION
            else "group"
            if key in _GROUPS
            else "vendor-apt"
            if key in _VENDOR_APT
            else "software"
        )
        add(key, "dotfiles", kind, "component-registry")
    managed = (dotfiles / "scripts/lib/managed_tool_state.sh").read_text()
    docker = re.search(r"^DOCKER_VENDOR_PACKAGES=\((.*?)\)", managed, re.MULTILINE | re.DOTALL)
    if docker is None or not docker[1].split():
        raise ValueError("Docker vendor package inventory is empty")
    for row in _managed_rows(managed, "RELEASE_CLI_TOOLS"):
        key, _label, repository, *_ = row
        result[key]["source"] = repository
    for row in _managed_rows(managed, "NPM_CLI_TOOLS"):
        key, _label, package, _binary = row
        result[key]["source"] = package
    for package in docker[1].split():
        if not re.fullmatch(r"[a-z0-9][a-z0-9.+-]*", package):
            raise ValueError("invalid Docker vendor package identity")
        add("docker." + package, "dotfiles", "vendor-apt", package)
    for line in (dotfiles / "packages/packages.txt").read_text().splitlines():
        package = line.split("#", 1)[0].strip()
        if package:
            add("os." + package.split("|", 1)[0], "dotfiles", "distro", package)
    for key, (kind, source) in {
        "nvm": ("git-source", "nvm-sh/nvm"),
        "asdf": ("software", "asdf-vm/asdf"),
        "uv": ("software", "astral-sh/uv"),
        "npm": ("software", "npm"),
        "asdf-golang": ("git-source", "asdf-community/asdf-golang"),
    }.items():
        add(key, "dotfiles", kind, source)
    add("skills_cli", "agentbot", "software", "skills")
    add("python.pip", "agentbot", "python-package", "pip")
    for filename in ("requirements.txt", "requirements-dev.txt"):
        for line in (agentbot / filename).read_text().splitlines():
            match = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)", line)
            if match:
                package = re.sub(r"[-_.]+", "-", match[1]).lower()
                add("python." + package, "agentbot", "python-package", package)
    skills = yaml.safe_load((agentbot / "skills.sources.yaml").read_text())
    for source in skills["sources"]:
        if source.get("enabled", True):
            add(
                "skill-source." + source["id"],
                "agentbot",
                "git-source",
                source.get("repo") or "first-party",
            )
    mcp = json.loads((agentbot / "mcp/catalog.json").read_text())
    for entry in mcp["entries"]:
        add(
            "mcp." + entry["id"],
            "agentbot",
            "service" if entry.get("origin") else "integration",
            entry["id"],
        )
    vscode = yaml.safe_load((agentbot / "vscode.yaml").read_text())
    for extensions in vscode["extensions"].values():
        for extension in extensions:
            add("extension." + extension.lower(), "agentbot", "extension", extension)
    return dict(sorted(result.items()))


def check_coverage(
    expected: dict[str, dict[str, Any]], declared: dict[str, dict[str, Any]]
) -> None:
    if not expected or not declared:
        raise ValueError("software inventory cannot be empty")
    missing = sorted(expected.keys() - declared.keys())
    extra = sorted(declared.keys() - expected.keys())
    if missing or extra:
        raise ValueError(f"catalog coverage mismatch: missing={missing}, unexplained={extra}")
    for key, value in expected.items():
        for field in ("owner", "kind", "source"):
            if declared[key].get(field) != value[field]:
                raise ValueError(f"catalog {field} mismatch for {key}")


def build_discovery(
    dotfiles: Path, inventory: dict[str, dict[str, Any]], agentbot: Path | None = None
) -> dict[str, dict[str, str]]:
    managed = (dotfiles / "scripts/lib/managed_tool_state.sh").read_text()
    result: dict[str, dict[str, str]] = {}
    owner = agentbot if agentbot is not None else Path(__file__).resolve().parents[1]
    extension_hosts = yaml.safe_load((owner / "vscode.yaml").read_text())["extensions"]
    for row in _managed_rows(managed, "RELEASE_CLI_TOOLS"):
        key, _label, repository, asset, archive, binary, version_arg = row
        result[key] = {
            "provider": "github-release",
            "locator": repository,
            "asset": asset,
            "archive": archive,
            "binary": binary,
            "version_arg": version_arg,
        }
    for row in _managed_rows(managed, "NPM_CLI_TOOLS"):
        key, _label, package, binary = row
        result[key] = {
            "provider": "npm",
            "locator": package,
            "archive": "npm-lock",
            "binary": binary,
        }
    result["skills_cli"] = {
        "provider": "npm",
        "locator": "skills",
        "archive": "npm-lock",
        "binary": "skills",
    }
    result["graphify_cli"] = {"provider": "pypi-tool", "locator": "graphifyy", "binary": "graphify"}
    result["claude_cli"] = {
        "provider": "claude-native",
        "locator": "stable",
        "auto_update_env": "DISABLE_UPDATES",
        "auto_update_value": "1",
    }
    result["antigravity_cli"] = {
        "provider": "antigravity-manifest",
        "locator": "linux_amd64",
        "auto_update_env": "AGY_CLI_DISABLE_AUTO_UPDATE",
        "auto_update_value": "true",
    }
    result["codex_cli"] = {
        "provider": "github-release",
        "locator": "openai/codex",
        "tag_prefix": "rust-v",
        "asset": "codex-package-{triple}-unknown-linux-musl.tar.gz",
        "archive": "tar.gz",
        "binary": "bin/codex",
    }
    result["chrome_headless"] = {"provider": "chrome-for-testing", "locator": "Stable"}
    result["nodejs"] = {"provider": "node-lts", "locator": "nodejs.org"}
    result["go"] = {"provider": "go-stable", "locator": "go.dev"}
    result["npm"] = {
        "provider": "npm",
        "locator": "npm",
        "archive": "tar",
        "binary": "package/bin/npm-cli.js",
    }
    result["portainer"] = {"provider": "portainer-lts", "locator": "portainer/portainer-ce"}
    for key, entry in inventory.items():
        if key.startswith("docker.") and entry["kind"] == "vendor-apt":
            result[key] = {"provider": "docker-apt", "locator": entry["source"], "channel": "noble"}
        if entry["kind"] == "python-package":
            result[key] = {"provider": "pypi", "locator": entry["source"]}
        elif entry["kind"] == "git-source":
            source = entry["source"]
            gitlab = source.startswith("gitlab.com/")
            locator = source.removeprefix("gitlab.com/" if gitlab else "github.com/")
            result[key] = {
                "provider": "gitlab-source" if gitlab else "github-source",
                "locator": locator,
            }
        elif entry["kind"] == "extension":
            hosts = sorted(
                host
                for host in ("windows", "wsl")
                if entry["source"].lower() in {value.lower() for value in extension_hosts[host]}
            )
            if not hosts:
                raise ValueError(f"extension has no declared host: {key}")
            result[key] = {
                "provider": "vscode-marketplace",
                "locator": entry["source"],
                "hosts": ",".join(hosts),
            }
    archives = {
        "asdf": ("asdf-vm/asdf", "asdf-v{version}-linux-{arch}.tar.gz", "tar.gz", "asdf"),
        "uv": (
            "astral-sh/uv",
            "uv-{triple}-unknown-linux-gnu.tar.gz",
            "tar.gz",
            "uv-{triple}-unknown-linux-gnu/uv",
        ),
        "boost_cli": ("jfrog/boost", "boost-linux-{arch}.tar.gz", "tar.gz", "boost"),
        "direnv": ("direnv/direnv", "direnv.linux-{arch}", "raw", "direnv"),
        "lazydocker": (
            "jesseduffield/lazydocker",
            "lazydocker_{version}_Linux_{release_arch}.tar.gz",
            "tar.gz",
            "lazydocker",
        ),
        "obsidian": (
            "obsidianmd/obsidian-releases",
            "obsidian_{version}_{arch}.deb",
            "deb",
            "obsidian",
        ),
        "powershell": (
            "PowerShell/PowerShell",
            "powershell_{version}-1.deb_{arch}.deb",
            "deb",
            "pwsh",
        ),
        "ubuntu_sans_mono_fonts": (
            "canonical/Ubuntu-Sans-Mono-fonts",
            "Ubuntu-Sans-Mono-fonts-v{version}.zip",
            "zip",
            "fonts",
        ),
    }
    for key, (repository, asset, archive, binary) in archives.items():
        if key in inventory:
            result[key] = {
                "provider": "github-release",
                "locator": repository,
                "asset": asset,
                "archive": archive,
                "binary": binary,
            }
    result["nvm"] = {"provider": "github-release", "locator": "nvm-sh/nvm"}
    return result
