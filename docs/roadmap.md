# Agentbot roadmap

**Status:** Phases 0, 1, 2, and 5 are complete. Phase 3 MCP ownership and Phase
4 memory remain deferred to the parent workspace roadmap. Slice 4M is complete,
but Slice 4.0A must not start until the planned cross-agent memory comparison is
resolved. The current core has one Python lifecycle backend, one diagnostics
snapshot, one command model, and focused shell adapters and tests.

This is a living roadmap. The runtime source and operational documentation are
the authority; this file records the delivered contracts and the future
direction. Historical research may retain older names, but active instructions
must use Agentbot and agentbot.

## Current source of truth

| Area | Live source | Status |
|---|---|---|
| Agentbot CLI and menu | install.sh, bin/agentbot, scripts/menu.sh | Live |
| Menu definitions | src/ui/menus.py plus scripts/menus/ dispatch | Live |
| Install component selection | scripts/menus/components.sh, src/install_plan.py | Live |
| Editor and CLI surfaces | src/vscode.py, src/cursor_statusline.py, src/cli_config.py | Live |
| Presentation contract | workspace docs/designs/presentation/ | Live |
| Workspace profile | agentos.yaml, src/workspace_profiles.py | Live |
| Canonical repository policy | base/AGENTS.md | Live |
| Claude template | base/CLAUDE.md | Live |
| Workspace renderer | src/workspace_render.py | Live |
| Local registration and resync | src/workspace_state.py, src/workspace_service.py | Live |
| Workspace commands | src/cli.py, install.sh | Live |
| Workspaces menu | scripts/menus/workspaces.sh | Live |
| Global machine baseline | global/AGENTS.md and install.sh global | Live |
| Curated skills | skills.sources.yaml and install.sh skills ... | Live |
| Dotfiles integration | sibling dotfiles Agentbot bridge | Live |
| Package catalog and MCP bundles | historical snapshots in archive/catalog/ and archive/mcp/ | Phase 3 (deferred) |
| Durable memory | workspace `docs/designs/memory/` design | Pending cross-agent memory comparison |
| Graphify CLI and assistant integration | sibling dotfiles component plus Agentbot integration | Live (Phase 5) |

Agentbot is the product, the public command, the Git repository, and the
clone directory. The repository was renamed from `agent_bootstrap` to
`agentbot` on 2026-09-05; GitHub redirects the old clone URL. AGENTBOT_TUI is
the active TUI environment variable.

## Phase dependency

```
Phase 0: slim skills + global baseline + static repository bootstrap ✅
    |
    v
Phase 1: standalone Agentbot menu + Dotfiles bridge + unified boot ✅
    |
    v
Phase 2: profiles + managed workspace render + local resync ✅
    |\
    | \--> Phase 5: optional Graphify CLI + assistant integration ✅
    |
    +----> Phase 3: package catalog + profile-filtered MCP bundles (deferred)
    |
    +----> Phase 4: durable memory (pending cross-agent comparison)
```

## Phase 0 — slim bootstrap ✅

The foundation is live:

- skills.sources.yaml declares curated public upstream sources;
- install.sh skills install/update/list/doctor manages skills through the Skills
  CLI and global lock;
- install.sh global renders global/AGENTS.md to machine-level agent outputs;
- agentbot boot provides the Phase 1 scaffold entrypoint;
- install.sh doctor validates the slim manifest, links, locks, and outputs.

## Phase 1 — standalone UX and unified Agentbot ✅

Phase 1 delivered the independent Agentbot menu and sibling bridge:

- install.sh and agentbot with no arguments open the TUI on a controlling TTY;
- headless invocation gives explicit Agentbot CLI guidance;
- Dotfiles launches the sibling Agentbot menu without duplicating its logic;
- agentbot update is a repository-first plan/confirm/apply transaction that
  reconciles skills and then refreshes registered workspaces and global outputs;
  `resync` remains the explicit workspace-focused preview/apply command;
- boot is the single project setup entrypoint and Agentbot is the active public
  product name.

## Phase 2 — profiles, workspace render, and local resync ✅

**Goal:** Maintain one canonical repository policy while generating the
compatibility files required by the selected agent surfaces, and allow the
operator to refresh registered folders safely.

