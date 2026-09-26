"""Read-only Memory Core: locate the private vault and validate it.

The vault is a private Git checkout of plain Markdown records. Its marker,
``.meta/vault.json``, names the schema version, and the version selects the
validator: schema 1 is the live baseline, schema 2 adds stable IDs, explicit
scope, structured supersession and review/validity dates. A tree that mixes
the two fails either validator.

Nothing here writes to the vault. Reads refuse symlinks at every component,
are bounded, and require UTF-8. Findings carry relative paths, rule IDs and
line numbers only: never a note body, a field value, or a matched secret.

Resolution order, first valid checkout wins, the same shape as dotfiles-shared:

1. ``AGENTBOT_MEMORY_ROOT``  already resolved by an entrypoint
2. ``AGENTBOT_MEMORY_DIR``   an operator naming the checkout outright
3. ``<parent of this repo>/agent-memory``
4. ``~/agent-memory``

No checkout found is not an error: memory is simply unconfigured.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml

MARKER = ".meta/vault.json"
MARKER_KEY = "agentbot_memory_schema"
SUPPORTED_SCHEMAS = (1, 2)

MAX_FILE_BYTES = 262_144
MAX_FRONT_MATTER_BYTES = 16_384
MAX_MARKER_BYTES = 4_096
TITLE_MAX = 160
PROJECT_SLUG_MAX = 64
TAG_SLUG_MAX = 48
PROJECTS_MAX = 20
TAGS_MAX = 50
SUPERSEDES_MAX = 20

SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

README_EXEMPT = frozenset(
    {
        ".meta/README.md",
        "decisions/README.md",
        "lessons/README.md",
        "projects/README.md",
        "drafts/README.md",
        "exports/README.md",
        "templates/README.md",
    }
)
# Tracked, but not records: never validated or retrieved.
UNVALIDATED_PREFIXES = ("templates/", "exports/")
# Application state Git carries. Never read.
IGNORED_PREFIXES = (".obsidian/",)
NON_MARKDOWN_ALLOWED = frozenset({".gitignore", ".gitattributes", MARKER})

SINGLETON_TYPES = {"active-context.md": "context", "preferences.md": "preference"}
DIRECTORY_TYPES = {"decisions": "decision", "lessons": "lesson", "projects": "project"}
DRAFT_TYPES = frozenset({"decision", "lesson", "project"})
CANONICAL_STATUSES = frozenset({"accepted", "superseded", "retired"})
SCOPES = frozenset({"global", "shared", "project"})

V1_FIELDS = frozenset({"schema", "type", "title", "date", "status", "projects", "tags"})
V2_REQUIRED = V1_FIELDS | {"id", "scope"}
V2_OPTIONAL = frozenset({"supersedes", "review_after", "valid_until"})

Severity = Literal["error", "warning"]
# Record has a field named date, which shadows the type inside the class.
Day = date


class MemoryVaultError(RuntimeError):
    """The vault exists but cannot be inspected safely."""


@dataclass(frozen=True)
class Finding:
    severity: Severity
    rule: str
    path: str
    message: str
    line: int | None = None


@dataclass(frozen=True)
class Record:
    path: str
    type: str
    status: str
    draft: bool
    id: str | None = None
    supersedes: tuple[str, ...] = ()
    title: str = ""
    date: str = ""
    projects: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    scope: str | None = None
    review_after: str | None = None
    valid_until: str | None = None

    def due(self, today: Day) -> bool:
        """Reached its review date. A review candidate; its status is unchanged."""
        return self.review_after is not None and date.fromisoformat(self.review_after) <= today

    def expired(self, today: Day) -> bool:
        """Valid through its valid_until day; out of default retrieval after it."""
        return self.valid_until is not None and date.fromisoformat(self.valid_until) < today


@dataclass
class ValidationReport:
    root: Path
    schema: int
    records: list[Record] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    acknowledged: frozenset[str] = frozenset()

    @property
    def errors(self) -> list[Finding]:
        return [item for item in self.findings if item.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [
            item
            for item in self.findings
            if item.severity == "warning" and item.rule not in self.acknowledged
        ]

    @property
    def valid(self) -> bool:
        return not self.errors and not self.warnings


@dataclass(frozen=True)
class VaultStatus:
    state: Literal["unconfigured", "ready", "invalid", "broken"]
    looked_in: tuple[Path, ...]
    root: Path | None = None
    schema: int | None = None
    branch: str | None = None
    commit: str | None = None
    changed_paths: int | None = None
    ahead: int | None = None
    behind: int | None = None
    statuses: Mapping[str, int] = field(default_factory=dict)
    drafts: int = 0
    due: int = 0
    expired: int = 0
    errors: int = 0
    warnings: int = 0
    problem: str | None = None


# --- Resolution --------------------------------------------------------------


def candidates(
    repo_root: Path, *, home: Path | None = None, environ: Mapping[str, str] | None = None
) -> list[Path]:
    env = os.environ if environ is None else environ
    found = [
        Path(env[name]) for name in ("AGENTBOT_MEMORY_ROOT", "AGENTBOT_MEMORY_DIR") if env.get(name)
    ]
    found.append(repo_root.parent / "agent-memory")
    found.append((home or Path.home()) / "agent-memory")
    return list(dict.fromkeys(found))


def _is_vault(path: Path) -> bool:
    try:
        marker = os.lstat(path / MARKER)
    except OSError:
        return False
    return stat.S_ISREG(marker.st_mode) and (path / ".git").exists()


def find_vault(
    repo_root: Path, *, home: Path | None = None, environ: Mapping[str, str] | None = None
) -> tuple[Path | None, tuple[Path, ...]]:
    """The first valid vault checkout, and every place that was looked."""
    looked = tuple(candidates(repo_root, home=home, environ=environ))
    for candidate in looked:
        if _is_vault(candidate):
            return candidate, looked
    return None, looked


# --- Safe reads --------------------------------------------------------------


class _Unsafe(Exception):
    def __init__(self, rule: str, message: str) -> None:
        super().__init__(message)
        self.rule = rule
        self.message = message


def read_bounded(root: Path, relative: str, limit: int) -> bytes:
    """Read one regular file beneath root, following no symlink on the way."""
    parts = relative.split("/")
    if not relative or any(part in {"", ".", ".."} for part in parts) or "\0" in relative:
        raise _Unsafe("MEMORY_PATH", "unsafe relative path")
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        directory = os.open(root, flags | os.O_DIRECTORY)
    except OSError as error:
        raise _Unsafe("MEMORY_SYMLINK", "the vault root is a symlink or unreadable") from error
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            except FileNotFoundError:
                raise
            except OSError as error:
                raise _Unsafe("MEMORY_SYMLINK", "a parent directory is a symlink") from error
            os.close(directory)
            directory = child
        try:
            handle = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            raise
        except OSError as error:
            raise _Unsafe("MEMORY_SYMLINK", "file is a symlink or unreadable") from error
    finally:
        os.close(directory)
    with os.fdopen(handle, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise _Unsafe("MEMORY_SPECIAL_FILE", "not a regular file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise _Unsafe("MEMORY_SIZE", f"larger than {limit} bytes")
    return data


def _decode(data: bytes) -> str:
    if b"\0" in data:
        raise _Unsafe("MEMORY_ENCODING", "contains a NUL byte")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _Unsafe("MEMORY_ENCODING", "not valid UTF-8") from error


def read_marker(root: Path) -> int:
    """The schema version the vault marker declares."""
    try:
        text = _decode(read_bounded(root, MARKER, MAX_MARKER_BYTES))
    except _Unsafe as error:
        raise MemoryVaultError(f"{MARKER}: {error.message}") from error
    except FileNotFoundError as error:
        raise MemoryVaultError(f"{MARKER}: missing") from error

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)):
            raise MemoryVaultError(f"{MARKER}: duplicate key")
        return dict(pairs)

    try:
        payload = json.loads(text, object_pairs_hook=unique)
    except json.JSONDecodeError as error:
        raise MemoryVaultError(f"{MARKER}: not valid JSON") from error
    if not isinstance(payload, dict) or set(payload) != {MARKER_KEY}:
        raise MemoryVaultError(f'{MARKER}: must hold exactly {{"{MARKER_KEY}": N}}')
    version = payload[MARKER_KEY]
    if isinstance(version, bool) or version not in SUPPORTED_SCHEMAS:
        raise MemoryVaultError(f"{MARKER}: unsupported schema version")
    return int(version)


# --- Git ---------------------------------------------------------------------


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    # GIT_OPTIONAL_LOCKS=0 keeps `git status` from refreshing the index, so a
    # read-only command really writes nothing into the vault.
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            check=False,
            env=env,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise MemoryVaultError("git is unavailable or did not respond") from error


def _git_text(root: Path, *args: str) -> str | None:
    result = _git(root, *args)
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace").strip() or None


def _listed_paths(root: Path) -> list[str]:
    result = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if result.returncode != 0:
        raise MemoryVaultError("git could not list the vault's files")
    paths = {item for item in result.stdout.decode("utf-8", "surrogateescape").split("\0") if item}
    paths.update(_walk_drafts(root))
    return sorted(paths)


def _walk_drafts(root: Path) -> Iterable[str]:
    """Drafts are Git-ignored, so Git does not list them. Walk without following."""
    # A symlinked drafts/ is itself listed by Git and rejected; never walk it.
    pending = [] if os.path.islink(root / "drafts") else ["drafts"]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(root / current))
        except (FileNotFoundError, NotADirectoryError):
            continue
        for entry in entries:
            relative = f"{current}/{entry.name}"
            if entry.is_dir(follow_symlinks=False):
                pending.append(relative)
            elif entry.is_symlink() or entry.name.endswith(".md"):
                yield relative


# --- Front matter ------------------------------------------------------------


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that leaves dates as strings, so they are checked here."""


