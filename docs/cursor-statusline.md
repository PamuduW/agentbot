# Cursor statusline

```bash
agentbot cursor status       # read-only
agentbot cursor statusline   # install the managed statusline
```

It is also the `cursor` install component, and one row of `agentbot status`.

## Investigation outcome

Roadmap 4.3 opened with a gate: confirm from official sources that Cursor can
run a managed statusline command, honour a documented width signal, and
preserve unrelated settings — and reject the work if it cannot.

**Cursor CLI: accepted.** Evidence, in order of strength:

1. Cursor ships its own documentation for the feature, in the `statusline`
   skill it installs at `~/.cursor/skills-cursor/statusline/SKILL.md`. It
   documents the config block, the full stdin payload schema, and states the
   spec is aligned with Claude Code's status line.
2. The installed CLI binary contains `statusLine`, `render_width_chars`, and
   `updateIntervalMs` (verified against `cursor-agent` 2026.09.02-c22c1a3).
3. Cursor's changelog and community documentation describe the `/statusline`
   command and the `cli-config.json` configuration.

All three gate conditions hold: a command is spawned per conversation update
(`type: "command"`), width is reported as `render_width_chars` in the payload,
and the block lives beside unrelated keys in a JSON object that can be merged.

**Cursor IDE: rejected.** Cursor documents no way to point the editor's status
bar at a managed command. That surface is VS Code's, reachable only from an
extension, and there is no documented width or layout signal for it. Claiming
IDE coverage would be claiming something untested, so the IDE is left alone.

## It reads like the Claude statusline

Same palette, same separator, same segment order, same wording:

```
 Claude Opus 5 · ~/Dev/new_setup · main * · Context 34% used     (Claude)
 Composer 1    · ~/Dev/new_setup · main * · Context 34% used     (Cursor)
```

Cursor adds two segments Claude has no equivalent for, and only when the
payload carries them: `wt:<name>` in a worktree, and the vim mode when vim mode
is on. A test pins the shared layout so it cannot drift apart.

## Why this is not the Claude statusline pointed at a second file

The contracts are aligned but not identical, and each difference is a silent
failure if ignored:

| | Claude Code | Cursor CLI |
|---|---|---|
| Width | `COLUMNS` environment variable | `render_width_chars` in the payload |
| Rate limits | `rate_limits.*` segments | no such fields |
| Editor state | — | `vim.mode`, `worktree.name` |

`COLUMNS` is not set for the Cursor command, so reusing the Claude script would
size the line against a default guess rather than the real terminal.

## What Agentbot owns

It writes exactly two things:

- `~/.cursor/statusline-command.sh`, copied from `global/cursor/statusline-command.sh`
- the `statusLine` key in `~/.cursor/cli-config.json`

Every other key in `cli-config.json` — model, permissions, editor, display — is
left untouched.

A `statusLine` already pointing somewhere else is reported as `unowned` and
**never replaced**. Doctor reports it as a warning rather than an error: you
chose it, and Agentbot's job is to say so, not to object.

An unparseable `cli-config.json` is reported and never overwritten.

## Payload shapes handled

Cursor documents fields that are absent or null in normal use, and the script
is tested against each:

- `model.display_name` containing a space (`"Composer 1"`)
- `vim` and `worktree` absent, which is the common case
- `context_window.used_percentage` null before the first API call
- an empty payload
- a narrow `render_width_chars`, which truncates with an ellipsis
