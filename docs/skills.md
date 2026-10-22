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

For a skills-only update, run `agentbot skills update` to preview source
revisions and the review ID. Apply that exact preview with
`agentbot skills update --yes --plan-sha256 <review-id>`. Apply runs the same
transaction as a full update (reconcile, then the install as its validation
step), so a skill moving between sources is handed over. It rechecks the
manifest, lock, source revisions, and discovered source inventory before it
installs anything. If any changed, preview again. Preview clones into temporary directories and does not change installed
skills or the lock.

The transaction snapshots the global lock, installed skill store and managed
Claude/Codex skill directories even when no skill names change. A failed
installation restores earlier source updates and their lock entries.

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

`agentbot skills restore` previews the pinned revisions. Add `--yes` to
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
- Reconciliation applies the same receipt check to upstream removals. Locally
  changed skills, their links and receipts are preserved and reported. The
  upstream selection can be removed from the manifest without deleting the
  customised copy. Use an exact-name prune decision to remove it.
- Install refuses a source that would overwrite, with different files, a skill
  folder that has no lock entry or no longer matches its receipt. The source
  fails before the Skills CLI runs, naming the folders to move or rename.
- An identical folder is not a conflict, and an unchanged managed skill updates
  as before. A pin recorded without a hash predates receipts and cannot show a
  change, so it is treated as unchanged.

## Adapted skills

Agentbot also prepares a few selected upstream skills in temporary copies
before installation. `writing-skills` receives its Codex and Gemini support
references; `github-actions`, `k8s` and `owasp` each receive their report-format
reference. Their links point inside the installed skill. The excluded sibling
policy entrypoints remain unselected.

The selected `verify-citations` keeps its verification scripts but runs
statelessly: it does not read or create paper-memory files or retain author
lessons. Missing required support files or changed memory instructions stop
preparation before the Skills CLI runs.

Each prepared skill includes `.agentbot-packaging.json`, recording its upstream
repository, source revision, packaging version and input-file SHA-256 hashes.
The install receipt covers the prepared content. The original checkout stays
unchanged, and exact-revision restore applies the same preparation rules.
The rules live in `src/skill_packaging.py`; review them when those upstream
files change. The first-party source selects its four skills by name, so new
upstream skills require a manifest decision.

A source may publish adaptations of another source's skills, listed in an
`adaptations.yaml` at its root with the upstream repository, the skill's path
there, the commit the adaptation was made from, and that path's Git tree hash
at that commit. Every applied update (not a dry run or a declined plan)
compares the recorded hash with the same path in the upstream source's
verified clone and records the result in
`~/.config/agentbot/skill-adaptations.json`; the update table and
`agentbot status` show it as "Adapted skills", for example
`brainstorming: superpowers changed since bb92a77`. A change elsewhere in the
upstream repository does not count. The check only reports: redoing an
adaptation is a manual edit in the adapting source (`agentbot_skills` keeps the
steps in `adaptations/README.md`), and the upstream must itself be a managed
source.

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

## Archify

`tt-a1i/archify` is a curated source for presentation-grade architecture,
workflow and trust-boundary diagrams; Mermaid
stays the default for repository documentation. The skill's `finalize` gate
needs a Chrome: Dotfiles installs Chrome for Testing's headless shell and its
`.bashrc` exports `ARCHIFY_CHROME`, plus `ARCHIFY_UPDATE_CHECK_DISABLED=1` so
the skill does not check GitHub for a newer release on every render. Agentbot
installs only the skill; it is name-only in Claude.

## Opt-in review and learning sources

Each lists only the skills that earned a place:

- `chaseai-yt/claudex-loop`: `claudex-loop` and `codex-review` (Claude plans,
  Codex reviews), for high-consequence plans, at most two rounds, by hand.
  `codex-review` reads its sibling's files, so the two come together;
  `codex-build` stays out. Name-only in Claude.
- `oaustegard/claude-skills`: `declaring-invariants` only. Name-only in Claude.
- `nykooi1/vibe-wise`: `learn` and `reset`, an opt-in learning mode. Both are
  user-invocable-only in Claude. Its notes live in a project's `.vibe-wise/`
  (a recorded ADR-0009 exception), which Dotfiles adds to the global Git
  ignore list.

### VibeWise SessionStart hook

