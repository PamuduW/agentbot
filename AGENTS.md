# Agentbot project overlay

Repository-specific instructions for Agentbot development. The parent
`new_setup/AGENTS.md` supplies the general engineering and safety policy when
this submodule is developed from the workspace root.

## Project

Agentbot installs curated Agent Skills, renders global agent policy, and
manages per-workspace policy surfaces through `boot`, `workspace`, and
`resync`. Keep it a small Python lifecycle with thin Bash entrypoints rather
than rebuilding the archived configuration plane.

**Stack:** Bash (`install.sh`, `bin/agentbot`), Python 3 (`src/`), and the
Skills CLI through `npx`.

| Path | Role |
|---|---|
| `install.sh` | Supported bootstrap, repository gate, token scope, and CLI adapter |
| `bin/agentbot` | Installed public launcher |
| `src/` | CLI, lifecycle, reconciliation, rendering, diagnostics, and workspace services |
| `skills.sources.yaml` | Canonical curated-source manifest; global pins live in `~/.agents/.skill-lock.json` |
| `base/` | Product templates rendered into managed repositories; not development policy |
| `global/` | Authored machine-policy and Claude statusline sources |
| `agentos.yaml` | Safe-default workspace profile and output allowlist |
| `docs/` | Current technical documentation and product roadmap |
| `tests/` | Python and shell regression suites |

## Commands

```bash
./install.sh install
./install.sh update --dry-run
./install.sh skills install
./install.sh doctor
env -u NO_COLOR bash tests/run.sh
```

Use `python3 -m unittest discover -s tests` or a suite under `tests/shell/` for
focused checks. Run the complete gate before handoff.

## Repository rules

- Keep product behavior in the Python CLI. Bash owns bootstrap, repository
  self-update, terminal presentation, secret scoping, and process adapters.
- `base/AGENTS.md`, `base/CLAUDE.md`, `global/AGENTS.md`, and
  `global/claude/statusline-command.sh` are canonical product sources. Do not
  confuse them with this development overlay or edit their generated outputs.
- Preserve the managed-block contract: resync may replace only the marked
  Agentbot prefix and must preserve project-owned content.
- Use `skills.sources.yaml` and the global Skills CLI flow; do not vendor
  upstream skills or copy the global lock into `skills-lock.json`.
- Dotfiles owns Graphify and Boost executables. Agentbot may configure their
  supported agent integrations but must not install, update, or silently widen
  either product's scope.
- Keep workspace mutation preview-first. Removing a workspace registration
  must never delete workspace files.
- Archived MCP JSON and catalog data are research inputs. No active command may
  load them until a new, tested ownership design is implemented.
- Do not restore retired commands or the old catalog control plane without an
  approved design and migration plan.