### Ownership model

```
base/AGENTS.md
        |
        v
target/AGENTS.md
  Agentbot-managed baseline block
  project-owned ## Project section
        |
        +--> target/CLAUDE.md
        +--> target/.cursor/rules/agentbot-policy.mdc
```

AGENTS.md is always created or preserved and is the canonical repository
policy. The managed block is delimited by:

```
<!-- BEGIN AGENTBOT MANAGED BASELINE -->
...
<!-- END AGENTBOT MANAGED BASELINE -->
```

Only that block is refreshed from base/AGENTS.md. The ## Project section and all
other content remain project-owned. An existing unmarked AGENTS.md is preserved
as custom policy and is never overwritten by base resync.

CLAUDE.md is a generated pointer to AGENTS.md. Cursor receives the generated
.cursor/rules/agentbot-policy.mdc Project Rule with alwaysApply: true, which is
also a pointer rather than a copy: Cursor applies .cursor/rules/, AGENTS.md, and
CLAUDE.md together, so rendering the policy into the rule loaded it twice per
request. Cursor-only behavior, if any is ever needed, belongs in that rule;
portable policy does not. The Cursor filename describes policy, not skill
installation.

### Profiles and targets

agentos.yaml contains the intentionally small safe-default profile. The fixed
Phase 2 targets are agents, claude, and cursor. With no selector,
Agentbot selects all four. Codex and agents are aliases for the canonical
AGENTS.md target; no separate Codex file is created. Any custom selection still
includes AGENTS.md.

Phase 2 does not install skills, execute community scripts, write MCP files, or
choose arbitrary filesystem paths from profile data.

### Registration and state

Every successful agentbot boot, whether it targets the current directory or an
explicit path, registers the canonical folder locally. A Git target is stored
at its Git root; a non-Git target is stored at its canonical directory path.
agentbot workspace PATH --yes has the same registration behavior.

The local registry is
${XDG_CONFIG_HOME:-${HOME}/.config}/agentbot/workspaces.json. It is
private local operator state, mode 0600 inside a mode 0700 directory, versioned,
atomic, and credential-free. It is never written into a project or Git-tracked.

### Public operations

```
agentbot boot
agentbot boot --agents
agentbot boot --agents --claude
agentbot boot --agents --cursor
agentbot boot --agents --claude --cursor
agentbot boot --profile safe-default

agentbot workspace PATH
agentbot workspace --yes PATH
agentbot workspaces
agentbot resync --dry-run --all
agentbot resync --yes --all
agentbot resync --yes PATH
```

Workspace commands preview by default. Existing unmarked compatibility files
are conflicts and are not replaced. The legacy --force boot option is not part
of Phase 2. No command stages, commits, pushes, resets, deletes, or changes
Git ignore rules automatically.

Batch resync processes enabled records independently, reports every result, and
does not delete missing records or silently convert a recorded Git workspace
into a directory record. Dirty Git state is reported but does not by itself
block an explicit apply.

### Phase 2 verification

The implementation is covered by:

- profile schema and invalid-configuration tests;
- managed-boundary, legacy migration, conflict, idempotency, and atomic-write
  renderer tests;
- private state, permissions, malformed-state, and symlink tests;
- folder/Git identity, custom policy, resync, and failure-isolation tests;
- CLI preview/apply/list/resync tests;
- Agentbot dispatcher, boot, menu, syntax, and temporary-folder acceptance
  tests.

## Phase 3 — package catalog and MCP bundles

**Goal:** Let an explicit package catalog determine which MCP servers and
related artifacts are rendered for a selected profile or workspace.

Planned work:

1. revalidate the historical `archive/catalog/packages.json` and
   `archive/mcp/mcp.json` snapshots against current client schemas;
2. restore catalog load/filter and provenance-aware managed-artifact behavior;
3. add profile-filtered MCP output only after the Phase 2 renderer contract is
   stable;
4. reintroduce import-local and remove-managed only with explicit ownership and
   conflict reporting;
5. extend Doctor for orphaned, conflicting, or unowned MCP entries.

Phase 3 must extend the Phase 2 renderer; it must not create a second unrelated
configuration system.

