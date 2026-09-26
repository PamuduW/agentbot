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
agentbot full                     # install, then update
agentbot boot /path/to/repo       # render and register a workspace
agentbot workspaces               # list registered workspaces
agentbot resync --dry-run --all   # preview every registered workspace
agentbot token                    # Token Config: the GitHub and GitLab credentials
agentbot gitlab-token status      # inspect the saved GitLab read token, by fingerprint
agentbot mcp catalog              # list validated MCP candidates
agentbot mcp status               # inspect MCP ownership without network access
```

## Memory vault

`agentbot memory status` and `agentbot memory validate` inspect the private
Markdown memory vault without writing to it. The vault is found the way
`dotfiles-shared` is: `AGENTBOT_MEMORY_ROOT`, then `AGENTBOT_MEMORY_DIR`, then
`agent-memory` beside this checkout, then `~/agent-memory`. A candidate counts
only when it is a Git checkout carrying `.meta/vault.json`. No vault is a clean
unconfigured state: both commands say so and exit `2`, and nothing else in
Agentbot depends on memory.

The marker's `agentbot_memory_schema` (`1` or `2`) selects the validator.
Validation covers every tracked or unignored file plus local drafts: Markdown
only, no symlinks or special files, UTF-8 without NUL, size and front-matter
limits, strict YAML (no duplicate keys, anchors, aliases or tags), type, path,
status, slug and project rules, and the built-in secret scanner
(`agentbot-memory-secrets/v1`). Schema 2 adds UUID uniqueness across records
and drafts, scope/path consistency, date order, and supersession edges.
Findings name a relative path, a rule ID and a line; never a note body, a field
value, or a matched secret. `--acknowledge-warning RULE_ID` accepts one warning
rule for a single run; blocking rules cannot be acknowledged. `validate` exits
`0` when valid and `1` otherwise; `status` exits `0` whenever it can report,
including a dirty or invalid vault.

`agentbot memory propose --type decision|lesson|project --title TITLE --scope
global|shared|project [--project SLUG] [--tag TAG] --from-file PATH|--stdin`
writes one draft under the vault's Git-ignored `drafts/`, with a fresh UUID and
`status: draft`. The body comes only from the named file or standard input; no
conversation is captured. It needs a schema 2 vault, and it writes nothing when
the draft would fail validation, trips a blocking secret rule or an
unacknowledged warning, or when `drafts/` is not ignored. Creation is
exclusive, so an existing file is never replaced. A draft is never accepted
memory or a retrieval result.

`agentbot memory review` lists at most 100 pending drafts, newest first, with
metadata and finding counts. `agentbot memory review drafts/FILE.md` shows one
draft's metadata, body size, findings, same-title records, and the exact path
approval would install it at. Neither prints a note body, and both redact
metadata when the scanner fires on that draft.

`agentbot memory approve drafts/FILE.md` previews the exact destination;
`--yes` promotes the draft. Apply takes a bounded per-vault lock in Agentbot's
private state (a lock held by a live process makes the approval exit as a
conflict after a few seconds; a dead holder's lock is broken and reported).
Inside the lock it re-reads the draft and the destination, writes the accepted
record beside the destination, and installs it with `link()`, which cannot
replace a file. The draft is removed only afterwards, and only if unchanged.
Any conflict or failure leaves the draft intact and no partial record.
Approval stages, commits, and pushes nothing.

A draft with `supersedes` is approved as one transition: under the same lock,
the new record is installed and each accepted target's `status` line becomes
`superseded`, with every target rechecked against the bytes that were
reviewed. A person's edit to a target during approval wins: the approval
rolls back its own writes and reports a conflict. Retired targets, and
replacements across type or scope, are refused; `--allow-cross-scope` accepts
the latter after human review, and the new record never inherits the old
scope. No record is deleted.

`agentbot memory hook [status|install|remove] [--yes]` manages the vault's
pre-commit and pre-push hooks, which run `memory validate` before Git proceeds.
Only hooks carrying the `agentbot-memory-hook` marker are written or removed;
a `core.hooksPath` outside the vault's Git directory is refused. The hooks are
an accident guard (`--no-verify` bypasses them), and they do not scan
`.obsidian/`, where plugin settings are committed unscanned.

`agentbot memory due [--limit N]` is the bounded review queue (20 by default,
at most 100): accepted records whose `review_after` date has arrived, and those
past `valid_until`, oldest first, by UTC calendar day. A record is valid
through its `valid_until` day. Both flags are derived; neither changes a
record's status or file, and superseded or retired records never appear.
`memory status` shows the due and expired counts. Schema 1 records carry no
dates, so the queue is empty there.

`agentbot memory search QUERY`, `memory show PATH`, and `memory brief` read
validated records straight from the files; there is no index to go stale.
Only records with no validation or scanner finding are eligible, and drafts
never are. Superseded, retired, and expired records, and records past each
pool's hot limit (64 per project, 32 global, 48 shared, newest first), are
left out unless `--history` asks for them, labelled. Scope is never guessed:
without `--project SLUG` only global and shared records are offered, and other
projects need `--cross-project`. Search reserves slots before merging (the
best global, shared, and target-project result, then one per other project),
caps the target project at 4 and each other project at 1, skips near-duplicate
titles, and returns 8 results by default with provenance and a bounded
excerpt. `brief` prints a disposable Markdown brief of 800 tokens by default
and at most 1,200 (four characters per token), with the project slice capped
at 65%. Schema 1 records have no scope field: preferences are global, project
records and single-project records are project-scoped, and the rest are
shared.

`agentbot memory backup --destination PATH` previews, and with `--yes` writes,
a private (`0700`) backup: a bare mirror of the vault's committed refs plus a
manifest. Each run builds a new mirror, verifies it with `git fsck`, compares
refs with the source, and trial-restores it through the validator before it
replaces the previous snapshot; a failed backup leaves the old one intact. A
destination inside the vault, containing it, symlinked, or holding another
vault's backup is refused. A dirty tree is reported, not blocking.
`agentbot memory restore --source BACKUP --destination PATH` clones into a new
or empty directory, removes the backup `origin`, validates the result, and
never touches the active checkout, Agentbot's configuration, or a remote.
Git snapshots hold committed history only: uncommitted edits, ignored drafts,
and exports are never included. Remote snapshots are not implemented.

`agentbot memory migrate` moves a schema 1 vault to schema 2 through a
reviewed mapping, in four steps:

```bash
agentbot memory migrate plan --write PATH          # IDs; scopes to choose
agentbot memory migrate check --mapping PATH       # isolated candidate, v2-validated
agentbot memory migrate apply --mapping PATH --snapshot DIR [--yes]
agentbot memory migrate rollback --snapshot DIR [--yes]
```

`plan` needs a v1 tree with no findings. It proposes one UUID per record and
draft; `preferences.md` is global and `projects/**` project-scoped by rule, and
every other scope is left `null` for a person to choose (a suggestion sits
beside it; an empty project list is never read as global). The mapping file is
written `0600` and holds paths, hashes, IDs, and scopes, never note bodies.
`check` converts an isolated copy and runs complete-tree v2 validation.
`apply` needs a clean checkout on a branch, takes a verified `0700` byte
snapshot outside the vault, marks `.meta/migration-in-progress` so every
read fails closed, rewrites each file under the promotion lock only while its
bytes match the mapping, and changes the marker last. Only front matter
changes: `schema`, a new `id`, and a `scope` line; templates get blank `id`
and `scope` lines. Any failure restores original bytes wherever they are
still what the migration wrote. `rollback` does the same from the snapshot
later and never overwrites a later edit. Nothing is staged, committed, or
pushed.

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

Agentbot provides a provider-neutral MCP control plane for Claude Code, Codex
CLI, and Cursor CLI. `mcp/catalog.json` is its manifest, the way
`skills.sources.yaml` is for skills: an entry marked eligible has passed its
admission gate and is reviewed, and an entry that has not stays unreachable.

MCP is an install component. Selecting **MCP servers** in the install selector
registers every reviewed entry for all three clients; `agentbot update` does
the same, so a server added to the catalog later reaches a machine that was set
up before it existed; and `agentbot status` reports registered pairs against
available ones. Deselecting the component reads without writing, the rule
Graphify and Boost follow.

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

The Agentbot-owned GitLab facade is also present as an ineligible candidate.
It exposes fifteen typed REST `GET` operations and no GraphQL, generic-request,
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

Keep the token only in `GITLAB_MCP_READ_TOKEN`. Set `GITLAB_MCP_ORIGIN` to the
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
response data out of test output. GitLab remains ineligible until this suite
and isolated Claude, Codex, and Cursor launches pass with the same credential.

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
