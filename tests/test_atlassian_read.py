from __future__ import annotations

import base64
import json
import os
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from mcp import Client, MCPError
from src.atlassian_read_client import AtlassianReadClient, AtlassianReadError
from src.atlassian_read_mcp import AtlassianReadMcp, build_atlassian_stdio_server, main
from src.mcp_catalog import load_mcp_catalog
from src.mcp_contract import descriptor_fingerprint
from src.mcp_render import parse_mcp_config, render_mcp_config

CLOUD_ID = "11111111-2222-3333-4444-555555555555"
EMAIL = "reader@example.test"
JIRA_TOKEN = "jira-test-read-token"
CONFLUENCE_TOKEN = "confluence-test-read-token"
CASES = {
    "jira_search": ({"jql": "project = DEMO"}, "jira", "/rest/api/3/search/jql"),
    "jira_issue": ({"issue_id": "DEMO-1"}, "jira", "/rest/api/3/issue/DEMO-1"),
    "jira_comments": ({"issue_id": "DEMO-1"}, "jira", "/rest/api/3/issue/DEMO-1/comment"),
    "jira_projects": ({}, "jira", "/rest/api/3/project/search"),
    "jira_boards": ({}, "jira", "/rest/agile/1.0/board"),
    "jira_sprints": ({"board_id": 7}, "jira", "/rest/agile/1.0/board/7/sprint"),
    "confluence_search": ({"cql": "type = page"}, "confluence", "/wiki/rest/api/search"),
    "confluence_page": ({"page_id": "42"}, "confluence", "/wiki/api/v2/pages/42"),
    "confluence_spaces": ({}, "confluence", "/wiki/api/v2/spaces"),
    "confluence_comments": (
        {"page_id": "42"},
        "confluence",
        "/wiki/api/v2/pages/42/footer-comments",
    ),
}


class AtlassianReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.requests: list[httpx.Request] = []
        self.response = httpx.Response(200, json={"results": [], "nextPageToken": "next"})

        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.response

        self.client = AtlassianReadClient(
            CLOUD_ID, EMAIL, JIRA_TOKEN, CONFLUENCE_TOKEN, transport=httpx.MockTransport(handle)
        )
        self.facade = AtlassianReadMcp(self.client)

    async def test_every_read_uses_fixed_gateway_get_and_product_credential(self) -> None:
        self.assertEqual(set(CASES), {tool.name for tool in self.facade.tools()})
        self.assertTrue(CASES)
        for name, (arguments, product, route) in CASES.items():
            with self.subTest(tool=name):
                result = await self.facade.call_tool(name, arguments)
                payload = json.loads(result.content[0].text)
                self.assertTrue(payload["untrusted"])
                request = self.requests[-1]
                self.assertEqual("GET", request.method)
                self.assertEqual("api.atlassian.com", request.url.host)
                self.assertEqual(f"/ex/{product}/{CLOUD_ID}{route}", request.url.path)
                token = JIRA_TOKEN if product == "jira" else CONFLUENCE_TOKEN
                expected = base64.b64encode(f"{EMAIL}:{token}".encode()).decode()
                self.assertEqual(f"Basic {expected}", request.headers["Authorization"])
                self.assertEqual(b"", request.content)
        self.assertEqual(len(CASES), len(self.requests))

    def test_reads_use_system_trust_without_shell_exports(self) -> None:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"SSL_CERT_FILE", "SSL_CERT_DIR"}
        }
        with patch.dict(os.environ, environment, clear=True):
            reference = ssl.create_default_context()
            bundle = Path("/etc/ssl/certs/ca-certificates.crt")
            if bundle.is_file():
                reference.load_verify_locations(cafile=str(bundle))
            system_roots = set(reference.get_ca_certs(binary_form=True))
            with patch("src.atlassian_read_client.httpx.Client", wraps=httpx.Client) as factory:
                self.client.jira_projects(limit=1)
        context = factory.call_args.kwargs.get("verify")
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(ssl.CERT_REQUIRED, context.verify_mode)
        self.assertTrue(context.check_hostname)
        self.assertTrue(system_roots)
        self.assertTrue(
            system_roots <= set(context.get_ca_certs(binary_form=True)),
            "system trust roots were not loaded",
        )

    def test_standalone_python_loads_the_distro_bundle_when_its_defaults_are_empty(self) -> None:
        bundle = Path("/etc/ssl/certs/ca-certificates.crt")
        self.assertTrue(bundle.is_file())
        expected = set(
            ssl.create_default_context(cafile=str(bundle)).get_ca_certs(binary_form=True)
        )
        self.assertTrue(expected)
        empty = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.assertFalse(empty.get_ca_certs())
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"SSL_CERT_FILE", "SSL_CERT_DIR"}
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("src.atlassian_read_client.ssl.create_default_context", return_value=empty),
            patch("src.atlassian_read_client.httpx.Client", wraps=httpx.Client) as factory,
        ):
            self.client.jira_projects(limit=1)
        context = factory.call_args.kwargs["verify"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(ssl.CERT_REQUIRED, context.verify_mode)
        self.assertTrue(
            expected <= set(context.get_ca_certs(binary_form=True)),
            "distro trust roots were not loaded",
        )

    def test_explicit_certificate_bundle_does_not_add_the_distro_roots(self) -> None:
        bundle = Path("/etc/ssl/certs/ca-certificates.crt")
        roots = ssl.create_default_context(cafile=str(bundle)).get_ca_certs(binary_form=True)
        self.assertGreater(len(roots), 1)
        with tempfile.TemporaryDirectory() as temporary:
            custom = Path(temporary) / "custom.pem"
            custom.write_text(ssl.DER_cert_to_PEM_cert(roots[0]))
            with (
                patch.dict(os.environ, {"SSL_CERT_FILE": str(custom), "SSL_CERT_DIR": ""}),
                patch("src.atlassian_read_client.httpx.Client", wraps=httpx.Client) as factory,
            ):
                self.client.jira_projects(limit=1)
        context = factory.call_args.kwargs["verify"]
        self.assertEqual(1, len(context.get_ca_certs()))
        self.assertTrue(context.check_hostname)
        self.assertEqual(ssl.CERT_REQUIRED, context.verify_mode)

    def test_invalid_certificate_override_returns_safe_error_before_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = str(Path(temporary) / "missing-private-ca.pem")
            with patch.dict(os.environ, {"SSL_CERT_FILE": missing}):
                with self.assertRaisesRegex(
                    AtlassianReadError, "^Atlassian TLS configuration failed$"
                ):
                    self.client.jira_projects(limit=1)
        self.assertEqual([], self.requests)

    async def test_mutations_and_generic_bypasses_make_no_requests(self) -> None:
        for name in (
            "jira_create_issue",
            "jira_transition_issue",
            "confluence_update_page",
            "confluence_delete_page",
            "raw_request",
            "graphql",
            "executeWrite",
        ):
            with self.subTest(name=name), self.assertRaises(MCPError):
                await self.facade.call_tool(name, {})
        self.assertEqual([], self.requests)

    async def test_invalid_arguments_are_rejected_before_transport(self) -> None:
        invalid = (
            ("jira_issue", {}),
            ("jira_issue", {"issue_id": "../project"}),
            ("jira_issue", {"issue_id": "https://evil.test"}),
            ("jira_issue", {"issue_id": "DEMO-1", "method": "DELETE"}),
            ("jira_search", {"jql": ""}),
            ("jira_search", {"jql": "x" * 9000}),
            ("jira_projects", {"limit": True}),
            ("jira_projects", {"limit": 101}),
            ("jira_projects", {"start_at": -1}),
            ("jira_sprints", {"board_id": False}),
            ("confluence_page", {"page_id": "42/../../content"}),
            ("confluence_page", {"page_id": 42}),
            ("confluence_comments", {"page_id": "42", "kind": "delete"}),
            ("confluence_spaces", {"url": "https://evil.test"}),
            ("confluence_spaces", {"cursor": None}),
        )
        for name, arguments in invalid:
            with self.subTest(tool=name, arguments=arguments), self.assertRaises(MCPError):
                await self.facade.call_tool(name, arguments)
        self.assertEqual([], self.requests)

    async def test_issue_read_does_not_update_view_history(self) -> None:
        await self.facade.call_tool("jira_issue", {"issue_id": "DEMO-1"})
        self.assertEqual("false", self.requests[0].url.params.get("updateHistory"))

    async def test_transport_errors_do_not_expose_request_details(self) -> None:
        def fail(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout(JIRA_TOKEN, request=request)

        client = AtlassianReadClient(
            CLOUD_ID, EMAIL, JIRA_TOKEN, CONFLUENCE_TOKEN, transport=httpx.MockTransport(fail)
        )
        with self.assertRaises(MCPError) as caught:
            await AtlassianReadMcp(client).call_tool("jira_projects", {})
        self.assertNotIn(JIRA_TOKEN, str(caught.exception))
        self.assertIn("timed out", str(caught.exception))

    async def test_continuations_are_query_values_and_links_are_never_followed(self) -> None:
        self.response = httpx.Response(
            200, json={"results": [], "_links": {"next": "https://evil.test/steal"}}
        )
        await self.facade.call_tool("confluence_spaces", {"cursor": "https://evil.test/a?x=1"})
        self.assertEqual(1, len(self.requests))
        self.assertEqual("api.atlassian.com", self.requests[0].url.host)
        self.assertEqual("https://evil.test/a?x=1", self.requests[0].url.params["cursor"])
        await self.facade.call_tool(
            "jira_search", {"jql": "project = DEMO", "next_page_token": "n"}
        )
        self.assertEqual("n", self.requests[-1].url.params["nextPageToken"])
        self.assertEqual("project = DEMO", self.requests[-1].url.params["jql"])
        await self.facade.call_tool("confluence_comments", {"page_id": "42", "kind": "inline"})
        self.assertTrue(self.requests[-1].url.path.endswith("/inline-comments"))

    async def test_redirects_and_http_failures_are_sanitized(self) -> None:
        for status in (302, 401, 403, 429, 500):
            self.requests.clear()
            self.response = httpx.Response(
                status, text=JIRA_TOKEN, headers={"Location": "https://evil.test"}
            )
            with self.subTest(status=status), self.assertRaises(MCPError) as caught:
                await self.facade.call_tool("jira_projects", {})
            self.assertNotIn(JIRA_TOKEN, str(caught.exception))
            self.assertEqual(1, len(self.requests))

    async def test_malformed_and_oversized_responses_are_refused(self) -> None:
        for response in (
            httpx.Response(200, text="not JSON"),
            httpx.Response(200, content=b" " * 4_194_305),
        ):
            self.response = response
            with self.assertRaises(MCPError):
                await self.facade.call_tool("confluence_spaces", {})

    async def test_invalid_unicode_returns_a_sanitized_mcp_error(self) -> None:
        self.response = httpx.Response(200, content=b'{"description":"\\ud800"}')
        with self.assertRaises(MCPError):
            await self.facade.call_tool("confluence_spaces", {})

    async def test_input_limit_and_serialized_output_limit_are_both_enforced(self) -> None:
        data = b'{"results":[]}'
        self.response = httpx.Response(200, content=data + b" " * (4_194_304 - len(data)))
        result = await self.facade.call_tool("confluence_spaces", {})
        self.assertEqual([], json.loads(result.content[0].text)["data"]["results"])
        self.response = httpx.Response(200, json={"description": "x" * 4_194_270})
        with self.assertRaises(MCPError):
            await self.facade.call_tool("confluence_spaces", {})

    async def test_credentials_and_credential_fields_do_not_leave_facade(self) -> None:
        encoded = base64.b64encode(f"{EMAIL}:{JIRA_TOKEN}".encode()).decode()
        self.response = httpx.Response(
            200,
            json={
                "description": f"{JIRA_TOKEN} {CONFLUENCE_TOKEN} {encoded}",
                "nested": [{"api_token": "another-credential", "title": "Page"}],
                "nextPageToken": "next",
            },
        )
        result = await self.facade.call_tool("jira_projects", {})
        output = result.content[0].text
        for secret in (JIRA_TOKEN, CONFLUENCE_TOKEN, encoded, "another-credential"):
            self.assertNotIn(secret, output)
        self.assertEqual("next", json.loads(output)["data"]["nextPageToken"])

    async def test_stdio_protocol_exposes_only_read_tools(self) -> None:
        server = build_atlassian_stdio_server(self.facade)
        capabilities = server.get_capabilities()
        self.assertIsNone(capabilities.prompts)
        self.assertIsNone(capabilities.resources)
        async with Client(server, raise_exceptions=True) as protocol:
            listed = await protocol.list_tools()
            self.assertEqual(set(CASES), {tool.name for tool in listed.tools})
            result = await protocol.call_tool("jira_issue", {"issue_id": "DEMO-1"})
            self.assertFalse(result.is_error)
            with self.assertRaises(MCPError):
                await protocol.call_tool("confluence_update_page", {"page_id": "42"})
        self.assertEqual(1, len(self.requests))

    def test_invalid_configuration_fails_without_echoing_values(self) -> None:
        for config in (
            ("../jira", EMAIL, JIRA_TOKEN, CONFLUENCE_TOKEN),
            (CLOUD_ID, "x\ny", JIRA_TOKEN, CONFLUENCE_TOKEN),
            (CLOUD_ID, EMAIL, "", CONFLUENCE_TOKEN),
            (CLOUD_ID, EMAIL, JIRA_TOKEN, "\nsecret"),
        ):
            with self.subTest(config=config), self.assertRaises(AtlassianReadError) as caught:
                AtlassianReadClient(*config)
            self.assertNotIn(JIRA_TOKEN, str(caught.exception))

    def test_main_missing_configuration_returns_failure_without_traceback(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict("os.environ", {}, clear=True),
            patch("sys.stderr") as stderr,
        ):
            self.assertEqual(1, main([], config_home=Path(directory) / "config"))
            self.assertTrue(stderr.write.called)

    def test_agentbot_cli_dispatches_to_local_atlassian_server(self) -> None:
        import argparse

        from src.cli import CommandContext, _handle_mcp
        from src.paths import default_paths

        context = CommandContext(
            args=argparse.Namespace(mcp_command="serve", catalog_id="atlassian_read"),
            paths=default_paths(),
            diagnostics=None,
            lifecycle=None,
        )
        with patch("src.atlassian_read_mcp.main", return_value=0) as serve:
            self.assertEqual(0, _handle_mcp(context))
            serve.assert_called_once_with([], config_home=context.paths.config_home)

    def test_frozen_contract_and_all_native_clients_match(self) -> None:
        root = Path(__file__).resolve().parents[1]
        contract = json.loads((root / "mcp/contracts/atlassian_read.json").read_text())
        tools = self.facade.tools()
        self.assertEqual({tool.name for tool in tools}, set(CASES))
        self.assertEqual(
            {item["name"]: item["descriptor_fingerprint"] for item in contract["tools"]},
            {tool.name: descriptor_fingerprint(tool) for tool in tools},
        )
        entry = next(
            item
            for item in load_mcp_catalog(root / "mcp/catalog.json").entries
            if item.id == "atlassian_read"
        )
        self.assertEqual(set(CASES), set(entry.allowed_tools))
        self.assertEqual(
            tuple(descriptor_fingerprint(tool) for tool in tools), entry.descriptor_fingerprints
        )
        required_env = {
            "ATLASSIAN_MCP_CLOUD_ID",
            "ATLASSIAN_MCP_EMAIL",
            "JIRA_MCP_READ_TOKEN",
            "CONFLUENCE_MCP_READ_TOKEN",
        }
        self.assertEqual((), entry.credential.environment)
        for client in ("claude", "codex", "cursor", "antigravity"):
            text = render_mcp_config(client, "", (entry,))
            key = "mcp_servers" if client == "codex" else "mcpServers"
            native = parse_mcp_config(client, text)[key][entry.name]
            self.assertEqual(["mcp", "serve", "--catalog-id", "atlassian_read"], native["args"])
            if client == "codex":
                self.assertEqual(
                    required_env | {"XDG_CONFIG_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR"},
                    set(native["env_vars"]),
                )
            elif client == "antigravity":
                self.assertNotIn("env", native)
            else:
                self.assertNotIn("env", native)
            self.assertNotIn(JIRA_TOKEN, text)
            self.assertNotIn(CONFLUENCE_TOKEN, text)


if __name__ == "__main__":
    unittest.main()
