from __future__ import annotations

import json
import unittest
from pathlib import Path
from urllib.parse import urlparse

from src.mcp_catalog import load_mcp_catalog
from src.mcp_contract import load_filter_contract

EXPECTED = {
    "context7": {
        "origin": "https://mcp.context7.com/mcp",
        "tools": ("resolve-library-id", "query-docs"),
    },
    "aws_knowledge": {
        "origin": "https://knowledge-mcp.global.api.aws",
        "tools": (
            "aws___read_documentation",
            "aws___search_documentation",
            "aws___list_regions",
            "aws___get_regional_availability",
            "aws___retrieve_skill",
        ),
    },
    "microsoft_learn": {
        "origin": "https://learn.microsoft.com/api/mcp",
        "tools": (
            "microsoft_docs_search",
            "microsoft_code_sample_search",
            "microsoft_docs_fetch",
        ),
    },
}


class McpOverlayContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.catalog = load_mcp_catalog(cls.root / "mcp" / "catalog.json")
        cls.entries = {entry.id: entry for entry in cls.catalog.entries}

    def contract(self, candidate: str) -> dict[str, object]:
        path = self.root / "mcp" / "contracts" / f"{candidate}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_each_candidate_has_a_fixed_anonymous_filter_contract(self) -> None:
        for candidate, expected in EXPECTED.items():
            with self.subTest(candidate=candidate):
                path = self.root / "mcp" / "contracts" / f"{candidate}.json"
                contract = self.contract(candidate)
                runtime = load_filter_contract(path, candidate)
                parsed = urlparse(runtime.origin)
                self.assertEqual("https", parsed.scheme)
                self.assertTrue(parsed.netloc)
                self.assertEqual(expected["origin"], runtime.origin)
                self.assertEqual(expected["tools"], runtime.allowed_tools)
                self.assertLessEqual(runtime.timeout_seconds, 60)
                self.assertLessEqual(runtime.max_response_bytes, 4_194_304)
                serialized = json.dumps(contract, sort_keys=True).lower()
                self.assertNotIn("credential", serialized)
                self.assertNotIn("authorization", serialized)
                self.assertNotIn("api_key", serialized)

    def test_catalog_routes_every_candidate_through_the_local_filter(self) -> None:
        for candidate, expected in EXPECTED.items():
            with self.subTest(candidate=candidate):
                entry = self.entries[candidate]
                runtime = load_filter_contract(
                    self.root / "mcp" / "contracts" / f"{candidate}.json",
                    candidate,
                )
                self.assertTrue(entry.eligible)
                self.assertEqual("local_stdio", entry.transport)
                self.assertIsNone(entry.origin)
                self.assertEqual(
                    ("agentbot", "mcp", "serve", "--catalog-id", candidate),
                    entry.entrypoint,
                )
                self.assertEqual("none", entry.credential.mode)
                self.assertEqual((), entry.credential.environment)
                self.assertEqual({"claude", "codex", "cursor"}, {c.client for c in entry.clients})
                self.assertTrue(all(c.enabled for c in entry.clients))
                self.assertEqual(expected["tools"], entry.allowed_tools)
                self.assertEqual(
                    tuple(runtime.descriptor_fingerprints[name] for name in runtime.allowed_tools),
                    entry.descriptor_fingerprints,
                )

    def test_aws_contract_excludes_account_and_execution_capabilities(self) -> None:
        contract = self.contract("aws_knowledge")
        tool_names = tuple(tool["name"] for tool in contract["filter"]["tools"])
        prohibited = (
            "call_aws",
            "script",
            "pricing",
            "infrastructure",
            "account",
            "sigv4",
            "presigned",
        )
        joined = " ".join(tool_names).lower()
        for marker in prohibited:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, joined)

    def test_every_tool_has_one_unique_full_descriptor_fingerprint(self) -> None:
        for candidate in EXPECTED:
            with self.subTest(candidate=candidate):
                runtime = load_filter_contract(
                    self.root / "mcp" / "contracts" / f"{candidate}.json",
                    candidate,
                )
                fingerprints = tuple(runtime.descriptor_fingerprints.values())
                self.assertEqual(len(runtime.allowed_tools), len(fingerprints))
                self.assertEqual(len(fingerprints), len(set(fingerprints)))
                for fingerprint in fingerprints:
                    self.assertRegex(fingerprint, r"^sha256:[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
