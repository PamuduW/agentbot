# Atlassian Cloud Read MCP Implementation Plan

> Execute locally on `development`. The user approved implementation in this session. Do not commit, push, or write credentials into the repository.

**Goal:** Add one local `atlassian_read` MCP for Jira and Confluence using separate scoped Cloud API tokens.

**Architecture:** Follow the existing GitLab local stdio facade. Expose fixed read operations over Atlassian's HTTPS gateway, with separate Basic authentication per product and no caller-controlled URL or HTTP method. Return bounded, untrusted JSON; paginate one page per call using opaque continuation arguments rather than following server URLs.

**Tech stack:** Existing Python 3.10+, MCP SDK and httpx; no new dependencies.

**Spec:** User-approved proposal in this conversation: scoped Jira and Confluence tokens, fixed read tools, no mutations, fixed Cloud endpoints.

## Constraints and review focus

- Keep changes in `agentbot`; preserve the clean `development` checkout and repository boundaries.
- Runtime variables: `ATLASSIAN_MCP_EMAIL`, `ATLASSIAN_MCP_CLOUD_ID`, `JIRA_MCP_READ_TOKEN`, `CONFLUENCE_MCP_READ_TOKEN`.
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

## Completion limits

Local code and offline validation can complete without credentials. Live API checks and actual client registration remain explicitly pending until site configuration and securely supplied tokens exist. Rollback is removal of this task's local changes; no persistent user data is migrated.

## Verification result

Implemented locally on `development`; no commits or client registrations. The focused suite ran 57 tests (56 passed, one live skip). The complete Agentbot gate passed outside the sandbox in 210 seconds: 1,445 Python tests (six opt-in skips), coverage, all shell suites, Ruff, mypy, Bash syntax, ShellCheck, shfmt, workflow security, shared-contract and whitespace checks. The sandbox run stalled in the existing isolated-home launcher test; the same test passed outside the sandbox. Independent review found one malformed-Unicode error boundary; a failing regression was added, the boundary fixed, and all checks rerun. Live Cloud reads and actual native-client acceptance remain pending credentials.

## Follow-up: Token Config menu

The user requested saved Atlassian credentials through the existing Token Config menu.

1. Add a private, atomic credential store and a stdin-only CLI; verify unsafe files, malformed values, replacement failure, and masked output.
2. Add the Atlassian submenu with hidden token prompts and the existing terminal seam; verify save, cancel, remove, and secret-free output.
3. Resolve saved settings at MCP startup and status, and remove mandatory JSON-client environment references. Preserve optional Codex environment forwarding.
4. Update setup docs and run focused tests and the full repository gate. Keep real credentials, client registration, commits, and pushes outside this development task.

Follow-up complete locally: Atlassian is available under Token Config, with hidden prompts, atomic private storage, masked status, optional read checks, and removal. MCP startup and status load saved settings without exports; Codex also forwards optional XDG configuration. Reviewed and fixed shell-tracing disclosure, filesystem inspection errors, and nested JSON rejection. Final gate passed in 205 seconds: 1,458 Python tests, four end-to-end Atlassian menu tests, coverage (85%), all remaining shell suites and configured lint/type/security/format/contract checks. No real credentials were saved and no live Cloud calls, client registrations, commits or pushes were performed.
