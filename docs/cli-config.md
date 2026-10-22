# Agent CLI configuration

```bash
agentbot cli-config status   # preview; writes nothing
agentbot cli-config apply    # merge the declared keys
```

It is also the `cli-config` install component, and one row of `agentbot status`.
`agentbot update` also merges these declared keys after refreshing managed
workspaces and before its final diagnostics. Its transaction covers the Claude,
Codex, Cursor, and Antigravity config files if a later update stage fails.

Agentbot owns only the keys you declare, merged key by key into each CLI's own
config. Everything you have not declared is left exactly as it was.
The Claude declaration includes the current model, effort, display, and
notification preferences, plus its supported automatic-memory switch. Hooks,
permission rules, plugins and client state remain outside this declaration.
Codex owns only the declared keys, including `features.memories`.

## Verified paths

Reconfirmed against this machine at implementation time rather than taken from
archived research:

| CLI | Config | Format |
|---|---|---|
| Claude Code | `~/.claude/settings.json` | JSON (comments tolerated) |
| Codex | `~/.codex/config.toml` | TOML |
| Cursor CLI | `~/.cursor/cli-config.json` | JSON (comments tolerated) |
| Antigravity CLI | `~/.gemini/antigravity-cli/settings.json` | JSON |

Agentbot resolves `CODEX_HOME` and `CLAUDE_CONFIG_DIR` when set; otherwise the
defaults above apply. The resolved homes are shared by configuration, rendering,
MCP and skill-link consumers. Empty overrides use the defaults. A custom Codex
home does not move Cursor, Antigravity or the shared skills directory. Overrides
select existing client homes; they do not copy authentication or history.

## Declaring desired state

One file per CLI, in that CLI's own format:

```
cli/claude.settings.json
cli/codex.config.toml
cli/cursor.cli-config.json
cli/antigravity.settings.json
```

