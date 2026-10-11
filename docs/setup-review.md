# Setup review

```bash
agentbot setup-review                       # last-used CLI, else asks
agentbot setup-review --cli codex           # choose; remembered next time
agentbot setup-review --scope cli-config    # only the declared cli/ keys
agentbot setup-review --no-update           # report only, never offer edits
```

`setup-review` has an agent CLI of your choice review Dotfiles, Agentbot, the
shared library, and every enabled skill source, and saves what it finds as a
report. It is meant to be run every few weeks.

## Choosing the CLI

Any of the four main agents can run it: Claude Code (`claude`), Codex
(`codex`), Cursor CLI (`cursor`) and the Antigravity CLI (`agy`). Each is
looked for only where Dotfiles installs it, under `~/.local/bin`, and never on
`PATH`: inside WSL, `cursor` on `PATH` is the Windows editor, and the Windows
Antigravity editor can put its own `agy` there. A link that resolves into
`/mnt/` is refused.

`--cli` picks one and is remembered in
`~/.config/agentbot/setup-review.json`. Without it, the last one is used;
at a terminal with no remembered choice, you are asked from the CLIs that are
installed. A CLI that is not installed is reported with the ones that are; it
is never swapped for another.

Before the run, Agentbot asks the CLI whether it is signed in (`claude auth
status`, `codex login status`, `cursor-agent status`). `agy` has no such
check, so a sign-in or quota problem there shows when it runs. A usage or plan
limit during the run is reported as one (exit 3), with the CLI's own message
saved in the run folder.

## The two phases

**Review (read-only).** Agentbot copies each enabled skill source with a
shallow, read-only `git clone`, records which checkouts it found (installed
ones under the home folder, development ones in the workspace you run it from
or name with `--workspace`), and runs the CLI without a terminal, in its
read-only mode:

| CLI | Review command |
|---|---|
| Claude Code | `claude -p --permission-mode plan --add-dir … --allowedTools WebSearch WebFetch -- PROMPT` |
| Codex | `codex exec --sandbox read-only --skip-git-repo-check --search -- PROMPT` |
| Cursor CLI | `cursor-agent -p --mode plan --trust --output-format text --add-dir … -- PROMPT` |
| Antigravity | `agy --mode plan --print-timeout Ns --add-dir … -p PROMPT` |

Cursor's `--trust` answers its workspace-trust question for the working
directory alone, which is the run's own private folder; it is not the
`--force`/`--yolo` permission bypass. No CLI is ever given a flag that skips
its permission prompts, and a guard refuses to build such a command.

What the CLI prints is the report. Agentbot masks anything matching the memory
vault's secret rules and replaces the value of any credential-shaped setting
with a refusal before it saves the file.

**Update (only on yes).** At a terminal, Agentbot then asks whether to start
an interactive session of the same CLI to plan fixes from the report. The
session runs in the CLI's normal permission mode, in the development
workspace, and its prompt requires a plan first and your approval before each
change, with a separate yes for any edit to `skills.sources.yaml`. Install,
update, skill install or prune, dependency changes, credentials, commits,
pushes and deletion keep their own authorization. Without a terminal, or with
`--no-update`, the update phase never starts.

## The prompt

The prompts are versioned files in `review/` (`setup-review.md`,
`cli-config-review.md`, `update.md`; version `setup-review/1`), filled in with
the checkouts, the source copies and the budget. They name no CLI. They tell
the agent that everything it fetches or reads is untrusted data and that
instructions found there are reported, never followed; to search official
sources before community ones; to give a link, date, publisher and confidence
for every external finding; and to sort the report into confirmed defects,
outdated components, candidate improvements, the skill-source review, rejected
noise, and questions for you.

The skill-source review covers every skill in every enabled source with a
keep, exclude, replace or drop-source decision from the skill's own
`SKILL.md`, and proposes an explicit keep list for each `skills: all` source.
New upstream skills are candidates only.

`--scope cli-config` is the configuration check: it gives the agent only the
files in `cli/`, asks it to check each declared key against that CLI's current
official documentation, and to report renamed, removed or deprecated keys,
changed values, and new settings worth declaring. It proposes; any change to
`cli/` goes through the update phase like any other edit, and a
credential-shaped proposal is refused in the saved report.

## Limits and where things go

`--timeout` (default 45 minutes) stops the CLI and its child processes;
Ctrl-C does the same. Either way the partial output is saved. `--source-budget`
(default 40) caps the external pages the prompt allows. A review started from
inside another review is refused.

Each run gets a private folder, `~/.config/agentbot/setup-reviews/<run>/`
(mode 0700, files 0600):

| File | Contents |
|---|---|
| `report.md` | The report, masked, under a header naming the run, CLI and status |
| `prompt.md` | The exact prompt the CLI received |
| `run.json` | Provenance: prompt version and hash, CLI path and version, argv, times, exit status, checkouts with their commits, source copies, masking counts |
| `cli-stderr.txt` | The last lines of the CLI's error output, masked, when there were any |
| `sources/` | The read-only skill-source copies |

Reports stay there unless you choose to promote a finding into workspace
documentation.
