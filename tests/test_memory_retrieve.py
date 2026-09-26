"""Bounded, diverse retrieval over a synthetic vault where one project dominates."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections import Counter
from datetime import date
from pathlib import Path
from unittest.mock import patch

from src import memory
from src import memory_retrieve as retrieve
from tests.support import run_cli_main
from tests.test_memory import FAKE_GITHUB_TOKEN, VaultFixture, _tree_digest, note, v1_vault, v2

TODAY = date(2026, 9, 30)
HOT = "hot"
OTHERS = ("alpha", "beta", "gamma")


def uid(index: int) -> str:
    return f"{index:08x}-0000-4000-8000-000000000000"


def body(topic: str, index: int) -> str:
    return f"## Notes\n\nThe {topic} deploy pipeline lesson number {index}.\nMore detail about caching.\n"


class Corpus:
    """150 records: 85% in one hot project, plus global, shared, and three small projects."""

    def __init__(self, root: Path) -> None:
        self.vault = VaultFixture(root, 2)
        self.vault.write(
            "preferences.md",
            note(
                v2("preference", uid(1), "global", title="Working preferences"), body("global", 1)
            ),
        )
        index = 10
        for n in range(128):  # 128 / 150 is about 85% of writes
            index += 1
            self.vault.write(
                f"projects/{HOT}/r{n:03}.md",
                note(
                    v2(
                        "project",
                        uid(index),
                        "project",
                        title=f"Hot record {n} {'x' * (n % 7)}",
                        projects=[HOT],
                        date=f"2026-{1 + n % 9:02}-{1 + n % 27:02}",
                    ),
                    body("hot", n),
                ),
            )
        for project in OTHERS:
            for n in range(4):
                index += 1
                self.vault.write(
                    f"projects/{project}/r{n}.md",
                    note(
                        v2(
                            "project",
                            uid(index),
                            "project",
                            title=f"{project} record {n}",
                            projects=[project],
                        ),
                        body(project, n),
                    ),
                )
        for n in range(6):
            index += 1
            self.vault.write(
                f"lessons/shared-{n}.md",
                note(
                    v2("lesson", uid(index), "shared", title=f"Shared pattern {n} about deploys"),
                    body("shared", n),
                ),
            )
        for n in range(3):
            index += 1
            self.vault.write(
                f"decisions/global-{n}.md",
                note(
                    v2("decision", uid(index), "global", title=f"Global decision {n}"),
                    body("global", n),
                ),
            )
        # Lifecycle exclusions.
        self.vault.write(
            "lessons/expired.md",
            note(
                v2(
                    "lesson",
                    uid(900),
                    "global",
                    title="Expired deploy advice",
                    valid_until="2026-09-29",
                ),
                body("expired", 0),
            ),
        )
        self.vault.write(
            "lessons/superseded.md",
            note(
                v2(
                    "lesson",
                    uid(901),
                    "global",
                    title="Superseded deploy advice",
                    status="superseded",
                ),
                body("old", 0),
            ),
        )
        self.vault.write(
            "lessons/retired.md",
            note(
                v2("lesson", uid(902), "global", title="Retired deploy advice", status="retired"),
                body("retired", 0),
            ),
        )
        self.root = self.vault.root


class RetrieveTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.corpus = Corpus(Path(cls._tmp.name) / "v")
        cls.root = cls.corpus.root

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def search(self, query: str = "deploy pipeline", **request: object) -> retrieve.Retrieval:
        limit = int(request.pop("limit", retrieve.SEARCH_DEFAULT))  # type: ignore[call-overload]
        return retrieve.search(
            self.root, query, retrieve.Request(**request), limit=limit, today=TODAY
        )  # type: ignore[arg-type]


class ScopeAndLifecycleTests(RetrieveTestCase):
    def test_the_corpus_validates_and_is_dominated_by_one_project(self) -> None:
        report = memory.validate(self.root)
        self.assertEqual([], report.findings)
        canonical = [r for r in report.records if not r.draft]
        hot = sum(1 for r in canonical if HOT in r.projects)
        self.assertGreaterEqual(hot / len(canonical), 0.8)

    def test_no_project_means_global_and_shared_only(self) -> None:
        result = self.search()
        self.assertTrue(result.hits)
        self.assertEqual({"global", "shared"}, {hit.pool for hit in result.hits})
        self.assertGreater(result.excluded.get("scope", 0), 0)

    def test_a_named_project_is_capped_and_global_shared_survive(self) -> None:
        result = self.search(project=HOT)
        pools = Counter(hit.pool for hit in result.hits)
        self.assertLessEqual(pools["project"], retrieve.TARGET_PROJECT_SLOTS)
        self.assertGreaterEqual(pools["global"], 1)
        self.assertGreaterEqual(pools["shared"], 1)
        self.assertLessEqual(len(result.hits), retrieve.SEARCH_DEFAULT)
        # Another project's records never leak into a single-project request.
        self.assertTrue(all(set(hit.record.projects) <= {HOT} for hit in result.hits))

    def test_cross_project_takes_one_per_project_first(self) -> None:
        result = self.search(cross_project=True, limit=10)
        projects = Counter(hit.record.projects[0] for hit in result.hits if hit.pool == "other")
        self.assertEqual(set(OTHERS) | {HOT}, set(projects))
        self.assertTrue(all(count == 1 for count in projects.values()))

    def test_lifecycle_exclusions_and_history(self) -> None:
        normal = {hit.record.path for hit in self.search("advice", limit=50).hits}
        self.assertFalse(
            normal & {"lessons/expired.md", "lessons/superseded.md", "lessons/retired.md"}
        )
        history = self.search("advice", history=True, limit=50)
        labels = {hit.record.path: hit.labels for hit in history.hits}
        self.assertEqual(("expired",), labels["lessons/expired.md"])
        self.assertEqual(("superseded",), labels["lessons/superseded.md"])
        self.assertEqual(("retired",), labels["lessons/retired.md"])

    def test_hot_pool_limit_makes_old_records_cold_not_gone(self) -> None:
        result = self.search("record", project=HOT, limit=100)
        self.assertEqual(128 - retrieve.HOT_LIMITS["project"], result.excluded.get("cold"))
        oldest = min(
            (r for r in memory.validate(self.root).records if HOT in r.projects),
            key=lambda r: (r.date, r.path),
        )
        historical = retrieve.search(
            self.root,
            oldest.title,
            retrieve.Request(project=HOT, history=True),
            limit=100,
            today=TODAY,
        )
        self.assertIn(oldest.path, [hit.record.path for hit in historical.hits])
        self.assertTrue((self.root / oldest.path).exists())

    def test_filters_and_provenance(self) -> None:
        result = self.search(project=HOT, kind="decision")
        self.assertTrue(result.hits)
        self.assertEqual({"decision"}, {hit.record.type for hit in result.hits})
        pointer = result.hits[0].pointer()
        self.assertEqual(
            {"id", "path", "status", "date", "scope", "projects", "labels"}, set(pointer)
        )
        self.assertTrue(
            all(len(hit.excerpt) <= retrieve.EXCERPT_MAX for hit in self.search(limit=50).hits)
        )

    def test_bad_requests_fail(self) -> None:
        for query in ("", " ", "x" * (retrieve.QUERY_MAX + 1)):
            with self.subTest(query=query[:5]), self.assertRaises(memory.MemoryVaultError):
                self.search(query)
        for request in ({"project": "../etc"}, {"project": HOT, "cross_project": True}):
            with self.subTest(request=request), self.assertRaises(memory.MemoryVaultError):
                self.search(**request)


class ShowTests(RetrieveTestCase):
    def test_show_enforces_scope_and_paths(self) -> None:
        hit, text = retrieve.show(self.root, "decisions/global-0.md", retrieve.Request())
        self.assertIn("deploy pipeline", text)
        self.assertEqual("decisions/global-0.md", hit.record.path)
        with self.assertRaises(memory.MemoryVaultError):
            retrieve.show(self.root, f"projects/{HOT}/r000.md", retrieve.Request())
        with self.assertRaises(memory.MemoryVaultError):
            retrieve.show(self.root, f"projects/{HOT}/r000.md", retrieve.Request(project="alpha"))
        retrieve.show(self.root, f"projects/{HOT}/r000.md", retrieve.Request(project=HOT))
        for bad in (
            "../x.md",
            "drafts/README.md",
            "templates/lesson.md",
            ".meta/vault.json",
            "missing.md",
        ):
            with self.subTest(bad), self.assertRaises(memory.MemoryVaultError):
                retrieve.show(self.root, bad, retrieve.Request(cross_project=True))

    def test_show_is_bounded(self) -> None:
        _, text = retrieve.show(
            self.root, "decisions/global-0.md", retrieve.Request(), max_bytes=10
        )
        self.assertEqual(10, len(text.encode("utf-8")))


class BriefTests(RetrieveTestCase):
    def test_brief_respects_budget_and_project_share(self) -> None:
        for budget in (400, 800, 1200, 5000):
            with self.subTest(budget=budget):
                result = retrieve.brief(
                    self.root, retrieve.Request(project=HOT), tokens=budget, today=TODAY
                )
                self.assertLessEqual(result.tokens, min(budget, retrieve.BRIEF_MAX_TOKENS))
                self.assertLessEqual(result.budget, retrieve.BRIEF_MAX_TOKENS)
                project_tokens = sum(
                    retrieve.estimate_tokens(retrieve._item_text(self.root, hit))
                    for hit in result.items
                    if hit.pool == "project"
                )
                self.assertLessEqual(project_tokens, result.budget * retrieve.PROJECT_SHARE + 1)
                pools = {hit.pool for hit in result.items}
                self.assertTrue({"global", "shared"} <= pools)
                for hit in result.items:
                    self.assertIn(hit.record.path, result.text)

    def test_a_mixed_brief_is_not_dominated(self) -> None:
        result = retrieve.brief(self.root, retrieve.Request(cross_project=True), today=TODAY)
        share = sum(1 for hit in result.items if HOT in hit.record.projects) / len(result.items)
        self.assertLess(share, 0.8)
        self.assertTrue(any(hit.pool in {"global", "shared"} for hit in result.items))

    def test_brief_excludes_lifecycle_states_and_is_disposable(self) -> None:
        before = _tree_digest(self.root)
        result = retrieve.brief(self.root, retrieve.Request(), today=TODAY)
        self.assertEqual(before, _tree_digest(self.root))
        for path in ("lessons/expired.md", "lessons/superseded.md", "lessons/retired.md"):
            self.assertNotIn(path, result.text)
        self.assertIn("the files are the authority", result.text)


class UnsafeRecordTests(unittest.TestCase):
    def test_a_record_with_a_secret_or_a_bad_file_is_never_returned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = VaultFixture(Path(tmp) / "v", 2)
            vault.write(
                "lessons/leak.md",
                note(
                    v2("lesson", uid(1), "global", title="Deploy leak"),
                    f"deploy {FAKE_GITHUB_TOKEN}\n",
                ),
            )
            vault.write(
                "lessons/ok.md",
                note(v2("lesson", uid(2), "global", title="Deploy ok"), "deploy fine\n"),
            )
            (vault.root / "lessons" / "link.md").symlink_to(vault.root / "lessons" / "ok.md")
            result = retrieve.search(vault.root, "deploy", retrieve.Request(), today=TODAY)
            self.assertEqual(["lessons/ok.md"], [hit.record.path for hit in result.hits])
            self.assertNotIn(FAKE_GITHUB_TOKEN, json.dumps(retrieve.search_json(result)))
            with self.assertRaises(memory.MemoryVaultError):
                retrieve.show(vault.root, "lessons/leak.md", retrieve.Request())
            self.assertNotIn(
                FAKE_GITHUB_TOKEN, retrieve.brief(vault.root, retrieve.Request(), today=TODAY).text
            )

    def test_v1_records_are_retrievable_without_guessing_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = v1_vault(Path(tmp) / "v")
            paths = {
                hit.record.path
                for hit in retrieve.search(vault.root, "record", retrieve.Request(), limit=50).hits
            }
            self.assertIn("lessons/2026-09-23-a-lesson.md", paths)
            self.assertNotIn("projects/alpha/2026-09-23-a-project.md", paths)
            self.assertNotIn(
                "decisions/2026-09-23-a-decision.md", paths
            )  # v1, one project: project-only
            scoped = {
                hit.record.path
                for hit in retrieve.search(
                    vault.root, "record", retrieve.Request(project="alpha"), limit=50
                ).hits
            }
            self.assertIn("projects/alpha/2026-09-23-a-project.md", scoped)


class RetrieveCliTests(RetrieveTestCase):
    def _cli(self, *argv: str) -> tuple[int, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBOT_MEMORY")}
        env.update(
            HOME=str(Path(self._tmp.name) / "home"),
            NO_COLOR="1",
            AGENTBOT_MEMORY_DIR=str(self.root),
        )
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(memory, "utc_today", return_value=TODAY),
        ):
            rc, stdout, _ = run_cli_main(
                ["agentbot", "--root", str(Path(self._tmp.name) / "agentbot"), *argv]
            )
        return rc, stdout

    def test_cli_round_trip(self) -> None:
        rc, stdout = self._cli("memory", "search", "deploy", "--project", HOT, "--json")
        self.assertEqual(0, rc)
        results = json.loads(stdout)["results"]
        self.assertTrue(results)
        rc, stdout = self._cli("memory", "show", results[0]["path"], "--project", HOT)
        self.assertEqual(0, rc, stdout)
        rc, stdout = self._cli("memory", "brief", "--project", HOT)
        self.assertEqual(0, rc)
        self.assertTrue(stdout.startswith("# Memory brief"))
        self.assertLessEqual(retrieve.estimate_tokens(stdout), retrieve.BRIEF_DEFAULT_TOKENS)
        rc, _ = self._cli("memory", "show", f"projects/{HOT}/r000.md")
        self.assertEqual(1, rc)
        rc, stdout = self._cli("memory", "search", "deploy")
        self.assertEqual(0, rc)
        self.assertIn("Not offered", stdout)


if __name__ == "__main__":
    unittest.main()
