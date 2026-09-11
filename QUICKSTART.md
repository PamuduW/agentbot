# Quickstart

See **[README.md](README.md)** for the full slim bootstrap guide.

**TL;DR:**

**Sibling of dotfiles (recommended):**

```bash
git clone <your-remote>/agentbot ~/agentbot   # next to ~/dotfiles
cd ~/agentbot
./install.sh install
```

`dotfiles full-update` resolves Agentbot as the sibling of the Dotfiles
checkout, so keep the two directories under the same parent.

**Standalone anywhere:**

```bash
git clone <your-remote>/agentbot /any/path/agentbot
cd /any/path/agentbot
./install.sh install
```

Then in any project folder or project repo: `agentbot boot`. It creates or
preserves `AGENTS.md`, renders the compatibility surfaces the active profile
lists in `default_targets`, and records the canonical folder or Git root in
private local state. A selector flag overrides the profile for that run.

Useful explicit selections:

```bash
agentbot boot --agents
agentbot boot --agents --claude
agentbot boot --agents --cursor
agentbot boot --agents --claude --cursor
agentbot boot --profile safe-default
```

Preview or refresh a registered workspace:

```bash
agentbot workspace ~/Dev/existing-project
agentbot workspace --yes ~/Dev/existing-project
agentbot workspaces
agentbot resync --dry-run --all
agentbot resync --yes --all
```

`AGENTS.md` is always present because it is the canonical repository policy.
The local registry is `${XDG_CONFIG_HOME:-$HOME/.config}/agentbot/workspaces.json`;
it is not written into the project or tracked by Git.

On a controlling TTY, `./install.sh` or `agentbot` with no arguments opens the
Agentbot menu. Headless runs should use an explicit command such as
`agentbot status` or `./install.sh doctor`.

`./install.sh install` installs every component. To choose instead, use
`agentbot install --menu` for the selector and execution plan, or name a subset
with `agentbot install --components skills,boost`.

Update skills later: `./install.sh skills update`.

Active and deferred phases: [`docs/roadmap.md`](docs/roadmap.md). Historical
MCP research inputs: [`archive/docs/README.md`](archive/docs/README.md).
