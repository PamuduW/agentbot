# Agentbot

Agentbot is a local bootstrap CLI for agent policy, curated Agent Skills, and
registered workspace outputs. It keeps policy in canonical Markdown, renders
provider-specific compatibility files, and makes mutating operations explicit.

Use `./install.sh` from a checkout or `agentbot` after installation. Run
`agentbot help` for the command index and `agentbot help COMMAND` for details.

## Requirements

- Bash, Git, Python 3, PyYAML, and tomlkit
- Node.js, npm, and `npx` for managed skill sources
- optional `graphify` and `boost` CLIs installed by Dotfiles

If PyYAML is unavailable, install the repository requirements in a local
environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Quick start

```bash
git clone <your-remote>/agentbot ~/agentbot
cd ~/agentbot
./install.sh install
```

Install validates the checkout, installs enabled skill sources, refreshes
optional Graphify and Boost integrations when their CLIs exist, reconciles the
VS Code, Cursor-statusline and CLI-config surfaces, renders global outputs, runs
Doctor, and links `bin/agentbot` to `~/bin/agentbot`. Ensure `~/bin` is on
`PATH`.

Those six parts are selectable. `agentbot install --menu` opens the component
selector and an execution plan first; `--components` names a subset directly.
Managed outputs, Doctor, and the launcher link always run. See
[Lifecycle and updates](docs/lifecycle-and-updates.md).

The repository is not copied. The installed launcher remains linked to the
checkout that ran install. Confirm the active checkout before maintenance:

```bash
command -v agentbot
readlink -f "$(command -v agentbot)"
```

Private state is stored under
`${XDG_CONFIG_HOME:-$HOME/.config}/agentbot`, not in the repository.

## Common workflows

```bash
agentbot                          # open the interactive menu
agentbot status                   # inspect managed state
agentbot doctor                   # validate skills, links, and outputs
agentbot install --menu           # choose components, review the plan, install
agentbot install --components L   # install a named subset, unattended
agentbot update --dry-run         # preview repository and lifecycle changes
agentbot update                   # confirm and apply an update
agentbot full                     # install, then update
agentbot boot /path/to/repo       # render and register a workspace
agentbot workspaces               # list registered workspaces
agentbot resync --dry-run --all   # preview every registered workspace
agentbot token                    # manage the optional private GitHub token
agentbot mcp catalog              # list validated MCP candidates
agentbot mcp status               # inspect MCP ownership without network access
```

The editor and CLI surfaces are part of an install and appear in `status`. They
are also directly addressable:

```bash
agentbot vscode status|seed|apply     # extensions and owned settings, per host
agentbot cursor status|statusline     # the managed Cursor CLI statusline
agentbot cli-config status|apply      # declared Claude, Codex, Cursor keys
```

The menu and direct CLI share the same command model and lifecycle code. Token
input is masked, and normal output shows only a fingerprint.

## MCP management

Gate 5.1A provides a provider-neutral, default-off MCP control plane for Claude
Code, Codex CLI, and Cursor CLI. The catalog can contain reviewed candidates,
but Agentbot currently selects, owns, and writes no MCP server. Each candidate
must pass its admission gate before `setup` can configure it.

```bash
agentbot mcp catalog
agentbot mcp status
agentbot mcp plan --select ID... --targets claude codex cursor
agentbot mcp setup --select ID... --targets CLIENT... --yes
agentbot mcp off --select ID... --targets CLIENT... --yes
agentbot mcp restore OPERATION_ID --yes
```

`catalog`, `status`, and `plan` are read-only; default status never contacts a
provider. Mutations require a non-empty server and client selection plus
`--yes`. Agentbot refuses malformed configuration, symlinks, unmanaged
same-name entries, and changes to an entry it previously rendered.

Ownership records live in private mode-`0600` state under
`${XDG_CONFIG_HOME:-$HOME/.config}/agentbot/mcp.json`. Each operation snapshots
all selected client files and state under a mode-`0700`
`backups/mcp/OPERATION_ID/` directory before writing. A failed multi-client
operation restores every original file; deliberate restore refuses if a
destination changed afterward.

