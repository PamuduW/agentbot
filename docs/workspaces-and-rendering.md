# Workspaces and rendering

## Policy sources

`base/AGENTS.md` is the canonical repository scaffold. `base/CLAUDE.md` is its
Claude compatibility template. `global/AGENTS.md` is the sole authored
machine-wide baseline.

Agentbot-generated repository policy is delimited by:

```text
<!-- BEGIN AGENTBOT MANAGED BASELINE -->
...
<!-- END AGENTBOT MANAGED BASELINE -->
```

Resync replaces only that managed block and preserves project-owned content.
An existing unmarked `AGENTS.md` whose prefix differs from the template is
treated as custom and left untouched.

## Workspace surfaces

Every managed workspace has `AGENTS.md`. Claude and Cursor are optional
compatibility targets selected by the workspace profile. Codex and `agents`
refer to the canonical `AGENTS.md`; Agentbot does not create a separate Codex
policy file. Cursor receives a pointer rule instead of a duplicate policy copy.

`agentbot boot PATH` previews or applies one workspace render and registers a
successful apply. `agentbot resync` refreshes registered workspaces. Both flows
preview unless `--yes` authorizes writes. Removing a registry record never
deletes workspace files.

When a schema 3 memory vault is configured and the target is a Git
repository, a successful `boot` also registers it for project memory, the same
as `agentbot memory project register`, and syncs that registration. The report
gains a `Project memory` row: `applied` for a new project, `ok` when the
repository already has one, `skipped` without a vault, on an older schema, or
outside a repository, and `conflict` on a registry collision. Memory never
changes boot's exit code. `--no-memory` skips the step.

Relative paths -- including `boot`'s current-directory default -- resolve
against the directory the command was invoked from, not the Agentbot checkout.
`boot` renders the active profile's `default_targets` unless a selector flag
overrides them.

## Global outputs

Global Codex and Claude policy adapters, Claude skill links, and the managed
Claude statusline are rendered from canonical sources. Use install, update, or
the relevant resync/setup flow to reconcile them.
