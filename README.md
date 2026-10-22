# Agentbot

Agentbot is a local bootstrap CLI for agent policy, curated Agent Skills, and
registered workspace outputs. It keeps policy in canonical Markdown, renders
provider-specific compatibility files, and makes mutating operations explicit.

Use `./install.sh` from a checkout or `agentbot` after installation. Run
`agentbot help` for the command index and `agentbot help COMMAND` for details.

## Requirements

- Bash, Git, Python 3, and `python3-venv`
- Node.js, npm, and `npx` for managed skill sources
- optional `graphify` and `boost` CLIs installed by Dotfiles

Agentbot provisions its own Python packages. `./install.sh install` creates
`.venv` in the checkout, fills it from `requirements.txt`, and every Agentbot
command runs through that interpreter. The step is skipped when `requirements.txt`
has not changed since the last fill, so a normal install does no network work.

This is why the packages are not listed as prerequisites: a distribution
interpreter marked `EXTERNALLY-MANAGED` refuses `pip install` under PEP 668,
and apt carries neither `mcp` nor a tomlkit new enough for `requirements.txt`.
Read-only commands never create the environment; they report what is missing
and name `./install.sh install` as the remedy.

To use an interpreter you manage yourself, set `AGENTBOT_PYTHON` to it.
Agentbot then resolves through it and provisions nothing.

## Quick start

```bash
git clone https://github.com/PamuduW/dotfiles-shared ~/dotfiles-shared
git clone <your-remote>/agentbot ~/agentbot
cd ~/agentbot
./install.sh install
```

Agentbot loads its terminal stack, token storage and repository-update machinery
from [`dotfiles-shared`](https://github.com/PamuduW/dotfiles-shared), a separate
repository resolved at runtime. `dotfiles/bootstrap.sh` clones it; clone it
yourself when installing Agentbot on its own. It is found beside this checkout,
at `$HOME/dotfiles-shared`, or wherever `DOTFILES_SHARED_DIR` points, and a
missing one stops with the clone command rather than part-way through.

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
agentbot full                     # first run: install + update; then: update
agentbot boot /path/to/repo       # render, register, and give it project memory
agentbot workspaces               # list registered workspaces
agentbot resync --dry-run --all   # preview every registered workspace
agentbot token                    # Token Config: the GitHub and GitLab credentials
agentbot gitlab-token status      # inspect the saved GitLab read token, by fingerprint
agentbot mcp catalog              # list validated MCP candidates
agentbot mcp status               # inspect MCP ownership without network access
agentbot boost status             # inspect configured hooks, captures, and runtime flags
agentbot boost analyze SUMMARY.md # preview one selected summary; --yes uses Codex quota
```

For Boost privacy controls, capture limits and manual Engine analysis, see
[Boost integration](docs/skills.md#manual-engine-analysis).

## Memory vault

### First-time setup

Installing Agentbot does not create a memory vault. Each person needs their
own private vault; use the same vault on their other machines. Run these steps
in your own terminal (inside WSL when using Windows).

1. Check that Git has your name and email:

   ```bash
   git config --global user.name
   git config --global user.email
   ```

   If either is missing or belongs to someone else, set your own values:

   ```bash
   git config --global user.name "Your Name"
   git config --global user.email "you@example.com"
   ```

2. Create an empty **private** repository named `agent-memory` in your GitHub
   account. Leave the README, license, and `.gitignore` options unchecked.
   Make sure Git in this terminal can authenticate to your account. The
   commands below use SSH; an HTTPS remote with a Git credential helper also
   works.

3. Preview the new vault, replacing `YOUR_USERNAME` with your GitHub username:

   ```bash
   agentbot memory setup --new "$HOME/agent-memory" \
     --remote git@github.com:YOUR_USERNAME/agent-memory.git
   ```

   Review the preview, then apply it:

   ```bash
   agentbot memory setup --new "$HOME/agent-memory" \
     --remote git@github.com:YOUR_USERNAME/agent-memory.git --yes
   ```

   Use an empty or nonexistent destination outside other Git repositories.
   On WSL, keep it on the Linux filesystem, such as `~/agent-memory`, rather
   than a Windows drive under `/mnt/c`. Setup creates the initial local
   commit and records the vault location; it does not push.

4. Push the initial vault and check its state:

   ```bash
   git -C "$HOME/agent-memory" push -u origin main
   agentbot memory status
   agentbot memory validate
   agentbot memory sync --status
   ```

5. From each project repository, register it for project memory:

   ```bash
   cd ~/path/to/your-project
   agentbot boot
   agentbot memory project
   ```

   Agents can then save project decisions, lessons, and notes. Personal
   preferences and profile changes go through proposals that you approve
   in your terminal with `agentbot memory review`.

For a local-only vault, skip the GitHub repository and initial push, and omit
`--remote` from both setup commands. Memory stays on this machine without a
remote backup or sync.

On another machine, clone your existing vault instead of creating a new one.
Replace `YOUR_USERNAME`, preview the command, then repeat it with `--yes`:

```bash
agentbot memory setup --clone git@github.com:YOUR_USERNAME/agent-memory.git \
  --dest "$HOME/agent-memory"
```

### Commands and behavior

`agentbot memory status` and `agentbot memory validate` inspect the private
Markdown memory vault without writing to it. Agentbot never searches for a
vault; each machine is set up explicitly, from the CLI or the menu's
**Memory** entry:

```bash
agentbot memory setup --path /home/me/agent-memory            # an existing checkout
agentbot memory setup --clone git@github.com:me/agent-memory.git --dest /home/me/agent-memory
agentbot memory setup --new /home/me/agent-memory [--remote URL]
agentbot memory setup                                          # show the current choice
agentbot memory setup --remove                                 # forget it; the vault is untouched
```

Each previews first and applies only with `--yes`. The choice lives in
`${XDG_CONFIG_HOME:-~/.config}/agentbot/memory.json` (`0600`, in a `0700`
directory): the path, the vault's identity (its root commits), the remote, and
the branch, never credentials. A remote URL with embedded credentials is
refused; SSH keys or a Git credential helper authenticate. A configured path
that later holds a different repository, or no vault, fails closed.
`--clone` and `--new` refuse a destination that is not empty, is inside
another repository, crosses a symlink, or sits on a Windows drive or in a
temporary directory; `--new` creates an empty schema 3 vault (a marker with a
new `vault_id`, an empty project registry, and the tier READMEs) and never
pushes.
`AGENTBOT_MEMORY_ROOT` and `AGENTBOT_MEMORY_DIR` still override the setting
(the vault hooks use them). No vault configured is a clean state: memory
commands say so and exit `2`, and nothing else in Agentbot depends on memory.

`agentbot memory project` shows which project memory the current repository
resolves to. Resolution goes from the Git top level to `remote.origin.url`,
normalized (SSH, `ssh://`, and HTTPS forms, credentials stripped, an SSH host
alias resolved through `ssh -G` to the host it stands for, a non-default port
kept, and case folded on GitHub, GitLab, and Bitbucket), to an entry in the vault's
`.meta/projects.json`, so the same repository maps to the same folder on
every machine. Forks and renames are separate projects until explicitly
linked. A repository without a remote gets a generated `local/<id>-<name>`
origin and must be attached on each extra machine; that binding stays in
private `memory-bindings.json`. Two projects claiming one origin, alias, or
folder is a collision, and project writes stop until it is fixed. `agentbot boot` gives the repository
project memory (a registry entry plus its `project.md`) unless `--no-memory`
is passed, and `memory project register` does the same on its own; `attach PROJECT`
binds a no-remote checkout on another machine, and `link URL PROJECT --yes`
(human-only) adds a rename or fork as an alias.

