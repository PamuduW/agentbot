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

## What happens when a CLI updates

Fields are merged, never files. A CLI that ships new settings in an update
keeps them: only the keys you declared are touched, and everything else in the
file is left byte-identical.

```
keys before : anotherNewOne, brandNewSettingFromV3, model, theme
keys after  : anotherNewOne, brandNewSettingFromV3, model, theme
we changed  : model: "sonnet" -> "opus"
we destroyed: nothing
```

If an update leaves a config Agentbot cannot parse, it is reported and never
written.

**The risk worth knowing about is a renamed setting.** If a CLI renames a key
you declare — `model` becoming `defaultModel`, say — Agentbot keeps writing the
old key. The CLI ignores it and falls back to its default, and `status` still
reports "current", because the declared state does match the file. That failure
is silent, and detecting it would require knowing each CLI's schema, which
Agentbot does not.

The same applies to a value that stops being valid.

Two things keep this manageable:

- **Declare only what you care about.** The blast radius is exactly the set of
  keys in `cli/`. A short list is a small risk.
- **Suspect the declaration after an upgrade.** If an agent starts ignoring a
  setting you thought you controlled, check `cli/*` against that CLI's current
  documentation. The previous file is always at `<name>.agentbot-backup`.

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
