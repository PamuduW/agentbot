from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .mcp_catalog import load_mcp_catalog
from .mcp_models import McpCatalog, McpPlan, McpStatusItem, McpStatusReport
from .mcp_state import McpStateStore
from .paths import AgentbotPaths

if TYPE_CHECKING:
    from .mcp_reconcile import McpReconcileContext

_ERROR_STATES = frozenset(
    {
        "unmanaged-conflict",
        "managed-drift",
        "config-invalid",
        "auth-reference-missing",
        "auth-rejected",
        "tool-missing",
        "unexpected-tool",
        "descriptor-drift",
        "wrong-readonly-mode",
        "prohibited-tool-present",
        "response-too-large",
        "timeout",
        "client-launch-failed",
    }
)
_WARNING_STATES = frozenset({"shadowed", "upstream-unreachable", "rate-limited"})


class McpService:
    def __init__(
        self,
        paths: AgentbotPaths,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.paths = paths
        self.environ = os.environ if environ is None else environ
        self.state_store = McpStateStore(paths.mcp_state_file)

    def catalog(self) -> McpCatalog:
        return load_mcp_catalog(self.paths.mcp_catalog_file)

    def status(self, *, live: bool = False) -> McpStatusReport:
        catalog = self.catalog()
        state = self.state_store.load()
        records = {record.key: record for record in state.managed}
        entries = {entry.id: entry for entry in catalog.entries}
        items: list[McpStatusItem] = []
        if not catalog.entries and not state.managed:
            return McpStatusReport(catalog_version=catalog.version, live=live, items=())

        for entry in catalog.entries:
            for contract in entry.clients:
                if not contract.enabled:
                    continue
                client = contract.client
                record = records.get((entry.id, client, entry.name))
                if not entry.eligible:
                    if record is not None:
                        items.append(
                            self._item(
                                entry.id,
                                client,
                                entry.name,
                                "config-invalid",
                                "owned entry remains after its catalog admission was withdrawn",
                            )
                        )
                    continue
                destination = self._client_destination(client)
                if record is None and not destination.exists() and not destination.is_symlink():
                    items.append(
                        self._item(entry.id, client, entry.name, "not-selected", "not selected")
                    )
                    continue
                from .mcp_render import entry_fingerprint

                try:
                    destination, native = self._native_entry(client, entry.name)
                except ValueError as error:
                    items.append(
                        self._item(entry.id, client, entry.name, "config-invalid", str(error))
                    )
                    continue
                if record is None:
                    status = "unmanaged-conflict" if native is not None else "not-selected"
                    detail = (
                        "matching name exists without Agentbot ownership"
                        if native is not None
                        else "not selected"
                    )
                elif record.destination != str(destination) or native is None:
                    status = "managed-drift"
                    detail = "owned entry is missing or its destination changed"
                elif entry_fingerprint(native) != record.rendered_fingerprint:
                    status = "managed-drift"
                    detail = "owned entry differs from the last Agentbot render"
                else:
                    missing = tuple(
                        name for name in entry.credential.environment if name not in self.environ
                    )
                    if missing:
                        status = "auth-reference-missing"
                        detail = "missing environment reference: " + ", ".join(missing)
                    else:
                        status = "ok"
                        detail = "owned configuration matches"
                items.append(self._item(entry.id, client, entry.name, status, detail))

        for record in state.managed:
            if record.catalog_id in entries:
                continue
            items.append(
                self._item(
                    record.catalog_id,
                    record.client,
                    record.name,
                    "config-invalid",
                    "ownership state references a catalog entry that no longer exists",
                )
            )
        return McpStatusReport(
            catalog_version=catalog.version,
            live=live,
            items=tuple(sorted(items, key=lambda item: (item.client, item.catalog_id, item.name))),
        )

    def plan(self, selection: Sequence[str], targets: Sequence[str]) -> McpPlan:
        from .mcp_reconcile import plan_mcp

        return plan_mcp(self._context(), selection, targets)

    def plan_off(self, selection: Sequence[str], targets: Sequence[str]) -> McpPlan:
        from .mcp_reconcile import plan_mcp_off

        return plan_mcp_off(self._context(), selection, targets)

    def setup(
        self,
        selection: Sequence[str],
        targets: Sequence[str],
        *,
        confirmed: bool,
    ) -> str:
        if not selection:
            raise ValueError("select at least one MCP server")
        self._require_confirmation(confirmed)
        from .mcp_reconcile import apply_mcp_plan

        return apply_mcp_plan(self._context(), self.plan(selection, targets))

    def off(
        self,
        selection: Sequence[str],
        targets: Sequence[str],
        *,
        confirmed: bool,
    ) -> str:
        if not selection:
            raise ValueError("select at least one MCP server")
        self._require_confirmation(confirmed)
        context = self._context()
        from .mcp_reconcile import apply_mcp_plan

        return apply_mcp_plan(context, self.plan_off(selection, targets))

    def restore(self, operation_id: str, *, confirmed: bool) -> None:
        self._require_confirmation(confirmed)
        from .mcp_reconcile import restore_mcp_operation

        restore_mcp_operation(self._context(), operation_id)

    def doctor_issues(self) -> tuple[tuple[str, str, str], ...]:
        issues = []
        for item in self.status().items:
            if item.severity not in {"warning", "error"}:
                continue
            issues.append((item.severity, f"mcp:{item.client}:{item.catalog_id}", item.detail))
        return tuple(issues)

    def _context(self) -> McpReconcileContext:
        from .mcp_reconcile import McpReconcileContext

        return McpReconcileContext(
            paths=self.paths,
            catalog=self.catalog(),
            state_store=self.state_store,
        )

    def _native_entry(self, client: str, name: str) -> tuple[Path, object | None]:
        from .mcp_render import parse_mcp_config

        destination = self._client_destination(client)
        if destination.is_symlink():
            raise ValueError(f"MCP configuration must not be a symlink: {destination}")
        if destination.exists() and not destination.is_file():
            raise ValueError(f"MCP configuration is not a regular file: {destination}")
        try:
            text = destination.read_text(encoding="utf-8") if destination.exists() else ""
        except (OSError, UnicodeError) as error:
            raise ValueError(f"cannot read MCP configuration: {destination}: {error}") from error
        document = parse_mcp_config(client, text)
        servers = document.get("mcp_servers" if client == "codex" else "mcpServers")
        return destination, None if servers is None else servers.get(name)

    def _client_destination(self, client: str) -> Path:
        if client == "claude":
            return self.paths.claude_home.parent / ".claude.json"
        if client == "codex":
            return self.paths.codex_home / "config.toml"
        if client == "cursor":
            return self.paths.cursor_home / "mcp.json"
        raise ValueError(f"unsupported MCP client: {client}")

    @staticmethod
    def _require_confirmation(confirmed: bool) -> None:
        if not confirmed:
            raise ValueError("MCP mutation requires explicit confirmation with --yes")

    @staticmethod
    def _item(catalog_id: str, client: str, name: str, status: str, detail: str) -> McpStatusItem:
        severity: Literal["ok", "info", "warning", "error"] = (
            "error"
            if status in _ERROR_STATES
            else "warning"
            if status in _WARNING_STATES
            else "ok"
            if status == "ok"
            else "info"
        )
        return McpStatusItem(catalog_id, client, name, status, detail, severity)
