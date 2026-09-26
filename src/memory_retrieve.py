"""Bounded, diverse retrieval straight from validated vault files.

There is no index to go stale: every call validates the tree and reads the
records it returns. Only records that validate with no finding at all are
eligible, so a file with a schema error or a secret-scanner hit is never
returned. Drafts are never retrieved.

Default retrieval excludes superseded, retired, and expired records, and
records beyond each pool's hot limit; ``history=True`` is the deliberate way
back to them. Scope is never guessed: with no project named, only global and
shared records are eligible. A named project adds that project's records, and
other projects' records need an explicit cross-project request.

Slots are allocated per pool before merging (the R2 composite quota), so one
project cannot take every result by owning most of the vault. Every result
carries its provenance: ID, path, status, date, scope, and projects. A brief
is disposable output, never a second authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .memory import (
    MAX_FILE_BYTES,
    SLUG,
    MemoryVaultError,
    Record,
    _decode,
    _Unsafe,
    read_bounded,
    utc_today,
    validate,
)

# Human-facing CLI ceilings, carried from schema v1.
SEARCH_DEFAULT = 8
SEARCH_MAX = 100
QUERY_MAX = 512
EXCERPT_MAX = 600
SHOW_DEFAULT_BYTES = 65_536
SHOW_MAX_BYTES = 262_144

# Model-facing limits, from the R2 recommendations.
TARGET_PROJECT_SLOTS = 4
PER_OTHER_PROJECT = 1
HOT_LIMITS = {"global": 32, "shared": 48, "project": 64}
BRIEF_DEFAULT_TOKENS = 800
BRIEF_MAX_TOKENS = 1_200
BRIEF_ITEM_TOKENS = 300
PROJECT_SHARE = 0.65
DUPLICATE_SIMILARITY = 0.8

WORD = re.compile(r"[a-z0-9]+")


def estimate_tokens(text: str) -> int:
    """The planning proxy the research used: four characters per token."""
    return (len(text) + 3) // 4


@dataclass(frozen=True)
class Request:
    project: str | None = None
    cross_project: bool = False
    history: bool = False
    kind: str | None = None
    tag: str | None = None


@dataclass(frozen=True)
class Hit:
    record: Record
    pool: str  # global, shared, project, other
    score: float
    line: int | None = None
    excerpt: str = ""
    labels: tuple[str, ...] = ()

    def pointer(self) -> dict[str, Any]:
        record = self.record
        return {
            "id": record.id,
            "path": record.path,
            "status": record.status,
            "date": record.date,
            "scope": scope_of(record),
            "projects": list(record.projects),
            "labels": list(self.labels),
        }


@dataclass
class Retrieval:
    hits: list[Hit] = field(default_factory=list)
    considered: int = 0
    excluded: dict[str, int] = field(default_factory=dict)


def scope_of(record: Record) -> str:
    """v2 scope, or the v1 reading: typed singletons and project paths, else shared."""
    if record.scope:
        return record.scope
    if record.type == "preference":
        return "global"
    if record.type == "project":
        return "project"
    return "shared" if not record.projects or len(record.projects) > 1 else "project"


def _check_request(request: Request) -> None:
    if request.project is not None and not SLUG.match(request.project):
        raise MemoryVaultError("--project must be a project slug")
    if request.project and request.cross_project:
        raise MemoryVaultError("name one project, or ask for cross-project results, not both")


def _pool(record: Record, request: Request) -> str | None:
    """Which pool a record may be offered from under this request, if any."""
    scope = scope_of(record)
    if scope in {"global", "shared"}:
        return scope
    if request.project is not None and request.project in record.projects:
        return "project"
    if request.cross_project:
        return "other"
    return None


def _lifecycle(record: Record, today: date) -> str | None:
    if record.status != "accepted":
        return record.status
    if record.expired(today):
        return "expired"
    return None


def candidates(
    root: Path, request: Request, *, today: date | None = None
) -> tuple[list[tuple[Record, str, tuple[str, ...]]], Retrieval]:
    """Eligible (record, pool, labels) and the exclusion counts."""
    _check_request(request)
    day = today or utc_today()
    report = validate(root)
    flagged = {item.path for item in report.findings}
    summary = Retrieval()
    pools: dict[str, list[tuple[Record, str, tuple[str, ...]]]] = {}

    def exclude(reason: str) -> None:
        summary.excluded[reason] = summary.excluded.get(reason, 0) + 1

    for record in report.records:
        if record.draft:
            continue
        summary.considered += 1
        if record.path in flagged:
            exclude("finding")
            continue
        pool = _pool(record, request)
        if pool is None:
            exclude("scope")
            continue
        if request.kind and record.type != request.kind:
            exclude("filter")
            continue
        if request.tag and request.tag not in record.tags:
            exclude("filter")
            continue
        state = _lifecycle(record, day)
        if state is not None and not request.history:
            exclude(state)
            continue
        labels = (state,) if state else ()
        if record.due(day):
            labels = (*labels, "review due")
        key = pool if pool != "other" else f"other:{','.join(record.projects)}"
        pools.setdefault(key, []).append((record, pool, labels))

    eligible: list[tuple[Record, str, tuple[str, ...]]] = []
    for key, members in pools.items():
        members.sort(key=lambda item: (item[0].date, item[0].path), reverse=True)
        limit = HOT_LIMITS.get(key.split(":")[0], HOT_LIMITS["project"])
        if not request.history and len(members) > limit:
            exclude_count = len(members) - limit
            summary.excluded["cold"] = summary.excluded.get("cold", 0) + exclude_count
            members = members[:limit]
        eligible.extend(members)
    return eligible, summary


def _terms(text: str) -> set[str]:
    return set(WORD.findall(text.lower()))


def _similar(first: Record, second: Record) -> bool:
    a, b = _terms(first.title), _terms(second.title)
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= DUPLICATE_SIMILARITY


def _body(root: Path, record: Record) -> str:
    try:
        text = _decode(read_bounded(root, record.path, MAX_FILE_BYTES))
    except (_Unsafe, OSError) as error:
        raise MemoryVaultError(f"{record.path} changed while being read; retry") from error
    lines = text.split("\n")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.rstrip("\r") == "---"), 0)
    return "\n".join(lines[end + 1 :])


def _match(root: Path, record: Record, terms: set[str]) -> tuple[float, int | None, str]:
    body = _body(root, record)
    title, tags = _terms(record.title), set(record.tags)
    score = 3.0 * len(terms & title) + 2.0 * len(terms & tags)
    first_line: int | None = None
    body_hits = set()
    for number, line in enumerate(body.split("\n"), 1):
        found = terms & _terms(line)
        if found and first_line is None:
            first_line = number
        body_hits |= found
    score += len(body_hits)
    excerpt = ""
    if first_line is not None:
        excerpt = body.split("\n")[first_line - 1].strip()[:EXCERPT_MAX]
    return score, first_line, excerpt


def _project_key(hit: Hit) -> str:
    return ",".join(hit.record.projects)


def _reserved(ranked: dict[str, list[Hit]]) -> list[Hit]:
    """The first pick of each pool, and of each other project, best first."""
    picks = [ranked[pool][0] for pool in ("global", "shared", "project") if ranked.get(pool)]
    firsts: dict[str, Hit] = {}
    for hit in ranked.get("other", []):
        firsts.setdefault(_project_key(hit), hit)
    picks.extend(sorted(firsts.values(), key=lambda hit: (-hit.score, hit.record.path)))
    return picks


def _allocate(ranked: dict[str, list[Hit]], limit: int) -> list[Hit]:
    """Reserve slots per pool before merging; then fill by score, skipping near-duplicates."""
    chosen: list[Hit] = []
    per_project: dict[str, int] = {}

    def fits(hit: Hit) -> bool:
        if any(_similar(hit.record, other.record) for other in chosen):
            return False
        if hit.pool == "project":
            return per_project.get("target", 0) < TARGET_PROJECT_SLOTS
        if hit.pool == "other":
            key = ",".join(hit.record.projects)
            return per_project.get(key, 0) < PER_OTHER_PROJECT
        return True

    def take(hit: Hit) -> None:
        chosen.append(hit)
        if hit.pool == "project":
            per_project["target"] = per_project.get("target", 0) + 1
        elif hit.pool == "other":
            key = ",".join(hit.record.projects)
            per_project[key] = per_project.get(key, 0) + 1

    # Reserves before any merge: the best global and shared result, the best
    # target-project result, then one per other project, so no project wins
    # every slot merely by owning most of the candidates.
    for hit in _reserved(ranked):
        if len(chosen) < limit and fits(hit):
            take(hit)
    remaining = sorted(
        (hit for hits in ranked.values() for hit in hits if hit not in chosen),
        key=lambda hit: (-hit.score, hit.record.path),
    )
    for hit in remaining:
        if len(chosen) >= limit:
            break
        if fits(hit):
            take(hit)
    return sorted(chosen, key=lambda hit: (-hit.score, hit.record.path))


def search(
    root: Path,
    query: str,
    request: Request,
    *,
    limit: int = SEARCH_DEFAULT,
    today: date | None = None,
) -> Retrieval:
    if not query.strip() or len(query) > QUERY_MAX:
        raise MemoryVaultError(f"the query must be 1-{QUERY_MAX} characters")
    limit = max(1, min(limit, SEARCH_MAX))
    terms = _terms(query)
    eligible, summary = candidates(root, request, today=today)
    ranked: dict[str, list[Hit]] = {}
    for record, pool, labels in eligible:
        score, line, excerpt = _match(root, record, terms)
        if score <= 0:
            continue
        ranked.setdefault(pool, []).append(Hit(record, pool, score, line, excerpt, labels))
    for hits in ranked.values():
        hits.sort(key=lambda hit: (-hit.score, hit.record.path))
    summary.hits = _allocate(ranked, limit)
    return summary


def show(
    root: Path, relative: str, request: Request, *, max_bytes: int = SHOW_DEFAULT_BYTES
) -> tuple[Hit, str]:
    """One record's provenance and bounded body, if this request may see it."""
    _check_request(request)
    max_bytes = max(1, min(max_bytes, SHOW_MAX_BYTES))
    report = validate(root)
    record = next(
        (item for item in report.records if item.path == relative and not item.draft), None
    )
    if record is None:
        raise MemoryVaultError(f"{relative} is not a valid canonical record")
    if any(item.path == relative for item in report.findings):
        raise MemoryVaultError(f"{relative} has validation or scanner findings; fix them first")
    pool = _pool(record, request)
    if pool is None:
        raise MemoryVaultError(
            f"{relative} is scoped to another project; name it with --project or ask --cross-project"
        )
    state = _lifecycle(record, utc_today())
    body = _body(root, record)
    encoded = body.encode("utf-8")[:max_bytes]
    return Hit(record, pool, 0.0, labels=(state,) if state else ()), encoded.decode(
        "utf-8", "ignore"
    )