_StrictLoader.yaml_implicit_resolvers = {
    first: [(tag, regex) for tag, regex in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _front_matter(text: str) -> dict[str, Any]:
    lines = text.split("\n")
    if lines[0].rstrip("\r") != "---":
        raise _Unsafe("MEMORY_FRONT_MATTER", "missing front matter")
    end = next(
        (index for index, line in enumerate(lines[1:], 1) if line.rstrip("\r") == "---"), None
    )
    if end is None:
        raise _Unsafe("MEMORY_FRONT_MATTER", "front matter is not closed")
    block = "\n".join(lines[1:end])
    if len(block.encode("utf-8")) > MAX_FRONT_MATTER_BYTES:
        raise _Unsafe("MEMORY_FRONT_MATTER", f"front matter exceeds {MAX_FRONT_MATTER_BYTES} bytes")
    try:
        return _load_strict(block)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        line = f" near line {mark.line + 2}" if mark is not None else ""
        # Never str(error): PyYAML quotes the offending source text.
        raise _Unsafe("MEMORY_YAML", f"malformed YAML front matter{line}") from None


def _load_strict(block: str) -> dict[str, Any]:
    for event in yaml.parse(block, Loader=_StrictLoader):
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
            raise _Unsafe("MEMORY_YAML", "YAML anchors and aliases are not allowed")
        if getattr(event, "tag", None) is not None:
            raise _Unsafe("MEMORY_YAML", "explicit YAML tags are not allowed")
    node = yaml.compose(block, Loader=_StrictLoader)
    if not isinstance(node, yaml.MappingNode):
        raise _Unsafe("MEMORY_YAML", "front matter must be a mapping")
    if not all(isinstance(key, yaml.ScalarNode) for key, _ in node.value):
        raise _Unsafe("MEMORY_YAML", "every key must be a string")
    keys = [key.value for key, _ in node.value]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise _Unsafe("MEMORY_YAML", f"duplicate key: {', '.join(map(str, duplicates))}")
    data = yaml.load(block, Loader=_StrictLoader)
    if not all(isinstance(key, str) for key in data):
        raise _Unsafe("MEMORY_YAML", "every key must be a string")
    return data


# --- Record rules ------------------------------------------------------------


@dataclass(frozen=True)
class _Destination:
    type: str | None  # None for a draft, whose type is the proposal's own
    draft: bool
    project: str | None = None


def _destination(relative: str) -> _Destination | None:
    if relative in SINGLETON_TYPES:
        return _Destination(SINGLETON_TYPES[relative], draft=False)
    parts = relative.split("/")
    if len(parts) < 2:
        return None
    if parts[0] == "drafts":
        return _Destination(None, draft=True)
    kind = DIRECTORY_TYPES.get(parts[0])
    if kind is None:
        return None
    if kind == "project":
        return _Destination(kind, draft=False, project=parts[1]) if len(parts) >= 3 else None
    return _Destination(kind, draft=False)


def _as_date(value: Any) -> date | None:
    if not isinstance(value, str) or not ISO_DATE.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _slug_list(value: Any, *, limit: int, count: int) -> str | None:
    """Why a slug list is invalid, or None."""
    if not isinstance(value, list):
        return "must be a list"
    if len(value) > count:
        return f"has more than {count} entries"
    if not all(isinstance(item, str) and SLUG.match(item) and len(item) <= limit for item in value):
        return "holds an invalid slug"
    if len(set(value)) != len(value):
        return "holds duplicates"
    return None


class _RecordCheck:
    """The per-file rules for one schema version. Collects findings."""

    def __init__(self, schema: int, relative: str, destination: _Destination) -> None:
        self.schema = schema
        self.path = relative
        self.destination = destination
        self.findings: list[Finding] = []

    def fail(self, rule: str, message: str) -> None:
        self.findings.append(Finding("error", rule, self.path, message))

    def run(self, fields: dict[str, Any]) -> Record | None:
        required = V1_FIELDS if self.schema == 1 else V2_REQUIRED
        allowed = required | (V2_OPTIONAL if self.schema == 2 else frozenset())
        for key in sorted(set(fields) - allowed):
            self.fail("MEMORY_FIELD", f"unknown key: {key}")
        for key in sorted(required - set(fields)):
            self.fail("MEMORY_FIELD", f"missing key: {key}")
        if self.findings:
            return None
        kind = self._common(fields)
        if self.schema == 2 and kind is not None:
            self._v2(fields, kind)
        if self.findings or kind is None:
            return None
        return Record(
            path=self.path,
            type=kind,
            status=fields["status"],
            draft=self.destination.draft,
            id=fields.get("id"),
            supersedes=tuple(fields.get("supersedes") or ()),
            title=fields["title"],
            date=fields["date"],
            projects=tuple(fields["projects"]),
            tags=tuple(fields["tags"]),
            scope=fields.get("scope"),
            review_after=fields.get("review_after"),
            valid_until=fields.get("valid_until"),
        )

    def _common(self, fields: dict[str, Any]) -> str | None:
        schema = fields["schema"]
        if isinstance(schema, bool) or schema != self.schema:
            self.fail("MEMORY_SCHEMA", f"schema must be {self.schema}, as the vault marker says")
        kind = fields["type"]
        if self.destination.draft:
            if kind not in DRAFT_TYPES:
                self.fail("MEMORY_TYPE", "a draft must be a decision, lesson, or project")
                kind = None
        elif kind != self.destination.type:
            self.fail("MEMORY_TYPE", f"type must be {self.destination.type} at this path")
            kind = None
        title = fields["title"]
        if not isinstance(title, str) or title != title.strip() or not 1 <= len(title) <= TITLE_MAX:
            self.fail("MEMORY_TITLE", f"title must be a trimmed string of 1-{TITLE_MAX} characters")
        if _as_date(fields["date"]) is None:
            self.fail("MEMORY_DATE", "date must be a valid YYYY-MM-DD date")
        status = fields["status"]
        if self.destination.draft and status != "draft":
            self.fail("MEMORY_STATUS", "a draft must have status draft")
        elif not self.destination.draft and status not in CANONICAL_STATUSES:
            self.fail("MEMORY_STATUS", "status must be accepted, superseded, or retired")
        self._lists(fields, kind)
        return kind

    def _lists(self, fields: dict[str, Any], kind: str | None) -> None:
        count = 1 if kind == "project" else PROJECTS_MAX
        problem = _slug_list(fields["projects"], limit=PROJECT_SLUG_MAX, count=count)
        if problem:
            self.fail("MEMORY_PROJECTS", f"projects {problem}")
        elif kind == "project":
            expected = self.destination.project
            projects = fields["projects"]
            if len(projects) != 1:
                self.fail("MEMORY_PROJECTS", "a project record lists exactly one project")
            elif expected is not None and projects != [expected]:
                self.fail("MEMORY_PROJECTS", "projects must match the project directory")
        problem = _slug_list(fields["tags"], limit=TAG_SLUG_MAX, count=TAGS_MAX)
        if problem:
            self.fail("MEMORY_TAGS", f"tags {problem}")

    def _v2(self, fields: dict[str, Any], kind: str) -> None:
        record_id = fields["id"]
        if not isinstance(record_id, str) or not UUID4.match(record_id):
            self.fail("MEMORY_ID", "id must be a lowercase UUID version 4")
        self._scope(fields, kind)
        if "supersedes" in fields:
            self._supersedes(fields, kind)
        self._dates(fields)

    def _scope(self, fields: dict[str, Any], kind: str) -> None:
        scope = fields["scope"]
        if scope not in SCOPES:
            self.fail("MEMORY_SCOPE", "scope must be global, shared, or project")
            return
        allowed = {
            "preference": {"global"},
            "context": {"global", "shared"},
            "project": {"project"},
        }.get(kind, SCOPES)
        if scope not in allowed:
            self.fail("MEMORY_SCOPE", f"a {kind} record cannot have scope {scope}")
            return
        projects = fields["projects"]
        if not isinstance(projects, list):
            return
        if scope == "global" and projects:
            self.fail("MEMORY_SCOPE", "a global record lists no projects")
        elif scope == "project" and len(projects) != 1:
            self.fail("MEMORY_SCOPE", "a project-scoped record lists exactly one project")

    def _supersedes(self, fields: dict[str, Any], kind: str) -> None:
        targets = fields["supersedes"]
        if kind not in DRAFT_TYPES:
            self.fail("MEMORY_SUPERSEDES", f"a {kind} record cannot supersede")
        elif not self.destination.draft and fields["status"] != "accepted":
            self.fail("MEMORY_SUPERSEDES", "only an accepted record may supersede")
        if not isinstance(targets, list) or not 1 <= len(targets) <= SUPERSEDES_MAX:
            self.fail("MEMORY_SUPERSEDES", f"supersedes must list 1-{SUPERSEDES_MAX} IDs")
        elif not all(isinstance(item, str) and UUID4.match(item) for item in targets):
            self.fail("MEMORY_SUPERSEDES", "supersedes holds an invalid ID")
        elif len(set(targets)) != len(targets):
            self.fail("MEMORY_SUPERSEDES", "supersedes holds duplicates")
        elif fields["id"] in targets:
            self.fail("MEMORY_SUPERSEDES", "a record cannot supersede itself")

    def _dates(self, fields: dict[str, Any]) -> None:
        created = _as_date(fields["date"])
        parsed: dict[str, date] = {}
        for key in ("review_after", "valid_until"):
            if key not in fields:
                continue
            value = _as_date(fields[key])
            if value is None:
                self.fail("MEMORY_DATE", f"{key} must be a valid YYYY-MM-DD date")
            elif created is not None and value < created:
                self.fail("MEMORY_DATE", f"{key} is earlier than date")
            else:
                parsed[key] = value
        if len(parsed) == 2 and parsed["review_after"] > parsed["valid_until"]:
            self.fail("MEMORY_DATE", "review_after is later than valid_until")


def _cross_record(records: list[Record]) -> list[Finding]:
    """Schema 2 rules that span records: ID uniqueness and supersession edges."""
    findings: list[Finding] = []
    by_id: dict[str, Record] = {}
    for record in records:
        if record.id is None:
            continue
        first = by_id.setdefault(record.id, record)
        if first is not record:
            findings.append(
                Finding(
                    "error",
                    "MEMORY_DUPLICATE_ID",
                    record.path,
                    f"id is already used by {first.path}",
                )
            )
    canonical = {record.id: record for record in records if record.id and not record.draft}
    for record in records:
        for target_id in record.supersedes:
            target = canonical.get(target_id)
            if target is None:
                findings.append(
                    Finding(
                        "error",
                        "MEMORY_SUPERSEDES_TARGET",
                        record.path,
                        "supersedes an ID with no canonical record",
                    )
                )
            elif not record.draft and target.status != "superseded":
                findings.append(
                    Finding(
                        "error",
                        "MEMORY_SUPERSEDES_STATUS",
                        record.path,
                        f"supersedes {target.path}, which is not marked superseded",
                    )
                )
    findings.extend(_cycles(by_id))
    return findings


def _cycles(by_id: dict[str, Record]) -> list[Finding]:
    state: dict[str, int] = {}  # 1 visiting, 2 done
    reported: set[str] = set()
    findings: list[Finding] = []

    def visit(record_id: str, trail: list[str]) -> None:
        state[record_id] = 1
        trail.append(record_id)
        for target in by_id[record_id].supersedes:
            if target not in by_id:
                continue
            if state.get(target) == 1:
                for member in trail[trail.index(target) :]:
                    if member not in reported:
                        reported.add(member)
                        findings.append(
                            Finding(
                                "error",
                                "MEMORY_SUPERSEDES_CYCLE",
                                by_id[member].path,
                                "supersession edges form a cycle",
                            )
                        )
            elif target not in state:
                visit(target, trail)
        trail.pop()
        state[record_id] = 2

    for record_id in sorted(by_id):
        if record_id not in state:
            visit(record_id, [])
    return findings


# --- Secret scanner ----------------------------------------------------------

SCANNER = "agentbot-memory-secrets/v1"


@dataclass(frozen=True)
class SecretRule:
    id: str
    severity: Literal["block", "warn"]
    description: str
    pattern: re.Pattern[str]


# Mirrors secret-rules-v1.yaml in the memory design. A pattern or severity
# change that is not backward compatible needs a new scanner version.
SECRET_RULES = (
    SecretRule(
        "SECRET_PRIVATE_KEY",
        "block",
        "Private-key PEM or OpenSSH block",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----", re.MULTILINE),
    ),
    SecretRule(
        "SECRET_GITHUB_CLASSIC_TOKEN",
        "block",
        "GitHub prefixed token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),
    ),
    SecretRule(
        "SECRET_GITHUB_FINE_GRAINED_TOKEN",
        "block",
        "GitHub fine-grained personal access token",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,255}\b"),
    ),
    SecretRule(
        "SECRET_OPENAI_KEY",
        "block",
        "OpenAI-style secret key prefix",
        re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,255}\b"),
    ),
    SecretRule(
        "SECRET_AWS_ACCESS_KEY",
        "block",
        "AWS access-key identifier",
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ),
    SecretRule(
        "WARN_PASSWORD_ASSIGNMENT",
        "warn",
        "Password-like assignment",
        re.compile(r"(?i)\b(?:password|passwd|pwd)\b\s*[:=]\s*[\"\']?[^\s\"\']{8,}"),
    ),
    SecretRule(
        "WARN_CONNECTION_STRING",
        "warn",
        "Connection URI that may embed credentials",
        re.compile(
            r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s/@:]+:[^\s/@]+@"
        ),
    ),
)
WARNING_RULES = frozenset(rule.id for rule in SECRET_RULES if rule.severity == "warn")