## Phase 4 — durable memory (pending cross-agent comparison)

**Goal:** Add human-approved durable project knowledge only when the current
Markdown policy surface is no longer sufficient.

Current gate status:

- [x] remove the public `archive/memory-vault/` prototype from the current
  branch;
- [x] remove the retired public-memory prose and keep the active private design
  in the parent workspace;
- [x] keep the private-memory design and implementation contracts in workspace
  workspace `docs/designs/memory/`;
- [x] review and merge the migration gate before implementation slices begin.

Later planned work:

- implement selected private-memory workflows with user approval before writes;
- keep memory local, reviewable, and separate from generated repository policy;
- keep obsidian-memory disabled until its source layout and provenance are
  explicitly designed.

Before implementation, compare the current design with viable cross-agent
memory systems identified by the workspace roadmap. Do not start with a vector
database or an always-on laptop service merely because a candidate bundles one.

## Phase 5 — optional Graphify integration ✅

**Goal:** Make Graphify an explicit, removable setup choice without confusing
CLI ownership, assistant integration, or Agentbot's canonical policy ownership.

### Ownership boundary

- Dotfiles owns installation and updates of the `graphifyy` Python package,
  exposed as the `graphify` CLI.
- Agentbot owns registration of the Graphify skill and its exposure to supported
  assistants.
- Graphify owns its generated `SKILL.md`, references, and
  `.graphify_version` stamp under the generic Agent Skills directory.
- Agentbot remains the only owner of its canonical `base/AGENTS.md` and
  `global/AGENTS.md` policy sources.

### Delivered behavior

1. Dotfiles provides a default-off `graphify_cli` component. Selecting it
   installs Graphify with `uv tool install graphifyy`; an existing non-uv
   Graphify installation is reported and preserved rather than silently
   replaced.
2. The existing `dotfiles update` flow updates only a uv-owned
   `graphifyy` tool with `uv tool upgrade graphifyy`. External installations
   are reported as unmanaged with a manual command instead of Dotfiles taking
   ownership.