# --- Brief -------------------------------------------------------------------


@dataclass
class Brief:
    text: str
    tokens: int
    budget: int
    items: list[Hit]
    omitted: int


def _lead(root: Path, record: Record, limit_tokens: int) -> str:
    """The first paragraph of prose, without headings, bounded."""
    paragraph: list[str] = []
    for line in _body(root, record).split("\n"):
        stripped = line.strip()
        if stripped.startswith("#") or (not stripped and not paragraph):
            continue
        if not stripped:
            break
        paragraph.append(stripped)
    text = " ".join(paragraph)
    return text[: limit_tokens * 4].rstrip()


def _item_text(root: Path, hit: Hit) -> str:
    record = hit.record
    where = scope_of(record) + (f" {','.join(record.projects)}" if record.projects else "")
    labels = f" [{', '.join(hit.labels)}]" if hit.labels else ""
    lead = _lead(root, record, BRIEF_ITEM_TOKENS - 40)
    line = f"- **{record.title}** ({record.type}, {record.date}, {where}){labels} `{record.id or record.path}` {record.path}"
    return line + (f"\n  {lead}" if lead else "")


def brief(
    root: Path, request: Request, *, tokens: int = BRIEF_DEFAULT_TOKENS, today: date | None = None
) -> Brief:
    """A disposable session brief within a hard token ceiling."""
    budget = max(100, min(tokens, BRIEF_MAX_TOKENS))
    eligible, _ = candidates(
        root, Request(project=request.project, cross_project=request.cross_project), today=today
    )
    by_pool: dict[str, list[Hit]] = {}
    for record, pool, labels in eligible:
        by_pool.setdefault(pool, []).append(Hit(record, pool, 0.0, labels=labels))
    header = (
        "# Memory brief\n\nDerived from accepted vault records; the files are the authority. "
        "Read one with `agentbot memory show PATH`.\n"
    )
    used = estimate_tokens(header)
    project_cap = int(budget * PROJECT_SHARE)
    project_used = 0
    chosen: list[tuple[Hit, str]] = []
    omitted = 0
    # The same reserves as search, newest first within each pool; then the
    # rest in date order, with the project slice capped.
    for hits in by_pool.values():
        hits.sort(key=lambda hit: (hit.record.date, hit.record.path), reverse=True)
    order = _reserved(by_pool)
    rest = [hit for hits in by_pool.values() for hit in hits if hit not in order]
    rest.sort(key=lambda hit: (hit.record.date, hit.record.path), reverse=True)
    order.extend(rest)
    for hit in order:
        text = _item_text(root, hit)
        cost = estimate_tokens(text) + 1
        in_project = hit.pool in {"project", "other"}
        if used + cost > budget or (in_project and project_used + cost > project_cap):
            omitted += 1
            continue
        if any(_similar(hit.record, other.record) for other, _ in chosen):
            omitted += 1
            continue
        chosen.append((hit, text))
        used += cost
        if in_project:
            project_used += cost
    body = header + "\n" + "\n".join(text for _, text in chosen) + "\n"
    return Brief(
        text=body,
        tokens=estimate_tokens(body),
        budget=budget,
        items=[hit for hit, _ in chosen],
        omitted=omitted,
    )


# --- JSON --------------------------------------------------------------------


def search_json(result: Retrieval) -> dict[str, Any]:
    return {
        "considered": result.considered,
        "excluded": result.excluded,
        "results": [
            {
                **hit.pointer(),
                "title": hit.record.title,
                "score": hit.score,
                "line": hit.line,
                "excerpt": hit.excerpt,
            }
            for hit in result.hits
        ],
    }


def show_json(hit: Hit, body: str) -> dict[str, Any]:
    return {**hit.pointer(), "title": hit.record.title, "type": hit.record.type, "body": body}


def brief_json(item: Brief) -> dict[str, Any]:
    return {
        "tokens": item.tokens,
        "budget": item.budget,
        "omitted": item.omitted,
        "sources": [hit.pointer() for hit in item.items],
        "text": item.text,
    }
