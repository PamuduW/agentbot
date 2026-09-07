# Agent CLI configuration

```bash
agentbot cli-config status   # preview; writes nothing
agentbot cli-config apply    # merge the declared keys
```

Agentbot owns only the keys you declare, merged key by key into each CLI's own
config. Everything you have not declared is left exactly as it was.

## Verified paths

Reconfirmed against this machine at implementation time rather than taken from
archived research:

| CLI | Config | Format |
|---|---|---|
| Claude Code | `~/.claude/settings.json` | JSON (comments tolerated) |
| Codex | `~/.codex/config.toml` | TOML |
| Cursor CLI | `~/.cursor/cli-config.json` | JSON (comments tolerated) |

## Declaring desired state

One file per CLI, in that CLI's own format:

```
cli/claude.settings.json
cli/codex.config.toml
cli/cursor.cli-config.json
```

Authoring in each CLI's native format is deliberate. A single shared format
would have to be translated into three, and translation is where values quietly
change shape — the same reason `vscode/` holds JSON rather than YAML.

```jsonc
// cli/claude.settings.json
{
  "model": "opus",
  "theme": "dark"
}
```

```toml
# cli/codex.config.toml
model = "gpt-5"
model_reasoning_effort = "high"
```

## Guarantees

- **Unowned keys survive.** Merging is per key; nothing else in the file is
  touched. Comments survive in JSON (the merge edits text rather than
  re-serialising) and in TOML (only the region above the first table header is
  rewritten).
- **The whole run rolls back.** If one target fails, the targets already
  written are restored. A half-applied run across three agents is worse than
  none: you would have to work out which two of three now disagree with the
  desired state.
- **Every write is verified first.** The merged text is re-parsed and the owned
  keys checked before it replaces anything. The input parsed a moment earlier,
  so an unparseable result is Agentbot's fault, not yours, and it says so.
- **Originals are backed up** to `<name>.agentbot-backup` immediately before
  the replace, and the write itself is atomic.

## What it refuses

- **Credential-shaped keys.** A key matching `token`, `secret`, `password`,
  `apikey`, or `credential` with a literal string value is refused outright.
  These files are committed, so a value here would be a secret in version
  control. Name an environment variable in an unambiguous key instead.
- **TOML tables.** Merging a `[hooks]` or `[tui]` block means rewriting a whole
  section without disturbing the rest of it, which this does not attempt yet. A
  declared table is reported, never half-written.
- **Unreadable configs.** A file that cannot be parsed is reported and left
  alone.
- **Missing CLIs.** No config directory means the CLI is not installed here,
  which is a reported skip rather than a failure.

## Not covered

Installing the CLIs (Dotfiles owns that), MCP server lists (roadmap item 5.1),
VS Code settings (`docs/vscode.md`), and the Cursor statusline
(`docs/cursor-statusline.md`), which has its own ownership contract.