def scan_secrets(relative: str, text: str) -> list[Finding]:
    """Rule ID, severity and line for each match. Never the matched value."""
    findings: list[Finding] = []
    for rule in SECRET_RULES:
        lines = sorted(
            {text.count("\n", 0, match.start()) + 1 for match in rule.pattern.finditer(text)}
        )
        severity: Severity = "error" if rule.severity == "block" else "warning"
        findings.extend(
            Finding(severity, rule.id, relative, rule.description, line) for line in lines
        )
    return findings


# --- Validation --------------------------------------------------------------


def validate(root: Path, *, acknowledge: Iterable[str] = ()) -> ValidationReport:
    """Validate the complete vault tree against the schema its marker names."""
    schema = read_marker(root)
    report = ValidationReport(
        root=root, schema=schema, acknowledged=frozenset(acknowledge) & WARNING_RULES
    )
    for relative in _listed_paths(root):
        if relative.startswith(IGNORED_PREFIXES):
            continue
        report.findings.extend(_check_file(root, relative, schema, report.records))
    if schema == 2:
        report.findings.extend(_cross_record(report.records))
    return report


def _check_file(root: Path, relative: str, schema: int, records: list[Record]) -> list[Finding]:
    def fail(rule: str, message: str) -> list[Finding]:
        return [Finding("error", rule, relative, message)]

    try:
        mode = os.lstat(root / relative).st_mode
    except FileNotFoundError:
        return []  # tracked but deleted: a dirty tree, which status reports
    if stat.S_ISLNK(mode):
        return fail("MEMORY_SYMLINK", "symlinks are not allowed in the vault")
    if not stat.S_ISREG(mode):
        return fail("MEMORY_SPECIAL_FILE", "not a regular file")
    if not relative.endswith(".md"):
        return (
            []
            if relative in NON_MARKDOWN_ALLOWED
            else fail("MEMORY_FILE_TYPE", "only Markdown is allowed")
        )
    try:
        text = _decode(read_bounded(root, relative, MAX_FILE_BYTES))
    except _Unsafe as error:
        return fail(error.rule, error.message)
    except FileNotFoundError:
        return []
    findings = [] if relative.startswith("exports/") else scan_secrets(relative, text)
    if relative in README_EXEMPT or relative.startswith(UNVALIDATED_PREFIXES):
        return findings
    destination = _destination(relative)
    if destination is None:
        return [*findings, *fail("MEMORY_LOCATION", "not a location a record may live in")]
    try:
        fields = _front_matter(text)
    except _Unsafe as error:
        return [*findings, *fail(error.rule, error.message)]
    check = _RecordCheck(schema, relative, destination)
    record = check.run(fields)
    if record is not None:
        records.append(record)
    return [*findings, *check.findings]


