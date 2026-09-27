"""Ticket M1: the two-clone concurrency experiment for two-tier memory (ADR-0009).

A local bare repository stands in for the shared remote; clones A and B stand
in for two machines. Everything is synthetic. Run this module directly to print
the stress-run metrics as JSON.
"""

from __future__ import annotations

import hashlib
import json
import random
import statistics
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src import memory_sync as sync
from src.memory import MemoryVaultError, scan_secrets, validate
from src.memory_projects import project_md

GIT = [
    "git",
    "-c",
    "user.name=fixture",
    "-c",
    "user.email=fixture@example.invalid",
    "-c",
    "init.defaultBranch=main",
]
PROJECTS = ("hot", "alpha", "beta")
FAKE_TOKEN = "ghp_" + "Z9y8X7w6" * 5


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        [*GIT, "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout


KINDS = {"lessons": "lesson", "decisions": "decision", "notes": "project"}
VAULT_ID = "f1f1f1f1-0000-4000-8000-000000000000"


def record(where: str, title: str, body: str = "Synthetic body.") -> bytes:
    """A valid schema 3 record for the vault path it is written to.

    The ID comes from the path and content, so writing the same record twice
    gives the same bytes, as a real replay would.
    """
    parts = [*where.split("/"), "", ""]
    if parts[0] == "projects":
        # Unknown folders fall back to a lesson: path tests hand in bad paths
        # on purpose, and the engine, not this helper, must refuse them.
        kind = "context" if parts[2] == "active-context.md" else KINDS.get(parts[2], "lesson")
        scope, projects = "project", f"[{parts[1]}]"
    else:
        kind = (
            "preference" if where == "core/user/preferences.md" else KINDS.get(parts[1], "lesson")
        )
        scope, projects = "global", "[]"
    h = hashlib.sha256(f"{where}\0{title}\0{body}".encode()).hexdigest()
    record_id = f"{h[:8]}-{h[8:12]}-4{h[13:16]}-8{h[17:20]}-{h[20:32]}"
    return (
        f"---\nschema: 3\nid: {record_id}\ntype: {kind}\ntitle: {json.dumps(title)}\n"
        f"date: 2026-09-27\nstatus: accepted\nscope: {scope}\nprojects: {projects}\n"
        f"tags: []\n---\n\n{body}\n"
    ).encode()


def at(project: str, rel: str) -> str:
    return f"projects/{project}/{rel}"


class Fleet:
    def __init__(self, base: Path) -> None:
        self.origin = base / "origin.git"
        subprocess.run([*GIT, "init", "-q", "-b", "main", "--bare", str(self.origin)], check=True)
        seed = base / "seed"
        subprocess.run(
            [*GIT, "clone", "-q", str(self.origin), str(seed)], check=True, capture_output=True
        )
        (seed / ".meta").mkdir()
        (seed / ".meta/vault.json").write_text(
            json.dumps({"agentbot_memory_schema": 3, "vault_id": VAULT_ID}) + "\n"
        )
        registry = {"projects": []}
        for index, project in enumerate(PROJECTS):
            folder = seed / "projects" / project
            (folder / "lessons").mkdir(parents=True)
            project_id = f"0000000{index}-0000-4000-8000-000000000000"
            origin = f"github.com/fixture/{project}"
            (folder / "project.md").write_bytes(project_md(project_id, project, origin))
            (folder / "active-context.md").write_bytes(
                record(at(project, "active-context.md"), f"{project} context")
            )
            for n in range(10):
                (folder / "lessons" / f"l{n}.md").write_bytes(
                    record(at(project, f"lessons/l{n}.md"), f"{project} lesson {n}")
                )
            registry["projects"].append({"id": project_id, "origin": origin, "folder": project})
        (seed / ".meta/projects.json").write_text(
            json.dumps(registry, indent=2, sort_keys=True) + "\n"
        )
        (seed / "core/user").mkdir(parents=True)
        (seed / "core/user/preferences.md").write_bytes(
            record("core/user/preferences.md", "Working preferences")
        )
        (seed / "core/lessons").mkdir()
        (seed / "core/lessons/c1.md").write_bytes(record("core/lessons/c1.md", "Core lesson"))
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", "seed")
        git(seed, "push", "-q", "origin", "main")
        self.a = base / "a"
        self.b = base / "b"
        for clone in (self.a, self.b):
            subprocess.run(
                [*GIT, "clone", "-q", str(self.origin), str(clone)], check=True, capture_output=True
            )

    def tip(self) -> str:
        return git(self.origin, "rev-parse", "main").strip()

    def head(self, clone: Path) -> str:
        return git(clone, "rev-parse", "HEAD").strip()

    def settle(self) -> None:
        for clone in (self.a, self.b, self.a):
            sync.sync(clone)


class FleetTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fleet = Fleet(Path(self._tmp.name))
        self.a, self.b = self.fleet.a, self.fleet.b

    def converged(self) -> None:
        self.fleet.settle()
        self.assertEqual(self.fleet.tip(), self.fleet.head(self.a))
        self.assertEqual(self.fleet.tip(), self.fleet.head(self.b))
        self.assertEqual([], sync.pending(self.a))
        self.assertEqual([], sync.pending(self.b))


class DeterministicCases(FleetTestCase):
    def test_01_independent_creates_converge(self) -> None:
        sync.put_project(
            self.a, "hot", "lessons/new-a.md", record(at("hot", "lessons/new-a.md"), "from A")
        )
        sync.put_project(
            self.b, "hot", "decisions/new-b.md", record(at("hot", "decisions/new-b.md"), "from B")
        )
        self.converged()
        for clone in (self.a, self.b):
            self.assertTrue((clone / "projects/hot/lessons/new-a.md").exists())
            self.assertTrue((clone / "projects/hot/decisions/new-b.md").exists())
        self.assertEqual([], sync.conflicts(self.a) + sync.conflicts(self.b))

    def test_02_cross_project_writes_do_not_conflict(self) -> None:
        sync.put_project(
            self.a, "alpha", "lessons/l0.md", record(at("alpha", "lessons/l0.md"), "alpha edited")
        )
        sync.put_project(
            self.b, "beta", "lessons/l0.md", record(at("beta", "lessons/l0.md"), "beta edited")
        )
        self.converged()
        self.assertIn(b"alpha edited", (self.b / "projects/alpha/lessons/l0.md").read_bytes())
        self.assertIn(b"beta edited", (self.a / "projects/beta/lessons/l0.md").read_bytes())
        self.assertEqual([], sync.conflicts(self.a) + sync.conflicts(self.b))

    def test_03_same_record_conflict_loses_nothing(self) -> None:
        sync.put_project(
            self.a,
            "hot",
            "active-context.md",
            record(at("hot", "active-context.md"), "A's context"),
        )
        op_b = sync.put_project(
            self.b,
            "hot",
            "active-context.md",
            record(at("hot", "active-context.md"), "B's context"),
        )
        sync.sync(self.a)
        result = sync.sync(self.b)
        self.assertEqual([op_b.id], [c["op"] for c in result.conflicts])
        conflict = result.conflicts[0]
        self.assertEqual("agent", conflict["resolver"])
        self.assertIn(b"A's context", (self.b / "projects/hot/active-context.md").read_bytes())
        preserved = git(
            self.b, "show", f"{conflict['recovery_ref']}:projects/hot/active-context.md"
        )
        self.assertIn("B's context", preserved)
        self.converged()

    def test_04_delete_versus_edit_has_no_silent_winner(self) -> None:
        delete = sync.delete_project(self.a, "hot", "lessons/l1.md")
        edit = sync.put_project(
            self.b, "hot", "lessons/l1.md", record(at("hot", "lessons/l1.md"), "edited on B")
        )
        sync.sync(self.a)
        result = sync.sync(self.b)
        self.assertEqual([edit.id], [c["op"] for c in result.conflicts])
        self.assertIn(
            "edited on B",
            git(
                self.b, "show", f"{result.conflicts[0]['recovery_ref']}:projects/hot/lessons/l1.md"
            ),
        )
        self.assertEqual("applied", sync.outcomes(self.a)[delete.id])
        # And the other order: the edit lands first, the delete conflicts.
        edit2 = sync.put_project(
            self.a, "hot", "lessons/l2.md", record(at("hot", "lessons/l2.md"), "edited on A")
        )
        delete2 = sync.delete_project(self.b, "hot", "lessons/l2.md")
        sync.sync(self.a)
        result = sync.sync(self.b)
        self.assertEqual([delete2.id], [c["op"] for c in result.conflicts])
        self.assertEqual("applied", sync.outcomes(self.a)[edit2.id])
        self.converged()
        self.assertTrue((self.a / "projects/hot/lessons/l2.md").exists())

    def test_05_forgetting_a_project_touches_nothing_else_and_reverts(self) -> None:
        before = git(self.a, "ls-tree", "-r", "HEAD", "projects/alpha")
        core_before = git(self.a, "ls-tree", "-r", "HEAD", "core", ".meta")
        forget = sync.forget_project(self.a, "alpha")
        sync.put_project(
            self.b, "beta", "lessons/new.md", record(at("beta", "lessons/new.md"), "beta addition")
        )
        self.converged()
        for clone in (self.a, self.b):
            self.assertFalse((clone / "projects/alpha").exists())
            self.assertTrue((clone / "projects/beta/lessons/new.md").exists())
            self.assertEqual(core_before, git(clone, "ls-tree", "-r", "HEAD", "core", ".meta"))
        commit = git(
            self.a, "log", "--format=%H", f"--grep=Agentbot-Op: {forget.id}", "main"
        ).strip()
        git(self.a, "revert", "--no-edit", commit)
        self.assertEqual(before, git(self.a, "ls-tree", "-r", "HEAD", "projects/alpha"))

    def test_05b_forget_conflicts_with_a_concurrent_write_to_that_project(self) -> None:
        forget = sync.forget_project(self.a, "alpha")
        sync.put_project(
            self.b,
            "alpha",
            "lessons/late.md",
            record(at("alpha", "lessons/late.md"), "late alpha write"),
        )
        sync.sync(self.b)
        result = sync.sync(self.a)
        self.assertEqual([forget.id], [c["op"] for c in result.conflicts])
        self.converged()
        self.assertTrue((self.a / "projects/alpha/lessons/late.md").exists())

    def test_06_core_conflicts_are_never_auto_resolved(self) -> None:
        sync.approve_core(
            self.a,
            "user/preferences.md",
            record("core/" + "user/preferences.md", "Preferences, A's approval"),
        )
        op_b = sync.approve_core(
            self.b,
            "user/preferences.md",
            record("core/" + "user/preferences.md", "Preferences, B's approval"),
        )
        sync.sync(self.a)
        result = sync.sync(self.b)
        self.assertEqual([op_b.id], [c["op"] for c in result.conflicts])
        self.assertEqual("human", result.conflicts[0]["resolver"])
        self.converged()
        self.assertIn(b"A's approval", (self.b / "core/user/preferences.md").read_bytes())

    def test_07_offline_backlog_replays_and_keeps_conflicts(self) -> None:
        url = git(self.a, "remote", "get-url", "origin").strip()
        git(self.a, "remote", "set-url", "origin", str(Path(self._tmp.name) / "unreachable.git"))
        offline = [
            sync.put_project(
                self.a, "hot", "lessons/off1.md", record(at("hot", "lessons/off1.md"), "offline 1")
            ),
            sync.put_project(
                self.a, "hot", "lessons/off2.md", record(at("hot", "lessons/off2.md"), "offline 2")
            ),
            sync.put_project(
                self.a,
                "hot",
                "active-context.md",
                record(at("hot", "active-context.md"), "offline context"),
            ),
        ]
        self.assertEqual("offline", sync.sync(self.a).state)
        self.assertEqual(3, len(sync.pending(self.a)))
        sync.put_project(
            self.b,
            "hot",
            "active-context.md",
            record(at("hot", "active-context.md"), "online context"),
        )
        sync.put_project(
            self.b, "beta", "lessons/on.md", record(at("beta", "lessons/on.md"), "online beta")
        )
        sync.sync(self.b)
        git(self.a, "remote", "set-url", "origin", url)
        result = sync.sync(self.a)
        self.assertEqual("synced", result.state)
        self.assertEqual({offline[0].id, offline[1].id}, set(result.applied))
        self.assertEqual([offline[2].id], [c["op"] for c in result.conflicts])
        self.converged()

    def test_08_a_secret_is_never_committed(self) -> None:
        before = self.fleet.head(self.a)
        with self.assertRaises(MemoryVaultError):
            sync.put_project(
                self.a,
                "hot",
                "lessons/leak.md",
                record(at("hot", "lessons/leak.md"), "leak", f"token {FAKE_TOKEN}"),
            )
        self.assertEqual(before, self.fleet.head(self.a))
        self.assertEqual([], sync.pending(self.a))
        self.assertEqual("", git(self.a, "status", "--porcelain"))

    def test_09_ssh_and_https_origins_resolve_to_one_project(self) -> None:
        sync.register_origin(self.a, "git@github.com:PamuduW/FreePlayground.git", "freeplayground")
        self.converged()
        entry = sync.resolve_project(self.b, "https://github.com/pamuduw/freeplayground.git")
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual("freeplayground", entry["folder"])
        self.assertEqual(
            entry, sync.resolve_project(self.b, "ssh://git@github.com/PamuduW/freeplayground")
        )

    def test_10_a_rename_keeps_the_same_project(self) -> None:
        hot = sync.resolve_project(self.a, "https://github.com/fixture/hot")
        assert hot is not None
        sync.register_origin(self.a, "https://github.com/fixture/hot-renamed", "hot", hot["id"])
        self.converged()
        renamed = sync.resolve_project(self.b, "git@github.com:fixture/hot-renamed.git")
        self.assertEqual(
            (hot["id"], "hot"), (renamed["id"], renamed["folder"]) if renamed else None
        )


class BoundaryCases(FleetTestCase):
    def test_registrations_merge_by_entry(self) -> None:
        sync.register_origin(self.a, "https://github.com/x/one", "one")
        sync.register_origin(self.b, "https://github.com/x/two", "two")
        self.converged()
        folders = {
            p["folder"]
            for p in json.loads((self.a / ".meta/projects.json").read_text())["projects"]
        }
        self.assertTrue({"one", "two"} <= folders)

    def test_a_contested_origin_or_folder_is_a_conflict(self) -> None:
        sync.register_origin(self.a, "https://github.com/x/same", "first")
        op = sync.register_origin(self.b, "https://github.com/x/same", "second")
        sync.sync(self.a)
        self.assertEqual([op.id], [c["op"] for c in sync.sync(self.b).conflicts])

    def test_paths_cannot_escape_the_project(self) -> None:
        for rel in ("../../core/user/preferences.md", "/abs.md", "a/../b.md", "notes.txt", ""):
            with self.subTest(rel=rel), self.assertRaises(MemoryVaultError):
                sync.put_project(self.a, "hot", rel, record(at("hot", rel), "x"))
        for project in ("../core", "Hot", ""):
            with self.subTest(project=project), self.assertRaises(MemoryVaultError):
                sync.put_project(
                    self.a, project, "lessons/x.md", record(at(project, "lessons/x.md"), "x")
                )

    def test_manual_edits_and_manual_commits_pause_automation(self) -> None:
        (self.a / "projects/hot/lessons/l0.md").write_bytes(
            record("projects/hot/lessons/l0.md", "half-finished edit")
        )
        with self.assertRaises(MemoryVaultError):
            sync.put_project(self.a, "hot", "lessons/x.md", record(at("hot", "lessons/x.md"), "x"))
        with self.assertRaises(MemoryVaultError):
            sync.sync(self.a)
        git(self.a, "commit", "-qam", "a manual commit")
        with self.assertRaises(MemoryVaultError):
            sync.sync(self.a)

    def test_the_remote_is_never_force_pushed(self) -> None:
        sync.put_project(self.a, "hot", "lessons/x.md", record(at("hot", "lessons/x.md"), "x"))
        sync.sync(self.a)
        first = self.fleet.tip()
        sync.put_project(self.b, "hot", "lessons/y.md", record(at("hot", "lessons/y.md"), "y"))
        sync.sync(self.b)
        self.assertEqual(
            "0", git(self.fleet.origin, "rev-list", "--count", f"main..{first}").strip()
        )


# --- Randomized stress pass ---------------------------------------------------


def run_stress(seed: int = 7, pairs: int = 100, *, phantom: bool = False) -> dict:
    rng = random.Random(seed)
    with tempfile.TemporaryDirectory() as tmp:
        fleet = Fleet(Path(tmp))
        clones = {"a": fleet.a, "b": fleet.b}
        issued: dict[str, tuple[str, str]] = {}
        refused_secrets = refused_invalid = 0
        times: list[float] = []
        for step in range(pairs * 2):
            name = "a" if step % 2 == 0 else "b"
            root = clones[name]
            project = "hot" if rng.random() < 0.8 else rng.choice(PROJECTS[1:])
            roll = rng.random()
            try:
                if roll < 0.02:
                    sync.put_project(
                        root,
                        project,
                        "lessons/leak.md",
                        record(at(project, "lessons/leak.md"), "leak", FAKE_TOKEN),
                    )
                    continue
                if roll < 0.04 and project != "hot" and (root / f"projects/{project}").exists():
                    op = sync.forget_project(root, project)
                elif roll < 0.07:
                    op = sync.approve_core(
                        root, "lessons/c1.md", record("core/" + "lessons/c1.md", f"core v{step}")
                    )
                elif roll < 0.25:
                    op = sync.put_project(
                        root,
                        project,
                        "active-context.md",
                        record(at(project, "active-context.md"), f"ctx {step}"),
                    )
                else:
                    lessons = (
                        sorted((root / f"projects/{project}/lessons").glob("*.md"))
                        if (root / f"projects/{project}/lessons").exists()
                        else []
                    )
                    if lessons and roll < 0.35:
                        op = sync.delete_project(
                            root, project, f"lessons/{rng.choice(lessons).name}"
                        )
                    elif lessons and roll < 0.6:
                        rel = f"lessons/{rng.choice(lessons).name}"
                        op = sync.put_project(
                            root, project, rel, record(at(project, rel), f"edit {step}")
                        )
                    else:
                        op = sync.put_project(
                            root,
                            project,
                            f"lessons/s{step}.md",
                            record(at(project, f"lessons/s{step}.md"), f"new {step}"),
                        )
                issued[op.id] = (name, op.kind)
            except MemoryVaultError as error:
                if "secret scanner" in str(error):
                    refused_secrets += 1
                    continue
                if "would make the vault invalid" in str(error):
                    # A write into a project this clone has forgotten: its
                    # folder would have no project.md.
                    refused_invalid += 1
                    continue
                raise
            if rng.random() < 0.35:
                times.append(sync.sync(root).seconds)
        for clone in ("a", "b", "a", "b"):
            times.append(sync.sync(clones[clone]).seconds)
        if phantom:
            # Negative control: an operation with no outcome anywhere must count as lost.
            issued["00000000-0000-4000-8000-00000000dead"] = ("a", "put")
        metrics = _check(fleet, clones, issued, refused_secrets, times)
        metrics["refused_invalid"] = refused_invalid
        return metrics


def _check(
    fleet: Fleet, clones: dict, issued: dict, refused_secrets: int, times: list[float]
) -> dict:
    outcomes = {**sync.outcomes(clones["a"]), **sync.outcomes(clones["b"])}
    conflicts = sync.conflicts(clones["a"]) + sync.conflicts(clones["b"])
    tip = fleet.tip()
    history = git(
        fleet.origin,
        "log",
        "--format=%H%x00%(trailers:key=Agentbot-Op,valueonly,separator=)%x00"
        "%(trailers:key=Agentbot-Tier,valueonly,separator=)%x1e",
        "main",
    )
    landed = {}
    boundary_violations = 0
    for entry in history.split("\x1e"):
        parts = entry.strip().split("\x00")
        if len(parts) < 3 or not parts[1].strip():
            continue
        commit, op_id, tier = parts[0], parts[1].strip(), parts[2].strip()
        landed[op_id] = commit
        paths = [
            p
            for p in git(
                fleet.origin, "diff-tree", "--no-commit-id", "--name-only", "-r", commit
            ).split()
            if p
        ]
        if tier == "project":
            roots = {"/".join(p.split("/")[:2]) for p in paths}
            if len(roots) > 1 or any(not p.startswith("projects/") for p in paths):
                boundary_violations += 1
        elif tier == "core" and any(not p.startswith(sync.CORE_PREFIXES) for p in paths):
            boundary_violations += 1
    lost = []
    for op_id in issued:
        outcome = outcomes.get(op_id)
        if outcome == "applied" and op_id not in landed:
            lost.append(op_id)
        elif outcome == "conflict":
            ref = next(c["recovery_ref"] for c in conflicts if c["op"] == op_id)
            if subprocess.run(
                [*GIT, "-C", str(clones[issued[op_id][0]]), "rev-parse", "-q", "--verify", ref],
                capture_output=True,
            ).returncode:
                lost.append(op_id)
        elif outcome not in {"applied", "noop", "conflict"}:
            lost.append(op_id)
    secret_commits = 0
    for obj in git(fleet.origin, "rev-list", "--objects", "--all").splitlines():
        sha = obj.split()[0]
        if git(fleet.origin, "cat-file", "-t", sha).strip() != "blob":
            continue
        text = git(fleet.origin, "cat-file", "-p", sha)
        if any(item.severity == "error" for item in scan_secrets("blob", text)):
            secret_commits += 1
    core_auto = sum(1 for c in conflicts if c["tier"] == "core" and c["op"] in landed)
    trees = {git(root, "rev-parse", "HEAD^{tree}").strip() for root in clones.values()}
    invalid_at_tip = sum(1 for item in validate(clones["a"]).findings if item.severity == "error")
    times.sort()
    return {
        "operations": len(issued),
        "refused_secrets": refused_secrets,
        "applied": sum(1 for o in (outcomes.get(i) for i in issued) if o == "applied"),
        "noops": sum(1 for o in (outcomes.get(i) for i in issued) if o == "noop"),
        "conflicts": len(conflicts),
        "core_conflicts": sum(1 for c in conflicts if c["tier"] == "core"),
        "conflict_rate": round(len(conflicts) / max(1, len(issued)), 3),
        "lost_writes": len(lost),
        "boundary_violations": boundary_violations,
        "invalid_at_tip": invalid_at_tip,
        "secret_blobs_on_remote": secret_commits,
        "core_conflicts_auto_resolved": core_auto,
        "pending_left": len(sync.pending(clones["a"])) + len(sync.pending(clones["b"])),
        "converged": len(trees) == 1
        and git(clones["a"], "rev-parse", "HEAD^{tree}").strip()
        == git(fleet.origin, "rev-parse", f"{tip}^{{tree}}").strip(),
        "sync_seconds_median": round(statistics.median(times), 3),
        "sync_seconds_p95": round(times[int(len(times) * 0.95) - 1], 3),
        "sync_calls": len(times),
        "remote_objects": git(fleet.origin, "count-objects", "-v").split("\n")[0],
    }


class StressPass(unittest.TestCase):
    def test_seeded_stress_holds_every_invariant(self) -> None:
        metrics = run_stress(seed=7, pairs=100)
        self.assertEqual(0, metrics["lost_writes"], metrics)
        self.assertEqual(0, metrics["boundary_violations"], metrics)
        self.assertEqual(0, metrics["invalid_at_tip"], metrics)
        self.assertEqual(0, metrics["secret_blobs_on_remote"], metrics)
        self.assertEqual(0, metrics["core_conflicts_auto_resolved"], metrics)
        self.assertEqual(0, metrics["pending_left"], metrics)
        self.assertTrue(metrics["converged"], metrics)
        self.assertGreater(metrics["refused_secrets"], 0, metrics)

    def test_the_checker_catches_a_lost_write(self) -> None:
        self.assertEqual(1, run_stress(seed=3, pairs=10, phantom=True)["lost_writes"])


if __name__ == "__main__":
    seeds = [int(arg) for arg in sys.argv[1:]] or [7]
    print(json.dumps({seed: run_stress(seed=seed) for seed in seeds}, indent=2))
