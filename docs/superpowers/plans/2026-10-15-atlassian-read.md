# Atlassian Cloud Read MCP Implementation Plan

> Completed 2026-10-15. Implemented on `development`, then committed, pushed and merged to `main` with the user's authorization. Credentials stay outside the repository.

**Goal:** Add one local `atlassian_read` MCP for Jira and Confluence using separate scoped Cloud API tokens.

**Architecture:** Follow the existing GitLab local stdio facade. Expose fixed read operations over Atlassian's HTTPS gateway, with separate Basic authentication per product and no caller-controlled URL or HTTP method. Return bounded, untrusted JSON; paginate one page per call using opaque continuation arguments rather than following server URLs.

**Tech stack:** Existing Python 3.10+, MCP SDK and httpx; no new dependencies.

**Spec:** User-approved proposal in this conversation: scoped Jira and Confluence tokens, fixed read tools, no mutations, fixed Cloud endpoints.

## Constraints and review focus

- Keep product changes in `agentbot`; preserve unrelated work and repository boundaries.
- Load saved Token Config settings at runtime; support optional overrides through `ATLASSIAN_MCP_EMAIL`, `ATLASSIAN_MCP_CLOUD_ID`, `JIRA_MCP_READ_TOKEN` and `CONFLUENCE_MCP_READ_TOKEN`.
- Cover Jira issues/search/comments/projects/boards/sprints and Confluence search/pages/spaces/footer and inline comments.
- No writes, redirects, URL following, generic requests, GraphQL or resource/prompt surfaces.
- Test unknown tools and extra arguments, path traversal, wrong primitive types, missing credentials, credential crossover, redirects, oversized responses and sanitized failures.
- Scope restrictions are enforced by Atlassian when the user creates scoped tokens. Do not claim runtime introspection of all granted scopes without a documented API.
- Live validation requires user credentials and safe issue/page/board fixtures. No global setup runs or real API calls during offline validation.

## Tasks and requirement mapping

### 1. Fixed Cloud client and facade

Files: `src/atlassian_read_client.py`, `src/atlassian_read_mcp.py`, `tests/test_atlassian_read.py`.

- [x] Write failing tests for exact tool dispatch, gateway routes, product credentials, argument restrictions and bounded/sanitized HTTP responses.
- [x] Confirm failure against the absent integration, then implement the client and facade.
- [x] Verify all tools through the MCP protocol and ensure mutation/bypass calls cause zero HTTP requests.

### 2. Agentbot integration and contract

Files: `src/cli.py`, `mcp/catalog.json`, `mcp/contracts/atlassian_read.json`, affected MCP tests.

- [x] Write failing catalog/render/CLI tests, then add dispatch and a catalog entry with frozen descriptor fingerprints.
- [x] Verify native rendering for every supported client keeps secrets out of configuration.

### 3. Setup documentation and acceptance

Files: `docs/atlassian-read.md`, `docs/architecture.md`, `README.md`, `tests/integration/test_atlassian_read_live.py`.

- [x] Document token creation, endpoint-specific read scopes, runtime variables, preview/setup commands, and the limitations of scope verification.
- [x] Add an opt-in live read-only acceptance suite without recording credentials or returned content.
- [x] Run focused tests and `env -u NO_COLOR bash tests/run.sh`; review diff and status.

## Acceptance and verification limits

Implementation, release and the requested live acceptance are complete. Jira
project and Confluence space reads passed on the work laptop after correcting
the saved Cloud ID. The user confirmed the MCP works in Claude and Cursor.

The full opt-in suite for all ten tools, including inline comments, was not run
against the user's site. Codex and Antigravity rendering and MCP protocol
behavior were tested offline; native live acceptance was reported for Claude
and Cursor only. Token scopes were confirmed during creation, not introspected
by the server.

The implementation is published, so rollback uses a reviewed revert or removal
of the managed MCP registrations. Removing local development files is no longer
the release rollback procedure.

## Verification result

The implementation and Token Config menu were released in `6610e33`. Follow-up
releases added system certificate trust (`06fed1b`) and general MCP update
recovery (`eba1049`). Both `main` and `development` received the releases, and
the workspace pins were updated.

The latest Agentbot code release passed 1,462 Python tests, 85% coverage, all
shell suites, Ruff, mypy, Bash syntax, ShellCheck, formatting, workflow security,
shared-contract and whitespace checks, plus GitHub CI. Earlier independent
review findings covering malformed Unicode, shell tracing, filesystem errors
and nested JSON rejection were fixed and tested.

## Follow-up: Token Config menu

The user requested saved Atlassian credentials through the existing Token Config menu.

- [x] Add a private, atomic credential store and a stdin-only CLI; verify unsafe files, malformed values, replacement failure, and masked output.
- [x] Add the Atlassian submenu with hidden token prompts and the existing terminal seam; verify save, cancel, remove, and secret-free output.
- [x] Resolve saved settings at MCP startup and status, and remove mandatory JSON-client environment references. Preserve optional Codex environment forwarding.
- [x] Update setup docs and run focused tests and the full repository gate. Keep real credentials outside the repository.

The menu is released and in use. It provides hidden token prompts, atomic
private storage, masked status, read checks and removal. MCP startup and status
load saved settings without token exports. HTTPS uses system certificate trust;
Codex also forwards optional certificate and XDG configuration overrides.
Missing credentials produce setup warnings, while invalid saved settings remain
errors. The user's credentials were entered on the work laptop, outside the
repositories.