Schema 3 (ADR-0009) splits the vault into trust tiers by path: `core/`
(`user/profile.md`, `user/preferences.md`, `decisions/`, `lessons/`; global or
shared scope, changed only with approval), `projects/<folder>/` (`project.md`,
`active-context.md`, `preferences.md`, `decisions/`, `lessons/`, `notes/`;
scope `project` for exactly that folder), and tracked `proposals/core/`
(drafts, never retrieved). Its marker carries a `vault_id`, and
`.meta/projects.json` is the project registry, cross-checked against the
project folders and each `project.md`. Supersession stays within one tier
and one project. Schema 3 is the only schema Agentbot reads: schema 1 and 2
vaults, and the commands that migrated them, were removed on 2026-09-27, so
an older vault is reported as unsupported. Every command that reads records
or changes the vault, sync and hook installation included, checks the marker
first, and so does `sync --status`, which reports the older schema as
unsupported. Three commands skip the check, because none of them reads a
record or writes the vault: `status` and `hook status` describe an older vault
so you can see what it is, `hook remove` removes only Agentbot's own hook, and
`sync --mode` / `--interval` change this machine's settings.

In Obsidian, the vault is a linked graph. Every project record carries
`up: "[[projects/<folder>/project]]"`, so `project.md` is the project's hub and
its backlinks list the project; a superseding record carries `replaces:` links
to what it replaced. Both are derived from `projects` and `supersedes`, which
stay canonical, and `project move` keeps them pointing at the right file. On
each machine where the vault is open in Obsidian, the sync sets Obsidian up
once: it adds `views/memory.base`, a Bases dashboard (project memory by
project, core memory, proposals waiting for approval, records due for review,
and superseded or retired records), enables the Bases core plugin, makes new
links absolute vault paths that update on rename, excludes `templates/` and
`exports/` from search and the graph, and colours core, projects and
proposals in the graph when no colour groups exist yet. All of it stays on
that machine. It never overwrites a dashboard or colour groups you already
have.

