# Agentbot technical documentation

This directory documents the current Agentbot implementation. The root README
is the operator entrypoint; these pages describe ownership, lifecycle, and
maintenance contracts.

Canonical policy templates live in `base/` and `global/`, the skill-source
manifest is `skills.sources.yaml`, and private workspace state lives under
`${XDG_CONFIG_HOME:-$HOME/.config}/agentbot`.

| Document | Purpose |
|---|---|
| [Architecture](architecture.md) | Runtime boundaries, code layout, design decisions and scope |
| [Skills and integrations](skills.md) | Source manifest, locks, reconciliation, Graphify, and Boost |
| [Workspaces and rendering](workspaces-and-rendering.md) | Policy ownership, generated surfaces, and registration |
| [Lifecycle and updates](lifecycle-and-updates.md) | Install, update, recovery, and transaction behavior |
| [VS Code](vscode.md) | Selected extensions and owned settings, per host |
| [Cursor statusline](cursor-statusline.md) | The managed Cursor CLI statusline |
| [Agent CLI configuration](cli-config.md) | Declared Claude, Codex, Cursor, and Antigravity config keys |
| [Atlassian read MCP](atlassian-read.md) | Scoped Jira and Confluence tokens, fixed read tools, and setup |
| [Setup review](setup-review.md) | The recurring agent-run review, filed in Dotfiles' `reviews/` |
| [Validation](validation.md) | Complete and focused validation commands |

How every surface in this repository is laid out — headings, tables, colour,
rollups, prompts — is one contract shared with Dotfiles:
[`docs/presentation.md`](https://github.com/PamuduW/dotfiles-shared/blob/main/docs/presentation.md)
in dotfiles-shared.