VibeWise ships its hook only in its Claude plugin, and Agentbot installs
skills, not plugins. So `global/claude/hooks/vibe-wise-session-start.py` is an
adapted copy of upstream's `hooks/session_start.py` (provenance in its header;
the one change points at the installed `learn` skill). Global render installs
it to `~/.claude/hooks/` and keeps exactly one `SessionStart` entry for it in
`~/.claude/settings.json`, leaving every other hook (Boost's included) as it
is. Disabling or removing the `vibe-wise` source removes both again. A file at
that path Agentbot did not write is preserved and never wired. Doctor reports a
missing, stale or left-over hook. It runs `python3` on every session start, is
silent unless the project has an active `.vibe-wise/profile.md`, and writes
nothing. Codex, Cursor and `agy` get the skills only.

## Boost

Dotfiles owns the verified `boost` executable. Agentbot owns Claude, Codex,
and Cursor setup, feature policy, diagnostics, and removal through
`agentbot boost status|setup|off` and manual `agentbot boost analyze SUMMARY.md`.
Setup wires whichever of those CLIs are
installed and skips the rest. A later run picks up a newly installed CLI.

Setup disables both tracing telemetry and upload, restricts owned Boost config
and history paths to private permissions, disables Boost auto-update, and keeps its own
BoostGraph integration (`boost-graph-integration`) off, and writes the declared
policy from `BOOST_FEATURE_POLICY` in `src/boost.py`. Repository
`.boost/config.toml` files can shadow global configuration; Doctor reports
registered workspaces that violate policy.

### BoostGraph

BoostGraph was adopted for code repositories and rejected on 2026-10-08.
Measured in real sessions, `boostgraph_explore`
returned about 4,000 tokens per call against a few hundred for a targeted
`rg` and read, and its schema cost about 950 tokens in every Codex and Cursor
session; it does not index Bash. Its `boost_graph` catalog entry stays as the
record of that review but is not eligible, and the MCP step of every install
and update removes Agentbot's own `agentbot_boost_graph` registration from any
machine that still has it. `boost-graph-integration` stays pinned off, and
Doctor reports any BoostGraph registration or `.codegraph/` index in a
registered workspace. Graphify remains the code-graph tool, rebuilt on demand.

### Feature-flag policy

`BOOST_FEATURE_POLICY` pins the 17 reviewed flags supported by Boost v0.16.6.
Status inspects `boost feature-flags` with remote fetching disabled. Doctor
reports unknown runtime flags even before they appear in saved configuration,
unsupported policy flags, and saved or effective values that differ from policy.
An unavailable runtime inspection does not count as proof of support.
Setup preserves unreviewed flags; a new release needs an explicit policy review.
The retired `boost-mcp-toon-format` user pin is removed; other stored metadata
for that retired entry is preserved. No replacement format is enabled.

| Flag | Policy | Why |
|---|---|---|
| `boost-agent-facing-redaction` | on | Scrubs secrets from what the agent sees; deliberately not retrievable |
| `boost-auto-update` | off (fixed) | Dotfiles owns the binary and its version stamp |
| `boost-claude-status-line` | off (fixed) | Agentbot owns the Claude status line |
| `boost-cli-filtering` | off | Rewrites return allow decisions. Claude can bypass ordinary asks; current Codex approval behavior is separate. Raw shell output is not redacted |
| `boost-english-abbreviation` | off | Lossy prose abbreviation, no gain here |
| `boost-files-optimization` | on | Document and image reads, source untouched |
| `boost-graph-integration` | off | BoostGraph rewrites files Agentbot owns |
| `boost-html-article` | off | Discards markup the agent is reading |
| `boost-html-article-cursor-nudge` | off | Undocumented; points at the disabled HTML path |
| `boost-html-article-preserve-agent-html` | on | Inert while HTML conversion is off; the safer setting if it is turned on |
| `boost-measurement-mode` | off | No matched local comparison has justified enabling it |
| `boost-mcp-browser-budget` | off | A 120-line snapshot cap hides elements browser automation must act on |
| `boost-mcp-filtering` | on | Host-specific replacement; Codex capture alone does not prove compression |
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

"Fixed" flags remain off by the user's decision of 2026-10-08. Tests enforce
that policy. Change the canonical map in `src/boost.py`; UI toggles are overwritten
by the next setup run.

### Hook evidence and static awareness

`ready` means the expected hooks and awareness files are registered and present.
It does not prove that a fresh session delivered hooks or received compressed
output. Status reports seven-day capture evidence separately for each host:
`recent`, `stale`, `none` or `unavailable`. These are read-only timestamp aggregates,
not transcript text. A custom database path currently reports unavailable capture
evidence. Doctor warns when a configured host lacks recent evidence. Verify that
host with a harmless task; do not enable CLI filtering to clear a warning.

Agentbot corrects the vendor awareness body after setup while preserving its
version marker and Cursor frontmatter. For Codex, the global renderer also embeds
Agentbot's canonical awareness in managed `AGENTS.md` while the regular `BOOST.md`
file exists. This avoids relying on an `@BOOST.md` import expander and excludes
stale local awareness text. User-owned instruction files remain untouched.
Ordinary code reads are not document
optimization. CSV and notebook reads can be incomplete previews, including with
`--level none`; use the original file when no retrieval marker exists. Secret
reduction is supplementary. CLI filtering off means raw shell output is outside
Boost's filtering and redaction.

Codex MCP capture does not prove model-facing replacement. Claude and Cursor
fixtures can replace text envelopes; that does not establish delivery for every
host or event. Static awareness is Agentbot-owned. Learned `BOOST.md` is a stored
SQLite digest printed by `boost insights digest`; never redirect it over static
awareness or canonical policy. Accepted durable lessons go through the memory CLI.

Setup pins `[tracing] telemetry=false`, `[tracing] upload=false` and
`[update] auto_update=false`, and writes the user value of reviewed flags.
Owned config/data directories use 0700 and known config/database files use 0600.
Symlinked, multiply linked or foreign-owned storage is rejected. Existing history
is preserved. Other engine, hook, tracking, filter and report settings remain
operator-owned. A registered repository config must contain all three safety
controls because it replaces the global config rather than merging with it.

### Manual Engine analysis

Use a short summary that you have reviewed and redacted. This command does not
read the live transcript history:

```bash
agentbot boost analyze SUMMARY.md          # preview; sends nothing
agentbot boost analyze SUMMARY.md --yes    # one native Codex invocation; consumes quota
```

Confirmed analysis requires an already accepted Boost configuration, a genuine
local Pro grant and an existing file-based Codex ChatGPT login. It accepts one
regular UTF-8 file up to 8192 bytes and rejects credential findings. The complete
vendor prompt is also checked and capped at 16384 bytes. This best-effort scanner
is not a substitute for reviewing the selected text before sending it.

The disposable HOME/CODEX_HOME contains only that selected summary and the private
grant. Native authentication uses the existing credential file; it is never
printed or included in the prompt. The native command ignores user config, uses
`gpt-6.1-sol` with low reasoning and a read-only sandbox, and disables shell,
hooks, apps, plugins, remote plugins, subagents, image tools, sleep and tool
suggestions. Web search is disabled. No configured MCP servers are inherited.
The client runs ephemerally. A changed feature contract fails before analysis.
[Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

An exclusive guard permits one Codex process. The adapter never retries, and a
90-second timeout stops the process group. Ctrl+C and SIGTERM also clean up.
Vendor temporary files stay under the private profile. Abrupt SIGKILL or host
failure cannot run cleanup. Native transport/auth retries are client behavior;
one process is not a guarantee of one HTTP request. Input, time and invocation
limits are not a hard token or dollar cap.

The result is a lesson proposal plus native usage counters. It does not modify
canonical instructions, static awareness or memory. The native context includes
more than the selected text, so use actual usage to assess cost. Do not use the
stock dashboard analysis or filter-generation buttons as a substitute for this
boundary. No unattended analysis is scheduled or enabled by Agentbot.

No local runtime or model is installed. The current WSL resource observation does
not justify the Gemma package; Julia has no adopted decision-runtime use here.
Keep Code Index, HTML conversion, browser truncation, CLI filtering and automatic
skill removal off. Reopen those decisions only for a concrete, measured need.

**Setup accepts Boost's preview terms on your behalf.** `boost init` is passed
`--accept-terms`, which accepts the JFrog Online Preview Agreement and Privacy
Notice without prompting. This is a deliberate choice recorded here rather than
a prompt suppressed quietly: on a fresh machine the unanswered prompt sat for
the full command timeout and then failed the whole Agentbot install. Boost
stores the acceptance in its own `~/.boost/config.toml`, so it only ever
applies the first time on a given machine. Selecting the Boost CLI component is
what opts you into this; if you would rather accept it yourself, remove
`--accept-terms` from `BoostIntegration.setup` and answer the prompt.