# --- Review dates ------------------------------------------------------------

DUE_LIST_DEFAULT = 20
DUE_LIST_MAX = 100


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _reviewable(records: Iterable[Record]) -> list[Record]:
    """Accepted canonical records. Superseded and retired stay out regardless of dates."""
    return [record for record in records if not record.draft and record.status == "accepted"]


@dataclass(frozen=True)
class DueItem:
    record: Record
    due: bool
    expired: bool


def due_queue(
    root: Path, *, today: date | None = None, limit: int = DUE_LIST_DEFAULT
) -> tuple[list[DueItem], int, int]:
    """Accepted records due for review or expired, oldest first.

    Returns the bounded list, the total, and the schema. Nothing is edited: a
    due or expired record keeps its status, file, and place in the vault.
    """
    report = validate(root)
    day = today or utc_today()
    items = [
        DueItem(record, record.due(day), record.expired(day))
        for record in _reviewable(report.records)
        if record.due(day) or record.expired(day)
    ]
    items.sort(
        key=lambda item: (
            item.record.review_after or item.record.valid_until or "",
            item.record.path,
        )
    )
    return items[: max(1, min(limit, DUE_LIST_MAX))], len(items), report.schema


def due_json(items: list[DueItem], total: int, schema: int, today: date) -> dict[str, Any]:
    return {
        "today": today.isoformat(),
        "schema": schema,
        "total": total,
        "shown": len(items),
        "records": [
            {
                "path": item.record.path,
                "id": item.record.id,
                "title": item.record.title,
                "status": item.record.status,
                "scope": item.record.scope,
                "projects": list(item.record.projects),
                "date": item.record.date,
                "review_after": item.record.review_after,
                "valid_until": item.record.valid_until,
                "due": item.due,
                "expired": item.expired,
            }
            for item in items
        ],
    }


