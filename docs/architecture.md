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
  token scope, and dispatch into the Python CLI. The repository gate is the
  first thing `install` does; `AGENTBOT_INSTALL_GATE_ONLY=1` stops it right
  after, which is how the component selector asks about the repository before
  anything is selected rather than after.
- `bin/agentbot` resolves `AGENTBOT_HOME` and provides the installed launcher.
- `src/commands.py` is the command metadata authority used by help and the TUI.
- `src/cli.py` parses commands, composes services, and owns exit policy.
- `src/lifecycle.py` coordinates install, update, workspace, and resync flows.
  `SELECTABLE_COMPONENTS` names the six parts of an install a selector may
  narrow; `PLATFORM_COMPONENTS` is the editor and CLI subset of them.
- `src/install_plan.py` builds the plan shown between the selector and the run.
  It reads local state only -- a plan that reached the network would make the
  screen before the install as slow as the install.
- `src/platform_surfaces.py` reduces VS Code, the Cursor statusline, and CLI
  configuration to one status row each. The same rows answer `status` and the
  install summary, which is what folding the former Platform submenu into those
  two surfaces means in practice.
- `src/ui/install_log.py` owns the `[STEP]`/`[OK]` progress vocabulary and the
  timing block. Markers are coloured where they are printed rather than by a
  relay in the menu, so they look the same from a terminal, through a pipe, and
  inside `dotfiles full-update`.
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
  workspaces, Graphify Lib -- and builds the derived ones: the Command
  Lib lists whatever `src/commands.py` declares, so a new command appears in the
  menu with nothing else edited, and the Graphify reference menus come from
  `src/ui/graphify_lib.py`, which is where those commands are written down. `agentbot_menu_run <name>` runs one by name
  with nothing built in Bash. Menus whose entries are computed at
  runtime still fill `MENU_SIMPLE_*` and call `agentbot_menu_run` with no name.
  Dispatch stays in Bash either way, and `tests/test_menu_definitions.sh` holds
  the halves together: every key has a branch and every branch has a key.

  `src/ui/checkbox.py` is the second menu shape -- a list where every row is on
  or off, with a status column and paging -- and `agentbot_checkbox_run` runs it
  the way `agentbot_menu_run` runs the simple one, through the same MENU_CB_*
  globals `menu_checkbox_run` uses. Install's component selector and Prune
  Skills both go through it.

  The Bash loop still answers in two cases: no `python3`, and a caller using the
  `MENU_CB_TOGGLE_FN` hooks, which are Bash functions a loop in another process
  cannot call. Neither Agentbot checkbox uses them; the Dotfiles component
  selector does.

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
