from __future__ import annotations

import json
import unittest

from src.mcp_models import McpCatalogEntry, McpClientContract, McpCredential
from src.mcp_render import (
    entry_fingerprint,
    parse_mcp_config,
    remove_owned_entry,
    render_mcp_config,
)


def remote_entry(*, environment: tuple[str, ...] = ("DOCS_TOKEN",)) -> McpCatalogEntry:
    return McpCatalogEntry(
        id="docs",
        name="agentbot_docs",
        publisher="Example",
        documentation_url="https://example.test/docs",
        license="MIT",
        terms_url="https://example.test/terms",
        reviewed_on="2026-09-13",
        eligible=True,
        transport="remote_http",
        origin="https://example.test/mcp",
        entrypoint=(),
        clients=tuple(
            McpClientContract(client=client, enabled=True)
            for client in ("claude", "codex", "cursor")
        ),
        credential=McpCredential(mode="bearer_env", environment=environment),
        allowed_tools=("search",),
        descriptor_fingerprints=("sha256:" + "a" * 64,),
        read_only_authority="public documentation only",
        limitations=("publisher behavior can change",),
        timeout_seconds=30,
        max_response_bytes=1000000,
        doctor_rules=("origin_matches",),
    )


class McpRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entry = remote_entry()

    def test_claude_renders_bearer_reference_and_preserves_unknown_fields(self) -> None:
        source = json.dumps({"theme": "dark", "mcpServers": {"manual": {"command": "x"}}})
        rendered = render_mcp_config("claude", source, (self.entry,))
        document = json.loads(rendered)
        self.assertEqual("dark", document["theme"])
        self.assertEqual({"command": "x"}, document["mcpServers"]["manual"])
        self.assertEqual(
            {
                "type": "http",
                "url": "https://example.test/mcp",
                "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"},
            },
            document["mcpServers"]["agentbot_docs"],
        )

    def test_cursor_renders_cursor_environment_reference(self) -> None:
        rendered = render_mcp_config("cursor", "", (self.entry,))
        document = json.loads(rendered)
        self.assertEqual(
            "Bearer ${env:DOCS_TOKEN}",
            document["mcpServers"]["agentbot_docs"]["headers"]["Authorization"],
        )

    def test_codex_merge_preserves_manual_server_and_comment(self) -> None:
        source = '# keep\n[mcp_servers.manual]\nurl = "https://example.test/mcp"\n'
        rendered = render_mcp_config("codex", source, (self.entry,))
        self.assertIn("# keep", rendered)
        self.assertIn("[mcp_servers.manual]", rendered)
        self.assertIn("[mcp_servers.agentbot_docs]", rendered)
        self.assertIn('bearer_token_env_var = "DOCS_TOKEN"', rendered)
        self.assertEqual(rendered, render_mcp_config("codex", rendered, (self.entry,)))

    def test_absent_json_file_gets_only_mcp_servers(self) -> None:
        rendered = render_mcp_config("claude", "", (self.entry,))
        self.assertEqual({"mcpServers"}, set(json.loads(rendered)))

    def test_malformed_input_fails_closed(self) -> None:
        for client, source in (("claude", "{"), ("cursor", "[]"), ("codex", "[broken")):
            with self.subTest(client=client):
                with self.assertRaisesRegex(ValueError, "invalid .* MCP configuration"):
                    render_mcp_config(client, source, (self.entry,))

    def test_wrong_mcp_servers_shape_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "mcpServers must be an object"):
            parse_mcp_config("claude", '{"mcpServers": []}')

    def test_literal_credential_value_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "credential environment name"):
            render_mcp_config("claude", "", (remote_entry(environment=("literal-secret",)),))

    def test_remove_deletes_only_named_entry(self) -> None:
        rendered = render_mcp_config(
            "cursor",
            '{"mcpServers":{"manual":{"command":"x"}}}',
            (self.entry,),
        )
        removed = json.loads(remove_owned_entry("cursor", rendered, "agentbot_docs"))
        self.assertEqual({"manual": {"command": "x"}}, removed["mcpServers"])

    def test_json_render_is_idempotent(self) -> None:
        rendered = render_mcp_config("claude", "", (self.entry,))
        self.assertEqual(rendered, render_mcp_config("claude", rendered, (self.entry,)))

    def test_entry_fingerprint_ignores_mapping_order(self) -> None:
        first = {"url": "https://example.test", "headers": {"A": "B"}}
        second = {"headers": {"A": "B"}, "url": "https://example.test"}
        self.assertEqual(entry_fingerprint(first), entry_fingerprint(second))
        self.assertRegex(entry_fingerprint(first), r"^sha256:[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
