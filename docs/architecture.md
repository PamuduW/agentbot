# Architecture

Agentbot has one Python lifecycle backend with thin Bash entrypoints.

```text
install.sh / bin/agentbot
        |
        v
src/cli.py ---- src/commands.py
        |
        v
src/lifecycle.py
   |       |        |
 skills  render   workspace services
   |       |        |
   +---- diagnostics + reports
```

## Main boundaries

- `install.sh` handles bootstrap prerequisites, repository maintenance, private
  token scope, and dispatch into the Python CLI.
- `bin/agentbot` resolves `AGENTBOT_HOME` and provides the installed launcher.
- `src/commands.py` is the command metadata authority used by help and the TUI.
- `src/cli.py` parses commands, composes services, and owns exit policy.
- `src/lifecycle.py` coordinates install, update, workspace, and resync flows.
- `src/diagnostics.py` produces the shared Status and Doctor snapshot.
- `scripts/lib/tui.sh` and `scripts/menus/` are presentation adapters.
- `src/ui/menu.py` draws menu frames. ADR-0001 in the workspace repository moves
  presentation to Python; the Dotfiles menu stack cannot follow, because it
  draws before that machine has an interpreter, but this repository is not
  installed until `python3` and PyYAML exist, so its menus can. Frames are in
  Python; reading keys and looping is still
  `scripts/lib/shared/tui/menu_simple.sh`. `tests/test_menu_parity.sh` compares
  the two byte for byte at every cursor position, width and palette, which is
  the same oracle that made the table migration verifiable.

## Authored and generated data

Authored sources include `skills.sources.yaml`, `base/AGENTS.md`,
`base/CLAUDE.md`, `global/AGENTS.md`, and `agentos.yaml`. Global assistant
files, workspace compatibility files, skill links, and the Claude statusline
are derived outputs. Edit an authored source and use the documented Agentbot
flow to refresh its outputs.

Local mutable state is outside the checkout under
`${XDG_CONFIG_HOME:-$HOME/.config}/agentbot` and `~/.agents/`. The MCP snapshots
under `archive/` are research inputs only.

## Cross-repository ownership

Dotfiles installs and updates the Graphify and Boost executables. Agentbot
configures their supported assistant surfaces. This prevents an integration
refresh from replacing an executable and a binary update from rewriting
Agentbot-managed policy.
