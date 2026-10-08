# Skills and integrations

## Curated sources

`skills.sources.yaml` is the canonical list of enabled repositories and skill
selections. Sources can select named skills or `skills: all`; an `exclude` list
removes unwanted upstream folders. GitHub uses `owner/repo`; explicit
`github.com/` and `gitlab.com/` prefixes are supported.

Agentbot clones each source before invoking the Skills CLI. A planned update
records the repository, manifest, global lock, and remote revisions, then
checks those inputs again before apply. A source failure makes the operation
fail even if other sources installed successfully.

For a skills-only update, run `./install.sh skills update` to preview source
revisions and the review ID. Apply that exact preview with
`./install.sh skills update --yes --plan-sha256 <review-id>`. Apply rechecks the
manifest, lock, source revisions, and discovered source inventory before it
installs anything. If any changed, preview again. Preview clones into temporary directories and does not change installed
skills or the lock.

## Lock ownership

Global `-g` installations are pinned in `~/.agents/.skill-lock.json`. The
repository `skills-lock.json` is a project-install stub. The formats and scopes
are different; do not copy the global lock into the project lock.

The global lock is the authority for curated machine installations. The
project stub remains empty until this repository intentionally adopts
project-local skill restoration.

Agentbot records each installed skill's source commit and content hash in the
global lock. Its `agentbotSources` section records each source's revision,
resolved installed names, and exclusions. `agentbotPreviousSources` keeps one
prior reviewed selection per changed source. A legacy lock without source
commits remains readable, but exact restore is unavailable until a reviewed
install or update records those commits.

`./install.sh skills restore` previews the pinned revisions. Add `--yes` to
clone those exact commits and reinstall only the recorded curated selections.
Use `skills restore --previous` to preview the prior reviewed selection, then
add `--yes` to apply it. Sources without a previous snapshot keep their current
pin. Only one prior selection per source is retained.
It leaves manually installed skills alone and refreshes managed assistant
links. If a pin is missing, malformed, no longer available upstream, or does
not match the manifest's source repository, restore fails without substituting
the latest revision. Neither update nor restore stages, commits, or pushes a
repository.

## Reconciliation and removal

`skills prune` classifies candidates as `excluded`, `orphaned`, `stale-pin`,
`manual`, or `modified`. It previews by default and requires `--yes` to write.
Manual and modified skills are preserved unless explicitly named. `skills remove-manual` accepts exact
names and never treats an empty selection as permission to remove everything.
Agentbot-protected Graphify output is not a manual-removal candidate.

Every install and update also applies two of those decisions without a
separate prune: a skill a source `exclude`s is removed when the lock pins it to
that source, and so is a skill the lock pins to a manifest source set to
`enabled: false`. The Skills CLI installs every skill a `skills: all` source
publishes, so the install pins each excluded skill it can prove it put there
(the whole folder is byte-identical to the source's, not just `SKILL.md`, and
the folder was not already on disk before the install ran) for that removal; a
copy with any changed or added file is the user's and stays. A skill pinned to a repository the manifest never names is
left alone, because the user installed it, and so is an unpinned directory that
only shares an excluded name: nothing shows Agentbot installed it, so it is a
`manual` candidate, removed only by name.

### Install receipts

A customised skill belongs to the user (Review 2, answer 3, 2026-10-08). The
lock's `skillFolderHash`, recorded at install, is the receipt:

- Prune never removes an excluded or orphaned skill whose files no longer match
  its receipt. It is classified `modified` and removed only by name.
- Install refuses a source that would overwrite, with different files, a skill
  folder that has no lock entry or no longer matches its receipt. The source
  fails before the Skills CLI runs, naming the folders to move or rename.
- An identical folder is not a conflict, and an unchanged managed skill updates
  as before. A pin recorded without a hash predates receipts and cannot show a
  change, so it is treated as unchanged.

## Claude listing states

Every listed skill's description is sent in every Claude session. The
`skillOverrides` key in `cli/claude.settings.json` (merged by
`cli-config apply`, `install` and `update`) sets each skill to `name-only`,
`user-invocable-only` or `off` without editing the skill. Building blocks that
other skills call are `name-only`, so they stay callable. The overrides also
cover Claude Code's built-in skills and the skills synced from the claude.ai
account (measured with `/context` on Claude Code 2.1.283). They do not reach
plugins synced from the account (for example the `knowledge-work-plugins`
ones), and neither does `enabledPlugins`; switch those off in the Claude
account. Codex and Cursor have no equivalent setting, so duplicates are
removed at the source instead.

## Graphify

Dotfiles owns the `graphify` executable. Agentbot runs
`graphify install --platform agents` only when the CLI exists, then refreshes
assistant links and managed outputs. A missing CLI is a valid skip. Agentbot
does not build project graphs, install hooks, or add Graphify to the curated
Git-source manifest.

## Boost