Project memory is written without approval, always for the repository you
are in: `agentbot memory project add --kind decision|lesson|note --title T
[--tag TAG] [--supersedes ID] --stdin`, `edit PATH [--title] [--tag]
[--stdin]`, `context --stdin` (the project's active context), `move PATH
NEW_PATH`, `delete PATH`, and `forget --yes` (the whole project folder; its
registry identity stays; the result says how to bring it back: find the
commit carrying its operation ID with `git log -F --grep 'Agentbot-Op: ID'`
and `git revert` it, on any machine, because sync replays the operation and
its local SHA changes). Paths are relative to the project and cannot leave
it; `project.md` cannot be moved or deleted. Each write generates or keeps
valid v3 front matter, is validated for its destination and secret-scanned,
and is one structured operation committed locally with the version it
expects, so a concurrent change on another machine becomes a preserved
conflict at sync time instead of an overwrite.

The engine underneath runs one memory operation at a time per clone (a lock
in `.git/agentbot-memory/`), stages each commit in its own Git index so
nothing you staged rides along, and validates that index written out to a
private folder, so the check reads exactly the bytes the commit will hold:
a change that adds a validation error, or takes a project past its record
limit, is refused, and on replay it becomes a preserved conflict instead.
Errors already in the vault do not block unrelated work; only new ones do.
Before an operation changes any file it is journaled, and before HEAD moves
it is queued. So after a crash or a killed process, the next memory command
puts back the files of a write that was never acknowledged, replays a
queued one on the next sync, and adopts one whose commit already landed. Each project write carries
its project's registry ID, and replay refuses it if that folder now belongs
to another project. Superseding marks
the replaced record in the same operation, all or nothing, and only within
the project. `AGENTBOT_MEMORY_PROJECT_WRITES=off` turns project writes off;
reads keep working.

Syncing is automatic. After every Agentbot-owned write
the vault is synced: fetched, pending operations replayed on the remote tip,
and pushed, never forced. Reads (`status`, `search`, `show`, `brief`, `due`,
`project`) fetch first, at most once per interval (300 seconds by default,
counted from the last attempt, so an offline machine does not retry on every
read); `validate` does not, because hooks run it mid-commit. Offline (or a
Git network command that times out), writes stay committed locally and
pending until the next sync; uncommitted manual edits pause automation until
they are committed or reverted; neither fails the command. Obsidian's own folders (`.obsidian/`, its settings and layout, `views/`, its
Bases dashboards, and `.trash/`) are per machine and not manual edits:
Obsidian rewrites them itself (on open, as you zoom the graph, as you resize a
table), so they never pause sync, and the first sync adds them to a managed block in the vault's
`.gitignore` and stops tracking them, keeping each machine's copy. While
history still tracks some, sync reads them just before each reset and puts
this machine's copy back; if there are more than it can hold (500 files or
16 MiB), it stops before the reset instead of keeping only some.
`agentbot memory sync` syncs now;
`--status` shows the mode, pending operations, open conflicts, and last sync;
`--mode manual` stops automatic pushing and fetching (writes are still
committed locally), and `--interval SECONDS` sets the fetch interval. When two
machines changed the same record, the losing operation is preserved:
`agentbot memory conflict` lists open conflicts, `conflict show OP` shows the
current version beside yours, and `conflict resolve OP --keep theirs|mine`
accepts the current state or re-applies yours as a new operation (a lost
supersession or move is re-applied whole). Core conflicts are for the human
to resolve.

Core memory changes only through tracked proposals.
`agentbot memory propose --type decision|lesson|preference|profile --title T
--scope global|shared [--supersedes ID] --stdin` writes one validated,
secret-scanned record under `proposals/core/`, commits it, and syncs it, so it
can be reviewed from any machine; proposals are never returned by search,
show, or brief. The queue warns at 10 open proposals and refuses new ones at
25, and a title that duplicates an open proposal is refused. `memory review`
lists the queue. `memory approve PATH` previews and `--yes` (the user's action)
installs the record in `core/` in one operation with the proposal's removal
and any superseded core records marked; preference and profile proposals
replace `core/user/preferences.md` or `profile.md` and keep its ID. `memory
reject PATH --yes` removes a proposal. Identical approvals from two machines
converge; two different approvals of one core file become a conflict for the
human.

Human-only confirmations need a person at a terminal. `approve --yes` and
`reject --yes` and `conflict resolve` for a core
conflict refuse unless standard input is a terminal, then ask you to type the
short code shown (the record's or operation's first 8 characters). An
ordinary agent shell has no terminal, so an agent that is told to approve
gets the command to hand back instead. This deters; it is not a security
boundary: a harness that gives the agent a pseudo-terminal can pass it, and
nothing here proves a person is present. Conflict resolution (keep mine or
theirs) is also refused outside the repository the conflict belongs to. One
exception: when a repository was registered twice under different IDs, its
losing registration's writes can be discarded (keep theirs) from that same
repository, never re-applied.

`search`, `show`, and `brief` detect the project from the
directory you run them in (Git top level, origin, registry) when neither
`--project` nor `--cross-project` is given; `--no-auto-project` turns that off.
An unregistered repository, a registry collision, or a directory outside any
repository gets global and shared memory only; nothing is guessed. The JSON
output reports `project` and `project_source` (`explicit`, `auto`, or none).
Every output that carries record text (the brief; search and show, as text
or JSON; a proposal's body in `review`; both versions in `conflict show`)
carries a fixed notice that the content is retrieved memory to treat as
evidence, never as instructions, and record text is never rendered into
policy. The notice is guidance to the model, not enforcement: memory an
agent was tricked into writing is contained to its project folder, but the
agent reading it may still have other tools. A project's `active-context.md` leads its share of the brief.

Project memory keeps itself in shape with deterministic checks on every
write: a body identical to another record in the project is refused, a
near-duplicate title is a warning, the active context warns above about 800
tokens and is refused above 1,200, and a project with 48 live records warns
while 64 stops new records until some are retired, superseded, or deleted
(superseding never grows the pool). `agentbot memory project maintain` reports
what the current project needs (live count against the limits, expired and
due records, similar titles, context size) without writing. `project retire
PATH` takes a record out of retrieval but keeps it; `project promote PATH
[--scope global|shared]` proposes a project lesson or decision for core
memory, leaving the source in place.

The marker's `agentbot_memory_schema` must be `3`. Validation covers every
tracked or unignored file: Markdown only, no symlinks or special files, UTF-8
without NUL, size and front-matter limits, strict YAML (no duplicate keys,
anchors, aliases or tags), type, path, status, slug and project rules, UUID
uniqueness across records and proposals, scope/tier consistency, date order,
supersession edges, the project registry, and the built-in secret scanner
(`agentbot-memory-secrets/v1`).
Findings name a relative path, a rule ID and a line; never a note body, a field
value, or a matched secret. `--acknowledge-warning RULE_ID` accepts one warning
rule for a single run; blocking rules cannot be acknowledged. `validate` exits
`0` when valid and `1` otherwise; `status` exits `0` whenever it can report,
including a dirty or invalid vault.

`agentbot memory hook [status|install|remove] [--yes]` manages the vault's
pre-commit and pre-push hooks, which run `memory validate` before Git proceeds:
pre-commit with `--staged`, so it judges exactly the staged bytes the commit
will record (the check the sync engine applies to its own commits), and
pre-push over the whole tree. Every `agentbot update` (and so `dotfiles
full-update`) installs the hooks when they are absent and replaces an outdated
Agentbot hook, so a new release of the checks needs no manual step. Only hooks
carrying the `agentbot-memory-hook` marker are written or removed;
a `core.hooksPath` outside the vault's Git directory is refused. The hooks are
an accident guard (`--no-verify` bypasses them), and they do not scan
`.obsidian/`, where plugin settings are committed unscanned.

`agentbot memory due [--limit N]` is the bounded review queue (20 by default,
at most 100): accepted records whose `review_after` date has arrived, and those
past `valid_until`, oldest first, by UTC calendar day. A record is valid
through its `valid_until` day. Both flags are derived; neither changes a
record's status or file, and superseded or retired records never appear.
`memory status` shows the due and expired counts.

`memory project add` and `memory propose` accept `--review-after YYYY-MM-DD`
and `--valid-until YYYY-MM-DD`. They store validated metadata rather than a
date only in the note body. Invalid dates, dates before the record's creation
date, or a review date later than expiry are refused before a commit. Core
proposals remain drafts requiring the existing human approval.

A project correction made with `--supersedes ID` inherits an omitted expiry.
An inherited future review date is retained; an already due review stays due
on the replacement's creation date. Supply new date flags to change those
dates deliberately. An already expired record cannot be made permanent by
omitting its expiry when superseding it.

`memory capture` gives an extracted fact a stable delivery receipt:

```bash
agentbot memory capture --event-id EVENT_UUID --client antigravity \
  --target project --kind note --title "Local tool workaround" --stdin --json
```

Generate the event UUID once and retain it for retries. The registered calling
repository determines the project; there is no foreign-project override. Clients
are `claude`, `codex`, `cursor`, and `antigravity`. Project captures support
`decision`, `lesson`, and `note`; `--target proposal` supports the existing core
proposal kinds and creates a global draft requiring human review. Tags, dates
and one `--supersedes ID` are supported. This command accepts an extracted fact;
it does not extract transcripts or call a model.

Repeated delivery with the same project, client, event UUID and payload returns
the original operation receipt without a new record. Reusing an event for a
different payload is refused. Receipt lookup and writing share the writer lock.
Hashes and receipt identifiers travel with the validated transaction and its
Git commit; fact text is not copied into commit metadata. A receipt identifies
the historical save, not whether that fact is still active or was later retired.

An interrupted operation that has not committed returns `pending`, never saved;
run `agentbot memory sync` and retry the same event. A preserved sync conflict
returns `conflict`, or `resolved` after resolution, without recreating the event.
These states return exit 1; a locally committed receipt returns exit 0, with sync
reported separately. Sync failures still leave committed facts available locally.

This prevents duplicate delivery within one clone and against committed history
already synced to another clone. Disconnected machines cannot coordinate
simultaneous claims. Conflict receipts are local state and do not follow a
discarded operation to another clone. Do not downgrade Agentbot while captured
operations remain pending; uncaptured operations retain their existing queue
format. This is not a durable queue for facts that have never reached the writer,
and client hook delivery remains a separate integration check.

When the vault cannot currently accept a write, use the same capture arguments
with `--defer`. This validates and secret-scans the extracted fact, then stores
it in a private local delivery queue. Exit 0 with `state: queued` confirms only
that queue write; it does not confirm accepted memory. No model runs and no
transcript is collected. Bodies are limited to 16 KiB, with at most 64 entries
and 2 MiB of queue data across projects.

```bash
agentbot memory capture-queue status --json
agentbot memory capture-queue drain --limit 1 --json
agentbot memory capture-queue discard QUEUE_ID       # preview only
agentbot memory capture-queue discard QUEUE_ID --yes # discard this pending input
```

The queue is under Agentbot's config home in `memory-capture/`, with directory
mode 700 and file mode 600. It contains fact text, stays on this machine and is
not included in the vault's Git backup or sync. Status shows counts and opaque
event IDs, attempts and reasons without fact text; `other_pending` counts items
bound elsewhere. Run commands from the original registered checkout. Nested
directories resolve to that checkout's root. A changed project identity cannot
redirect queued facts to another project.

Drain defaults to one entry and accepts limits from 1 to 8. It uses the normal
validated writer, retaining failures as pending. The existing capture receipt
handles a crash after commit but before dequeue without duplicating the record.
Drain exits 1 if this checkout still has pending entries. Fix the write or vault
problem and retry; for a permanently invalid or expired input, preview its
discard before confirming. Discard never changes accepted vault records. Unsafe
or malformed queue files are refused and preserved for recovery. Facts never
extracted and submitted cannot be recovered by this queue.

`agentbot memory serve --project-root /absolute/path/to/repository --client CLIENT`
runs a local stdio MCP server using Agentbot's existing environment. Configure
that exact command in the client's trusted workspace connection. The path must
resolve to a registered checkout. Startup binds its project UUID, vault and
client; tools cannot override them. The validated identity reaches the final
writer, so changing the checkout's origin cannot redirect a capture elsewhere.
This interface is opt-in; installing Agentbot does not register or approve it.

It exposes `memory_brief`, `memory_search`, `memory_show`, `memory_capture`,
`memory_queue_status` and `memory_queue_drain`. Reads use the consistency guard
and normal retrieval scope and budgets. They read the local vault without
fetching; freshness metadata explicitly reports that limitation. Capture takes
an extracted fact and a stable event UUID. It checks an existing committed
receipt first, otherwise queues the fact privately and attempts that exact event.
Only a committed receipt returns `saved: true`; `state: queued, saved: false`
means pending delivery. A saved proposal is a durable draft, not accepted core
memory. Capture and drain use the existing best-effort sync after local commits.

Unknown tools, extra fields, invalid types and oversized inputs are rejected.
Operational refusals and failed drain attempts set the MCP error flag without
printing private exception text. Retrieved text carries its data-only notice.
There are no tools for arbitrary files, commands, core approval, permission
changes or discarding pending inputs. No listening port or model service is
created. These choices follow the [MCP tool contract](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
and [stdio transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).

The client must actually launch and trust the process with the required access.
Its shell sandbox and MCP process can have different permissions. A configured
connection does not prove successful tool delivery or model use. In the current
restricted test harness, even an empty SDK server's stdio handshake times out;
the same probe and synthetic save/restart/recall tests pass outside that sandbox.
Keep per-host acceptance separate from the storage and protocol checks.

Session adapters are explicit and opt-in:

```bash
agentbot memory session recall --client codex --session-id SESSION --json
agentbot memory session checkpoint --client codex --session-id SESSION --event-id EVENT --json
agentbot memory session hook --client antigravity --event SessionStart --project-root /absolute/checkout
```

`recall` prepares a local bounded brief using the same consistency guard.
It reports `model_use_verified: false`: preparing context is not evidence that
an agent received or used it. `checkpoint` prepares a short capture reminder;
it always says `saved: false`. Facts must still go through validated capture.
Receipt logging is private and best-effort; logging failure does not invalidate
successful recall. Receipts contain hashed identities, client, event kind and
time, never memory bodies, raw session identifiers or transcripts. State lives
in `memory-sessions/`, directory mode `0700`, file mode `0600`. The latest 256
receipts suppress duplicate checkpoint reminders; older events can be reminded
again. Recall is never suppressed, including after context loss.

`session hook` consumes at most 64 KiB of JSON stdin, validates its single
workspace against the explicitly bound checkout and emits only the host's
context envelope. It supports Claude/Codex `SessionStart`, Cursor `sessionStart`
and Antigravity `SessionStart`. Checkpoint envelopes use Claude `PostToolUse`,
Codex `PreToolUse`, Cursor `postToolUse` and Antigravity `PreInvocation`.
Unknown events, bad payloads and unavailable memory fail open with `{}` and a
sanitized stderr notice. No transcript, prompt or tool-result text is stored.
No hook grants permissions, blocks a turn, forces continuation or launches a
model. Installing Agentbot does not register these hooks; preserve existing
hooks and obtain native trust before testing delivery in each installed client.
Context-loss delivery and tool readiness remain host-specific acceptance checks.
Checkpoint cadence needs tuning before broad registration; every eligible new
event can issue a reminder. These contracts follow the current
[Claude](https://code.claude.com/docs/en/hooks),
[Codex](https://learn.chatgpt.com/docs/hooks),
[Cursor](https://cursor.com/docs/hooks) and
[Antigravity](https://www.antigravity.google/docs/hooks) references.

Doctor reads sync state without creating its directory and uses a consistent
vault snapshot. Incomplete snapshots need a retry, not a broken-vault claim.
Private capture input pending for this vault produces a count-only warning;
it is separate from committed writes waiting for remote sync. Queue corruption
is reported without exposing its contents or deleting the pending input.

`agentbot memory search QUERY`, `memory show PATH`, and `memory brief` read
validated records straight from the files; there is no index to go stale.
A record under a project's `notes/` keeps the schema type `project` in its
file but reads as kind `note` in their output, and `--type note` selects it.
Only records with no validation or scanner finding are eligible, and proposals
never are. Superseded, retired, and expired records are left out unless
`--history` asks for them, labelled. A query searches all eligible records
before ranking and bounding its output, so older accepted facts remain
searchable. The brief and header-only index retain each pool's hot limit
(64 per project, 32 global, 48 shared, newest first). Scope is never guessed:
without `--project SLUG` only global and shared records are offered, and other
projects need `--cross-project`. Search reserves slots before merging (the
best global, shared, and target-project result, then one per other project),
caps the target project at 4 and each other project at 1, skips near-duplicate
titles, and returns 8 results by default with provenance and a bounded
excerpt. Without a query, `search` is the index: record headers (title, type,
tags, date, path) with no excerpt, newest first, 30 by default and at most
100, within the same scope. It lists eligible headers from the recent pools, without the
search's near-duplicate skip or per-project quota, and reports any past the
limit as `over-limit`, so an agent can see what exists before reading anything. A
bad request (an overlong query, a path outside scope) is refused as `refused`,
never reported as a broken vault. A record over about 400 tokens is still
written, with advice to split it into one record per idea. `brief` prints a disposable Markdown brief of 800 tokens by default
and at most 1,200 (four characters per token), with the project slice capped
at 65%.

Optional sync failures do not prevent `search`, `show`, or `brief` from reading
a consistent local view. Their JSON output includes `refresh` with `state`
(`current`, `skipped`, or `degraded`), a content-free `reason`, and `local_only`.
`current` means this invocation completed synchronization; `skipped` does not
claim that the local data is fresh. Human output reports degraded refresh on
stderr. Explicit sync and all write failures keep their existing behavior.

Recall opens an existing writer lock read-only and checks file identities
before publishing a result. It creates no sync state and performs no journal
recovery. An active writer, an interrupted journal, an uncommitted operation,
unsafe state or repeated concurrent edits returns `state: unavailable`, with
no memory body, rather than presenting a mixed snapshot as valid. A normal
writer/sync operation must perform any required recovery. This guard also covers session recall and Doctor vault inspection. It does not
establish universal availability or automatic model-facing capture.

`agentbot memory backup --destination PATH` previews, and with `--yes` writes,
a private (`0700`) backup: a bare mirror of the vault's committed refs plus a
manifest, together one generation. Each run builds a new generation, verifies
it with `git fsck`, compares refs with the source, trial-restores it through
the validator, writes its manifest, and only then switches the destination's
`current` link to it in one rename; a backup that fails or is interrupted
before that switch leaves the previous generation intact. A destination inside
the vault, containing it, symlinked, or holding another vault's backup (other
history or vault ID) is refused. A dirty tree is reported, not blocking.
`agentbot memory restore --source BACKUP --destination PATH` clones into a new
or empty directory, removes the backup `origin`, validates the result, and
never touches the active checkout, Agentbot's configuration, or a remote.
Git snapshots hold committed history only: uncommitted edits and exports are
never included. Remote snapshots are not implemented. A completed backup
records its destination (and assurance) in this machine's `memory.json`;
each `agentbot update`, and so `agentbot full` and `dotfiles fu`, refreshes it
once its earlier steps have run, and shows a "Memory backup" row, and `memory backup --yes` without
`--destination` refreshes it by hand. A refresh that fails is reported and
leaves the previous snapshot in place.

The editor and CLI surfaces are part of an install and appear in `status`. They
are also directly addressable:

```bash
agentbot vscode status|seed|apply     # extensions and owned settings, per host
agentbot cursor status|statusline     # the managed Cursor CLI statusline
agentbot cli-config status|apply      # declared Claude, Codex, Cursor keys
```

`agentbot setup-review` (also in the menu) has an agent CLI you choose review
the setup read-only, after fetching and fast-forwarding the installed
checkouts, and commits the report to Dotfiles' `reviews/` on `development`.
See [Setup review](docs/setup-review.md).

The menu and direct CLI share the same command model and lifecycle code. Token
input is masked, and normal output shows only a fingerprint.

## MCP management

Agentbot provides a provider-neutral MCP control plane for Claude Code, Codex
CLI, and Cursor CLI, with Antigravity CLI (`~/.gemini/config/mcp_config.json`)
as an optional fourth client. `mcp/catalog.json` is its manifest, the way
`skills.sources.yaml` is for skills: an entry marked eligible has passed its
admission gate and is reviewed, and an entry that has not stays unreachable.

MCP is an install component. Selecting **MCP servers** in the install selector
registers every reviewed entry for Claude, Codex and Cursor, and for
Antigravity when `agy` is installed; `agentbot update` does
the same, so a server added to the catalog later reaches a machine that was set
up before it existed; and `agentbot status` reports registered pairs against
available ones. Deselecting the component reads without writing, the rule
Graphify and Boost follow.

Antigravity is a target only once it is installed, and only for entries whose
`antigravity` contract is enabled. `agy` passes `${NAME}` in headers, `env`
and args through as literal text (checked with agy 1.3.3), so Agentbot renders
its stdio servers without an `env` block and they inherit the token from the
shell that started `agy`. Context7, AWS Knowledge, Microsoft Learn and the
GitLab facade are enabled that way. GitHub authenticates with a header, so
Antigravity reaches it through `agentbot mcp serve --catalog-id github`: a
local bridge that reads `GITHUB_MCP_TOKEN` from its environment, sends the same
read-only controls as the other clients, and serves only the catalog's 18 tools
whose descriptors match their pins. The catalog refuses to enable Antigravity
for any other header-authenticated remote entry unless it names such a bridge
command.

The component never overwrites configuration it does not own. A same-name entry
another tool wrote, or a registered entry that no longer matches its record, is
reported and left alone -- resolving those means overwriting somebody's file,
which stays an explicit act through the commands below.

```bash
agentbot mcp catalog
agentbot mcp status
agentbot mcp plan --select ID... --targets claude codex cursor
agentbot mcp setup --select ID... --targets CLIENT... --yes
agentbot mcp off --select ID... --targets CLIENT... --yes
agentbot mcp restore OPERATION_ID --yes
```

The GitLab facade reads with the token saved under **Token Config › GitLab**,
or `agentbot gitlab-token`. It is stored as a single mode-`0600` assignment in
`${XDG_CONFIG_HOME:-$HOME/.config}/agentbot/gitlab.env`, is never passed through
an argument vector, and is only ever reported by fingerprint. Nothing else is
stored beside it: every facade tool takes `project_id` as a call argument, so
what may be read is decided by the token's own access.

Startup and `agentbot mcp status` use the same credential lookup. A non-empty
`GITLAB_MCP_READ_TOKEN` overrides the saved token; an unset or empty variable
uses the saved file, so a fresh session needs no manual export. An invalid
explicit value or an unsafe saved file is refused without showing its contents.
The file must have mode `0600` and its directory mode `0700`. Status checks
credential configuration locally; they do not verify access with GitLab.
Claude, Cursor and Antigravity inherit optional environment overrides. Codex
forwards the token, origin, XDG configuration location and TLS certificate
variables when present. No client configuration contains the token itself.
Reapply Agentbot's managed MCP configuration after updating from a version
that required token interpolation in Claude or Cursor.

**A credential that can write is refused, not warned about.** Both token
screens check scopes before storing anything, and check them again on demand,
because a token that was read-only when it was saved is exactly the thing whose
scopes get widened later. GitLab reads them from its token self-inspection
endpoint; GitHub reads the `X-OAuth-Scopes` response header and allowlists the
read-only ones, so a scope invented after this was written fails closed. A
fine-grained token publishes no scopes at all — that is unknown rather than
none, and unknown is never treated as a refusal.

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

### Filtered knowledge overlays

Context7, AWS Knowledge, and Microsoft Learn are admitted catalog entries. They
need no credential in the shipped anonymous configuration. Each client starts
the same local Agentbot stdio filter, which connects only to the candidate's
fixed HTTPS origin and exposes only the exact descriptor-pinned tools recorded
in `mcp/contracts/`. Missing tools or changed descriptors stop the filter
before it serves any tool.

AWS Knowledge exposes public documentation, region availability, and published
AWS skill retrieval only. It does not expose an AWS account, AWS API execution,
pricing, signing, agent-script, or infrastructure-mutation tool. Microsoft
Learn exposes documentation search, article fetch, and code-sample search.
Context7 exposes library resolution and documentation queries; its optional API
key is deliberately outside the managed anonymous contract.

To opt in to all three on all supported clients:

```bash
agentbot mcp plan \
  --select context7 aws_knowledge microsoft_learn \
  --targets claude codex cursor
agentbot mcp setup \
  --select context7 aws_knowledge microsoft_learn \
  --targets claude codex cursor \
  --yes
```

The public live and installed-client gates remain opt-in test commands because
they require network access and all three CLIs:

```bash
AGENTBOT_TEST_MCP_OVERLAYS=1 \
  python3 -m unittest tests.integration.test_mcp_overlays_live -v
AGENTBOT_TEST_MCP_CLIENTS=1 \
  python3 -m unittest tests.integration.test_mcp_overlay_clients -v
```

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

### GitLab admission status

The Agentbot-owned GitLab facade exposes fifteen typed REST `GET` operations and no GraphQL, generic-request,
pipeline-action, artifact, package, registry, or mutation tool. Its credential
must have only GitLab's
[`read_api` scope](https://docs.gitlab.com/security/tokens/access_token_scopes/).
Prefer a project access token for one project, then a group access token for a
group; use a personal access token only when the required reads cross those
boundaries. [Project access tokens](https://docs.gitlab.com/api/rest/authentication/#project-access-tokens)
on GitLab.com require Premium or Ultimate; self-managed GitLab provides them on
Free, Premium, and Ultimate. Internal projects can be visible beyond their
membership boundary to authenticated users, so use an explicitly safe test
project and verify its visibility before admission.

Save the token through **Token Config › GitLab** for normal use. The optional
live acceptance test below still requires `GITLAB_MCP_READ_TOKEN` explicitly.
Set `GITLAB_MCP_ORIGIN` to the
fixed HTTPS origin for a self-managed instance; omit it for GitLab.com. The
opt-in acceptance gate also requires declarations that make the tested
environment auditable without recording the credential or returned content:

```bash
AGENTBOT_TEST_GITLAB_MCP=1 \
  GITLAB_MCP_READ_TOKEN="$GITLAB_MCP_READ_TOKEN" \
  GITLAB_MCP_TEST_PROJECT="safe-group/safe-project" \
  GITLAB_MCP_INSTANCE_VERSION="your-version" \
  GITLAB_MCP_TEST_TIER="free|premium|ultimate" \
  GITLAB_MCP_TOKEN_KIND="project|group|personal" \
  GITLAB_MCP_TEST_ROLE="your-project-role" \
  python3 -m unittest tests.integration.test_gitlab_read_live -v
```

The safe project needs a default branch with at least one file, one merge
request, and one completed CI job. The suite calls all fifteen tools, rejects
write-shaped and generic bypass names locally, bounds every request, and keeps
response data out of test output. Run this suite and isolated Claude, Codex,
and Cursor launches when verifying live access with a credential.

### Atlassian Cloud read MCP

`atlassian_read` exposes ten local read tools for Jira and Confluence, using
separate scoped API tokens. The server uses fixed Cloud gateway routes, rejects
mutation and generic-request tools, and never follows redirects or returned
URLs. Save the tokens, email and Cloud ID through **5. Token Config → Atlassian tokens**;
the MCP loads the private saved settings automatically.
See [setup, read scopes and validation](docs/atlassian-read.md).
Live site and native-client acceptance remain pending.

## Skills

[`skills.sources.yaml`](skills.sources.yaml) is the canonical source manifest.
Global installs and pins live under `~/.agents/`; the committed
[`skills-lock.json`](skills-lock.json) is a project-level stub, not a copy of
the global lock. Before the launcher is installed, run the same commands
from a checkout as `./install.sh skills ...`.

```bash
agentbot skills install
agentbot skills update
agentbot skills list
agentbot skills doctor
agentbot skills prune             # preview source reconciliation
agentbot skills prune --yes       # remove selected managed candidates
agentbot skills remove-manual     # preview user-placed skills
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
- [Setup review](docs/setup-review.md)
- [Validation](docs/validation.md)
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
