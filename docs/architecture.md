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
- `src/ui/menu.py` draws menu frames and `src/ui/menu_select.py` runs the
  selection loop. ADR-0001 in the workspace repository moves presentation to
  Python; the Dotfiles menu stack cannot follow, because it draws before that
  machine has an interpreter, but this repository is not installed until
  `python3` and PyYAML exist, so its menus can.

  `agentbot_menu_run` in `scripts/lib/tui.sh` is the seam: it describes the menu
  as `name\x1fvalue` lines, Python draws and reads it, and the chosen key comes
  back. One process per menu display, not one per keystroke. Dispatch stays in
  Bash. Shared code that runs a menu takes it from `MENU_SIMPLE_RUNNER`, which
  defaults to the Bash `menu_simple_run` so `scripts/lib/shared/tui/` stays
  language-neutral and Dotfiles keeps its Bash loop.

  Without `python3`, or if the Python side fails (exit 3, as opposed to exit 1
  for a cancelled menu), the shared Bash loop answers instead.

  `src/ui/menus.py` holds the menus whose entries are fixed -- main, libraries,
  platform, workspaces, Graphify Lib -- and builds the derived ones: the Command
  Lib lists whatever `src/commands.py` declares, so a new command appears in the
  menu with nothing else edited, and the Graphify reference menus come from
  `src/ui/graphify_lib.py`, which is where those commands are written down. `agentbot_menu_run <name>` runs one by name
  with nothing built in Bash. Menus whose entries are computed at
  runtime still fill `MENU_SIMPLE_*` and call `agentbot_menu_run` with no name.
  Dispatch stays in Bash either way, and `tests/test_menu_definitions.sh` holds
  the halves together: every key has a branch and every branch has a key.

  `src/ui/checkbox.py` is the second menu shape -- a list where every row is on
  or off, with a status column and paging. Frames only so far; the loop that
  reads keys and toggles is still `scripts/lib/shared/tui/menu_checkbox.sh`.

  Three suites hold it: `tests/test_menu_parity.sh` compares frames byte for
  byte at every cursor position, width and palette and compares the key decoder
  against `menu_read_key`; `tests/test_menu_select.py` drives real sessions
  through a pty; `tests/test_menu_runner.sh` covers the handover, including the
  no-`python3` fallback for a caller-built menu. A *named* menu has no such
  fallback -- its definition lives on the Python side, and every action behind
  it runs the Python CLI, so a machine that cannot run one cannot run the
  other.

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