Dotfiles owns the verified `boost` executable. Agentbot owns Claude, Codex,
and Cursor setup, feature policy, diagnostics, and removal through
`agentbot boost status|setup|off`. Setup wires whichever of those CLIs are
installed and skips the rest. A later run picks up a newly installed CLI.

Setup disables tracing upload and Boost auto-update, keeps BoostGraph and its
MCP changes disabled, and writes the declared policy from
`BOOST_FEATURE_POLICY` in `src/boost.py`. Repository `.boost/config.toml` files
can shadow global configuration; Doctor reports registered workspaces that
violate policy, including a BoostGraph index in `.codegraph/` (v0.14 and
later) or in `.boost/` (earlier versions).

### Feature-flag policy

`BOOST_FEATURE_POLICY` names every feature flag Boost v0.15.1 ships, because
an unpinned flag is not neutral -- its effective value is a remote default
JFrog can change without warning. `boost doctor` prints the effective value,
the `user` pin and the remote default of each. Doctor warns about any flag
Boost writes to its config that the policy does not name, so a new release's
flags are decided rather than run by default; setup leaves such a flag alone
until the policy names it.

| Flag | Policy | Why |
|---|---|---|
| `boost-agent-facing-redaction` | on | Scrubs secrets from what the agent sees; deliberately not retrievable |
| `boost-auto-update` | off (fixed) | Dotfiles owns the binary and its version stamp |
| `boost-claude-status-line` | off (fixed) | Agentbot owns the Claude status line |
| `boost-cli-filtering` | on | The compression itself |
| `boost-english-abbreviation` | off | Lossy prose abbreviation, no gain here |
| `boost-files-optimization` | on | Document and image reads, source untouched |
| `boost-graph-integration` | off | BoostGraph rewrites files Agentbot owns |
| `boost-html-article` | off | Discards markup the agent is reading |
| `boost-html-article-cursor-nudge` | off | Undocumented; points at the disabled HTML path |
| `boost-html-article-preserve-agent-html` | on | Inert while HTML conversion is off; the safer setting if it is turned on |
| `boost-mcp-browser-budget` | off | A 120-line snapshot cap hides elements browser automation must act on |
| `boost-mcp-filtering` | on | Filters MCP responses; the original stays retrievable |
| `boost-mcp-toon-format` | on | Lossless reformat of MCP responses |
| `boost-path-ignore` | off | Enables `[ignore] optimize` exclusion globs; none are needed yet |
| `boost-pr-usage-comments` | off (fixed) | Outward-facing writes to GitHub PRs |
| `boost-share-cli-outputs` | off (fixed) | Sends command output to JFrog |
| `boost-target-version` | off (fixed) | Remote-chosen update target |

Boost would otherwise refresh the remote defaults from JFrog whenever its flag
cache expires, from any hook, and that request carries the Git email and the
repository name. Every flag is pinned, so the remote values change nothing:
Dotfiles' `.bashrc` exports `BOOST_FEATURE_FLAGS_DISABLE=1`, which stops the
fetch while the pins still apply. `boost feature-flags` then shows the pinned
values without contacting JFrog.

A lossy transform stays on only when the agent can retrieve the original with
`boost retrieve`. Secrets and credentials are the one exemption: redaction is
meant to be unrecoverable. "Fixed" flags are off permanently by the user's decision of 2026-10-08, and a
test fails if one is turned on. Each entry carries its full reasoning in
`src/boost.py`; change the map there rather than toggling in Boost's report
UI, which the next setup run reverts.

Since v0.15.1 the awareness file `boost init` writes tells agents to prefer
WebFetch because "Boost converts HTML to article Markdown". The paragraph is
fixed text in the binary, written whether or not `boost-html-article` is on, so
here it overstates what Boost does; the advice itself is harmless. Agentbot
leaves the file as Boost writes it: `boost init` owns and rewrites it, and the
stale-artifact check reads its version marker.

Everything else in `~/.boost/config.toml` is left to Boost and to you. Setup
pins `[tracing] upload` and `[update] auto_update` to `false` and writes the
`user` key of each policy flag; it does not touch `[hooks] exclude_commands`,
`[tracing] report`, `[tracking] database_path`, `[report]`, `[filters]`
(`disabled`, `retrieve_disable_threshold`, `show_loaders`), or `[mcp]
toon_format`.

**Setup accepts Boost's preview terms on your behalf.** `boost init` is passed
`--accept-terms`, which accepts the JFrog Online Preview Agreement and Privacy
Notice without prompting. This is a deliberate choice recorded here rather than
a prompt suppressed quietly: on a fresh machine the unanswered prompt sat for
the full command timeout and then failed the whole Agentbot install. Boost
stores the acceptance in its own `~/.boost/config.toml`, so it only ever
applies the first time on a given machine. Selecting the Boost CLI component is
what opts you into this; if you would rather accept it yourself, remove
`--accept-terms` from `BoostIntegration.setup` and answer the prompt.