The catalog and state store environment-variable names only. Claude, Codex,
and Cursor receive their native reference forms; Agentbot never writes a token
value, token-bearing URL, command argument, or MCP `.env` file. `off` removes
only unchanged Agentbot-owned entries and does not revoke client-owned OAuth or
delete environment variables.

### GitHub admission status

The official GitHub remote is present as an ineligible candidate. Its frozen
contract uses the `/readonly` endpoint, exact individual tools, and Codex's
matching `enabled_tools`. Agentbot will not configure it until the live remote
and all three clients pass the admission suite.

For that gate, create a repository-limited fine-grained personal access token
with only Metadata, Contents, Issues, Pull requests, and Actions set to `Read`.
Expose it to the test process as `GITHUB_MCP_TOKEN`; do not put the value in an
Agentbot file or command argument. The opt-in test remains a clean skip unless
both variables are present:

```bash
AGENTBOT_TEST_GITHUB_MCP=1 \
  GITHUB_MCP_TOKEN="$GITHUB_MCP_TOKEN" \
  python3 -m unittest tests.integration.test_mcp_github_live -v
```

The test records no token, header, repository content, issue text, comment, or
job log. Until it passes, `agentbot mcp plan --select github ...` fails closed
because the candidate is not eligible.

## Skills

[`skills.sources.yaml`](skills.sources.yaml) is the canonical source manifest.
Global installs and pins live under `~/.agents/`; the committed
[`skills-lock.json`](skills-lock.json) is a project-level stub, not a copy of
the global lock.

```bash
./install.sh skills install
./install.sh skills update
./install.sh skills list
./install.sh skills doctor
./install.sh skills prune             # preview source reconciliation
./install.sh skills prune --yes       # remove selected managed candidates
./install.sh skills remove-manual     # preview user-placed skills
```

Graphify is installed through its own CLI and is not a curated Git source.
Dotfiles owns the Graphify and Boost binaries; Agentbot owns their assistant
integration. See [Skills and integrations](docs/skills.md) for source syntax,
lock ownership, pruning rules, and integration policy.

## Workspaces

`base/AGENTS.md` is the canonical project scaffold. Agentbot always maintains
an `AGENTS.md` managed block and can render Claude and Cursor compatibility
surfaces. Project-owned content outside the marked block is preserved, and an
unmarked custom `AGENTS.md` is not overwritten.

A successful apply registers the canonical workspace path in private local
state. Workspace and resync operations preview by default; `--yes` authorizes
writes. Removing a registry entry never deletes workspace files.

`global/AGENTS.md` is the authored machine baseline. Generated global Codex and
Claude files must be refreshed through Agentbot rather than edited directly.
See [Workspaces and rendering](docs/workspaces-and-rendering.md).

## Documentation

- [Technical documentation index](docs/README.md)
- [Architecture](docs/architecture.md)
- [Skills and integrations](docs/skills.md)
- [Workspaces and rendering](docs/workspaces-and-rendering.md)
- [Lifecycle and updates](docs/lifecycle-and-updates.md)
- [VS Code](docs/vscode.md)
- [Cursor statusline](docs/cursor-statusline.md)
- [Agent CLI configuration](docs/cli-config.md)
- [Validation](docs/validation.md)
- [Roadmap](docs/roadmap.md)
- [Archived MCP research inputs](archive/docs/README.md)
- [Quick start](QUICKSTART.md)

## Development

Python owns lifecycle behavior. Bash is limited to bootstrap, repository
self-update, terminal presentation, secret scoping, and process adapters.
Change canonical sources rather than rendered outputs, and keep Dotfiles-owned
binary installation separate from Agentbot-owned integration.

Run the complete local gate before handing off a change:

```bash
env -u NO_COLOR bash tests/run.sh
```

For Ruff and coverage checks matching CI, put the development tools in a local
virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
PATH="$PWD/.venv/bin:$PATH" env -u NO_COLOR bash tests/run.sh
```
