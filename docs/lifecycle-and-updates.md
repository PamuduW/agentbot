# Lifecycle and updates

## Install

Install validates the repository, installs enabled curated skills, refreshes
Graphify and Boost when their Dotfiles-owned CLIs exist, reconciles the editor
and CLI surfaces, renders managed global outputs, runs Doctor, and creates the
user launcher link. Installation does not copy the checkout.

### Selecting components

Six parts of an install are optional and can be narrowed:

| Key | Covers |
|---|---|
| `skills` | the sources named in `skills.sources.yaml` |
| `graphify` | the Graphify Agent Skills integration |
| `boost` | Boost for Claude, Codex, and Cursor |
| `vscode` | selected extensions and owned settings, per host |
| `cursor` | the managed Cursor CLI statusline |
| `cli-config` | the declared Claude, Codex, and Cursor configuration keys |

```bash
agentbot install                                  # every component
agentbot install --components skills,boost        # a subset
agentbot install --menu                           # choose in a selector first
```

Managed outputs, Doctor, and the launcher link are not selectable: they are the
baseline every install leaves behind, and an install that skipped them would
report on a state it had not established.

`--menu` opens the three screens the menu's **Install Agentbot** entry drives —
the component selector, the execution plan, then the run. It needs a controlling
terminal. The Dotfiles bootstrap asks for it by name so its install step gets
that sequence rather than the whole Agentbot menu; a checkout whose launcher
predates the flag falls back to a plain `install`.

### Order of the screens

The repository gate runs **before** the selector, not inside the install that
follows it: a pull moves the checkout and restarts the run, which would throw a
selection away. `AGENTBOT_INSTALL_GATE_ONLY=1` is the seam — it stops `install`
immediately after the gate.

The install plan reports what each selected component will do against local
state and asks once, in the same shortcut format the Dotfiles execution plan
uses:

| Key | Meaning |
|---|---|
| `c` | confirm and run |
| `e` | edit — back to the selector |
| `q` | back to the menu |

Dotfiles' fourth key, `x confirm_forced`, is deliberately absent. Forcing means
bypassing "this is already present" checks, and an Agentbot install has none to
bypass: skills reconcile from the lock every time and the integrations set up
idempotently. A key that did what `c` does would be a lie about having a choice.

With no terminal to ask, the plan is printed and the run proceeds — an
unattended caller already chose by invoking the install, and waiting for an
answer nobody can give would hang it.

### While it runs

Each stage announces itself with a `[STEP]` line and closes with `[OK]`,
`[SKIP]`, or `[WARN]`. The markers are coloured where they are printed, so they
look the same from a terminal, through a pipe, and inside
`dotfiles full-update`. The run closes with a summary table, its rollup, and a
timing block naming the slowest components.

## Lifecycle update

The lifecycle builds a read-only plan before it writes. The plan covers skill
source reconciliation, optional integrations, registered workspaces, and
global outputs. Apply verifies that planned inputs have not changed, confirms
source-owned deltas once, and uses the shared rollback boundary for managed
surfaces.

The update prints a four-column report — component, installed, available,
action — confirms, applies, and then prints the same four columns with a
result. Timing comes last.

Use `agentbot update --dry-run` before an interactive update. `agentbot full`
runs install and update together.

## Repository update gate

Repository maintenance validates the expected origin, inspects local changes,
fetches, and classifies the checkout as current, ahead, behind, or diverged. A
clean confirmed behind checkout advances with a fast-forward-only pull.

Replacing dirty, ahead, or diverged state requires explicit authorization.
Before replacement, Agentbot preserves tracked and untracked changes in a
stash and local commits on a timestamped `recovery/agentbot-*` branch. It
verifies those backups before reset. It does not run `git clean`, delete
recovery data, commit, push, or force-push.

When an update changes the checkout, the old process exits with status 2 so a
caller can restart from the new code. Detached HEAD, missing upstream,
declined recovery, failed fetch, or failed backup stops downstream work.

`dotfiles full-update` is the system-maintenance orchestrator. It can authorize
the documented repository recovery, restart Agentbot after a self-update, and
run the non-interactive lifecycle update.

## Contracts a calling run may set

These are for a caller sequencing Agentbot inside a longer run — `dotfiles
full-update` and `bootstrap.sh` are the only ones today. Unset, each leaves
behaviour exactly as it is.

| Variable | Effect |
|---|---|
| `AGENTBOT_INSTALL_CONFIRM=yes` | pre-approve the install the caller already asked about |
| `AGENTBOT_INSTALL_GATE_ONLY=1` | run the repository gate and stop |
| `AGENTBOT_QUIET=1` | drop the `[info]` lines meant for someone running Agentbot directly |
| `AGENTBOT_TIMING_FILE=PATH` | `agentbot full` appends one `<stage> <seconds>` line per stage, so a caller can report its own sections instead of timing the whole command as one |
| `REPO_UPDATE_CALLER_RESTARTS=1` | the caller restarts the run itself, so the repository gate must not tell the operator to run setup again |

`agentbot install --menu` exits **4** when the operator backs out of the
selector. Nothing was installed and nothing is broken, which a caller has to be
able to tell apart from a completed install. A moved checkout still outranks it:
exit 2 means restart.