# --- Status ------------------------------------------------------------------


def status(
    repo_root: Path, *, home: Path | None = None, environ: Mapping[str, str] | None = None
) -> VaultStatus:
    root, looked = find_vault(repo_root, home=home, environ=environ)
    if root is None:
        return VaultStatus(state="unconfigured", looked_in=looked)
    today = utc_today()
    try:
        report = validate(root)
        git = _git_state(root)
    except MemoryVaultError as error:
        return VaultStatus(state="broken", looked_in=looked, root=root, problem=str(error))
    counts: dict[str, int] = dict.fromkeys(sorted(CANONICAL_STATUSES), 0)
    for record in report.records:
        if not record.draft:
            counts[record.status] += 1
    return VaultStatus(
        state="ready" if report.valid else "invalid",
        looked_in=looked,
        root=root,
        schema=report.schema,
        statuses=counts,
        drafts=sum(1 for record in report.records if record.draft),
        due=sum(1 for record in _reviewable(report.records) if record.due(today)),
        expired=sum(1 for record in _reviewable(report.records) if record.expired(today)),
        errors=len(report.errors),
        warnings=len(report.warnings),
        **git,
    )


def _git_state(root: Path) -> dict[str, Any]:
    porcelain = _git(root, "status", "--porcelain=v1", "--untracked-files=normal")
    changed = len(porcelain.stdout.splitlines()) if porcelain.returncode == 0 else None
    ahead = behind = None
    counts = _git_text(root, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    if counts and len(counts.split()) == 2:
        behind, ahead = (int(value) for value in counts.split())
    return {
        "branch": _git_text(root, "symbolic-ref", "--short", "-q", "HEAD"),
        "commit": _git_text(root, "rev-parse", "--short", "HEAD"),
        "changed_paths": changed,
        "ahead": ahead,
        "behind": behind,
    }


# --- JSON --------------------------------------------------------------------


def status_json(item: VaultStatus) -> dict[str, Any]:
    return {
        "state": item.state,
        "root": str(item.root) if item.root else None,
        "looked_in": [str(path) for path in item.looked_in],
        "schema": item.schema,
        "branch": item.branch,
        "commit": item.commit,
        "changed_paths": item.changed_paths,
        "ahead": item.ahead,
        "behind": item.behind,
        "records": dict(item.statuses),
        "drafts": item.drafts,
        "due": item.due,
        "expired": item.expired,
        "errors": item.errors,
        "warnings": item.warnings,
        "problem": item.problem,
    }


def report_json(report: ValidationReport) -> dict[str, Any]:
    return {
        "state": "valid" if report.valid else "invalid",
        "root": str(report.root),
        "schema": report.schema,
        "scanner": SCANNER,
        "records": len([record for record in report.records if not record.draft]),
        "drafts": len([record for record in report.records if record.draft]),
        "acknowledged": sorted(report.acknowledged),
        "findings": [
            {
                "severity": item.severity,
                "rule": item.rule,
                "path": item.path,
                "line": item.line,
                "message": item.message,
                "acknowledged": item.severity == "warning" and item.rule in report.acknowledged,
            }
            for item in report.findings
        ],
    }