3. Agentbot's main menu provides a read-only **Graphify Lib** item for assistant
   and shell forms, sourced from Graphify's official
   [common command reference](https://github.com/Graphify-Labs/graphify#common-commands).
   Keep `agentbot graphify status|setup` as direct inspection and repair
   commands rather than a separate TUI submenu.
4. Main Agentbot Install and successful Update run the skill-only command
   `graphify install --platform agents` whenever the CLI exists, then refresh
   Agentbot's existing assistant links and global outputs. Direct Graphify
   setup uses the same path. This makes the canonical skill available from
   `~/.agents/skills/graphify/` without allowing Graphify to edit a repository
   policy file.
5. A missing Graphify CLI is a non-failing skip. Setup failure is reported as a
   failed main flow, while `agentbot update --dry-run` previews setup, refresh,
   or skip without invoking Graphify.
6. The canonical base and global policies include short conditional Graphify
   guidance. Agents use Graphify only when the skill and a usable
   `graphify-out/graph.json` exist, and fall back to normal source inspection
   when they do not. The graph is scoped to the current project root; sibling
   repositories are not implicitly searched.

### Safety and non-goals

- Do not run bare `graphify install`; it defaults to one platform rather than
  the generic Agent Skills location.
- Do not run `graphify claude install`, `graphify agents install`,
  `graphify codex install`, `graphify cursor install`, or similar always-on
  setup commands because they can modify project instruction files or hooks.
- The official assistant forms are `/graphify <path>` for Claude/Cursor and
  `$graphify <path>` for Codex. The headless shell equivalents use an explicit
  subcommand such as `graphify extract .`, `graphify update .`, or
  `graphify query "..."`; do not document a bare shell `graphify .` as a
  general CLI build command.
- Do not scrape or dummy-install upstream `AGENTS.md` text into Agentbot
  templates. Agentbot owns a small reviewed policy block; version-specific
  operating detail stays in Graphify's installed skill.
- Do not automatically build graphs, install Git hooks, enable strict mode,
  purge generated data, stage files, commit, or push.
- Do not add Graphify to the curated `npx skills` manifest. Its skill is
  installed and versioned by the official Graphify CLI, not by an unrelated
  Git skill source.

Phase 5 was independent of the package-catalog and MCP work. Its completion did
not implement Phase 4; Slice 4M is complete, while the next implementation
slice waits for the parent workspace's cross-agent memory decision.

## Known costs, measured

`agentbot update` clones every enabled skill source twice: once so the plan can
report what would change, once to verify the apply against the revisions the
plan recorded.

| | Plan | Verify | Total |
|---|---|---|---|
| Before 2026-09-12 | 9.3s | 23.2s | 32.5s |
| After | 8.4–10.6s | 8.1–8.9s | 17.3–18.7s |

Twelve sources. The gap was not the second clone: it was that the plan's pass
ran through a worker pool and the apply's was a serial loop over the same
sources. Both go through one helper now.

What remains is one clone pass, which is the cost of the verification itself —
reading reality is how the apply checks the plan against it.

**Decided on 2026-09-12: it stays.** Carrying the plan's checkouts into the
apply would remove the second pass entirely, but that is a change of contract
rather than a speed fix — the apply would install exactly what was previewed
instead of aborting when upstream moved. The two failure modes are not
symmetric: aborting is loud and recoverable, while installing a revision that
moved during the operator's confirmation is silent. A product whose design is
preview-first with explicit mutation should fail loudly.

Reopen if the source count grows past roughly forty, `update` starts running
unattended in a loop, or the target machine is on a genuinely slow connection.
The full reasoning is workspace roadmap item 4.5.

## Explicitly out of scope

- automatic Git staging, commits, pushes, resets, or destructive cleanup;
- MCP configuration in Phase 2;
- arbitrary profile-selected output paths;
- token values or secrets in repository files or workspace state;
- obsolete product names, legacy TUI variables, and broad profile modes;
- Hermes, Proxmox, vector databases, or always-on laptop services.

## Deferred AgentOS capabilities

These ideas remain planning inputs, not runtime features:

- a full numbered policy taxonomy;
- an `agentos tui` overhaul and a source-ingest review pipeline;
- Hermes or opencode orchestration and home-server control planes;
- OpenClaw bootstrap files and additional ChatGPT/export adapters;
- Graphify hooks, Mem0, Graphiti, or GraphRAG memory upgrades.

Restore catalog or MCP work only through a new Phase 3 design. Do not restore
removed control-plane code directly into the live lifecycle.

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | Initial phase map created after the slim-bootstrap rework |
| 2026-07-18 | Reconciled Phases 0–1 with live Agentbot naming and entrypoints |
| 2026-07-18 | Marked Phase 2 complete after implementing profiles, renderer, local state, CLI, boot registration, menu, and validation |
| 2026-07-27 | Deferred Phases 3–4 and delivered Phase 5 as the independent Graphify integration phase |
| 2026-07-28 | Reconciled README, archive indexes, and Phase 4 design notes with the live Phase 5 boundary |
| 2026-07-28 | Selected Phase 4 as the next workstream and applied Slice 4M public-memory retirement in the working tree; review remains pending |
| 2026-08-06 | Confirmed Slice 4M merged, reconciled Graphify Lib and automatic Install/Update setup behavior, and selected Slice 4.0A as next |
| 2026-08-20 | Consolidated lifecycle, diagnostics, command metadata, TUI primitives, and validation before Phase 4 implementation |
| 2026-09-03 | Consolidated technical documentation and moved deferred AgentOS notes into the active roadmap |
| 2026-09-03 | Deferred Slice 4.0A until provider-neutral MCP and cross-agent memory evaluations are complete |
| 2026-09-11 | Workspace roadmap item 4.1 finished: install gained a component selector and execution plan, the Platform submenu folded into install and status, and both products now share one presentation contract |
| 2026-09-11 | Recorded the update transaction's double clone as a measured, open cost rather than leaving it in a commit message |
| 2026-09-12 | Halved the update's source-clone cost by running the apply's pass concurrently, as the plan's already was (32.5s to ~18s over twelve sources) |
| 2026-09-12 | Decided the apply keeps re-cloning to verify the plan; workspace roadmap item 4 is closed |