Authoring in each CLI's native format is deliberate. A single shared format
would have to be translated into each, and translation is where values quietly
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
model = "gpt-6.1-sol"
model_reasoning_effort = "medium"
```

A TOML desired file may declare one table level. Each key in it is merged on
its own, so the table's other keys stay; nested tables such as `[hooks.state]`
are refused. Codex's desired state uses this to turn off Codex's own memory
(`[features] memories = false`). This disables Codex's native memory feature
when it is the effective configuration; it does not delete generated memories
or session history. Profile overrides and live client behavior require their
own checks. [Codex memory controls](https://learn.chatgpt.com/docs/customization/memories).

Claude's declaration now owns `autoMemoryEnabled: false`. Applying it disables
Claude's automatic memory through its supported setting while preserving existing
memories, session history, hooks and unowned settings. Higher-precedence client
settings or environment overrides still need checking. This source change does
not apply itself to a running client. Do not claim all four clients' native
memories are disabled: the checked Cursor CLI and Antigravity documentation does
not establish equivalent controls. Fresh-session delivery remains a separate
acceptance check. The optional `agentbot memory serve` interface is documented
in the [memory commands](../README.md); it is not automatically registered or
trusted. [Claude auto memory](https://code.claude.com/docs/en/memory).

Claude's desired state also owns `skillOverrides`, which decides how each
skill is listed to Claude (see [skills](skills.md#claude-listing-states)).
Agentbot owns that whole object, so add personal overrides in
`cli/claude.settings.json` rather than in `~/.claude/settings.json`.

Codex's desired state sets `approvals_reviewer = "auto_review"`, a decision
recorded on 2026-10-13 after the setup review found it undocumented. When
Codex would stop to ask -- an escalated shell command, a blocked network
request, an edit outside the writable roots, an MCP tool approval -- it routes
the request to a separate reviewer agent instead of to the operator. It cannot
widen the sandbox: the sandbox and the allow-lists still bound what any
approval can grant. It is kept because Codex runs long unattended tasks here
(co-council, Remote Control), where a person is often not watching the prompt.
Set it to `"user"` to have Codex ask the operator again. Source: OpenAI's
auto-review page, `learn.chatgpt.com/docs/sandboxing/auto-review`.

Antigravity's desired state holds `agentMode: accept-edits`,
`artifactReviewPolicy: agent-decides`, verbosity, notifications, the
feedback survey, `enableTelemetry: false` (on by default in `agy`), and
`toolPermission: request-review` (keys from the official settings page,
`antigravity.google/docs/settings?tab=cli`; `agy` writes only values that
differ from its defaults). Edits and plans are reviewable after the fact, so
those two are shared. `toolPermission` decides whether `agy` runs write, shell
and web tools without asking; `request-review` asks first, as the other three
clients do. It was per machine until the 2026-10-13 setup review found
`always-proceed` on the home PC, and the operator chose to declare the asking
value everywhere (declaring it grants nothing). `trustedWorkspaces` stays
per machine too, because it lists local paths. The checked Antigravity CLI
settings documentation does not establish a native-memory disable switch.
Treat that control as unverified; instruction policy alone does not enforce
exclusive memory use. [Antigravity settings](https://www.antigravity.google/docs/settings).

**Checked 2026-10-12** with `agentbot setup-review --scope cli-config`, then by
hand. Two keys were dropped: Codex `service_tier = "default"` (no model
advertises a `default` tier; unset already means the standard tier) and
Antigravity `model` (not a documented settings key; choose the model with
`/model` or `--model`). Cursor's `modelSlashCommands` and `display.mode` are
missing from Cursor's documentation but are live settings in the CLI's own
settings schema (2026.10.01), so they stay.

**Effort on Opus 5.5 (checked 2026-10-12).** In `~/.claude/settings.json`,
the top-level `effortLevel` is the older form: Opus 5.5 and later models ignore
it and start at their own default, so `model: opus` was not getting the
declared `medium`. The desired state now also declares `modelSettings` with
`medium` for `claude-opus-5-5`, `claude-sonnet-5` and `claude-sonnet-5-5` (keys are full model IDs, so each Sonnet release needs its own); the top-level key stays
for older models. Agentbot owns `modelSettings` as a whole, so a level saved
with `/effort` lasts until the next update; change it here instead. Two other
suggestions from the same review were not taken: Cursor's `version` is
Cursor's own schema number (declaring it would write `1` back after Cursor
moves on), and Codex's `[memories]` keys only matter when
`features.memories`, already declared `false`, is on.

## Remote Control

Claude and Codex have separate Remote Control behavior.

- **Claude Code:** `cli/claude.settings.json` declares
  `remoteControlAtStartup: true`, so interactive sessions request Remote Control.
- **Codex:** its daemon owns
  `~/.codex/app-server-daemon/settings.json`. Normal Agentbot install, update and
  `cli-config apply` preserve the saved on/off choice. Missing/disabled settings
  are skipped without invoking Codex. Enable Remote Control yourself through
  `codex remote-control start` when you want it.

For an already-enabled Codex host, Agentbot checks `codex login status` first.
Only a successful status identifying ChatGPT login allows a daemon start;
signed-out, API-key-only, timed-out and unrecognized login results are skipped.
No login output or credential is printed. This check does not establish account
entitlements or relay connectivity: an enabled, authenticated host can still
report a connection failure that needs attention.

`cli-config status` inspects without starting the daemon. The lifecycle update
also maintains already-enabled, authenticated remote control after its Doctor
gate. No command here logs in, pairs a device, disables an existing setting, or
stops a running daemon. Settings saved by older auto-enable behavior are
preserved too; an enabled flag alone does not prove the user opted in.

Pairing a phone to Codex is a one-time step you do yourself:
`codex remote-control pair`. The pairing code is a secret. Do not log it,
paste it anywhere or commit it.

## Guarantees

- **Unowned keys survive.** Merging is per key; nothing else in the file is
  touched. Comments survive in JSON (the merge edits text rather than
  re-serialising) and in TOML (only the lines of owned keys are rewritten: above the
  first table header for top-level keys, inside the table for a table's keys).
- **The whole run rolls back.** If one target fails, the targets already
  written are restored. A half-applied run across four agents is worse than
  none: you would have to work out which of them now disagree with the
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

`agentbot setup-review --scope cli-config` automates that check: an agent CLI
reads only `cli/`, compares each declared key against the CLI's current
official documentation, and reports renamed, removed, or deprecated keys. It
proposes; it does not write. See [Setup review](setup-review.md).

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

Installing the CLIs (Dotfiles owns that), MCP server lists (`agentbot mcp`),
VS Code settings (`docs/vscode.md`), and the Cursor statusline
(`docs/cursor-statusline.md`), which has its own ownership contract.
