### Checklist: agentbot

Agentbot configures the four agent CLIs: skills, policy files, MCP servers,
CLI settings, Boost integration and the memory vault. Paths are relative to
its checkout.

1. **Lifecycle.** `install.sh`, `bin/agentbot`, `src/lifecycle.py`,
   `src/cli.py`: install and update must preview before they write, honour a
   component selection, and roll back or report cleanly when a step fails.
2. **Skills.** `skills.sources.yaml`, `src/skills_sources.py`,
   `src/skills_installer.py`, `src/skill_prune.py`, `src/skill_reconcile.py`:
   is a source fetched over HTTPS and recorded by commit; is a skill the
   operator customised or installed by hand ever overwritten or deleted; do
   `exclude:` entries keep a skill off disk; are test fixtures or stubs
   swept in?
3. **Policy files.** `global/AGENTS.md`, `base/`, `agentos.yaml`,
   `src/render.py`, `src/workspace_render.py`, `src/workspace_service.py`:
   rendered files must change only inside their managed block, and the
   global policy must not contradict a repository's own `AGENTS.md`.
4. **MCP servers.** `mcp/catalog.json`, `mcp/contracts/`, `src/mcp_*.py`,
   `src/github_mcp_bridge.py`, `src/gitlab_read_*.py`: each server's
   descriptor pinned and checked, tokens kept out of argv and logs, write
   tools withheld where the catalog says read-only, and the same server set
   registered in all four clients.
5. **Agent CLI settings.** `cli/*`, `src/cli_config.py`: every declared key
   still read by its CLI's current version (check each CLI's settings
   documentation and changelog), no credential-shaped value, and no setting
   that approves actions on the operator's behalf without that being a
   recorded decision.
6. **Boost integration.** `src/boost.py` (`BOOST_FEATURE_POLICY`) and
   `docs/skills.md`: every flag Boost currently ships pinned with a reason;
   what Boost's hooks do in each client, and whether any hook answers a
   permission request for the operator; telemetry and network behaviour.
7. **Memory.** `src/memory*.py`: the secret scanner, the sync engine's
   handling of conflicts and partial writes, and that nothing but the CLI
   writes the vault. Do not open the vault itself.
8. **Credentials.** `src/gitlab_token.py`, `scripts/lib/github_token.sh`,
   `src/command_runner.py`: where each token is stored, its file mode, that
   it never reaches argv, logs or output, and that a token with more scope
   than documented is refused.
9. **Diagnostics.** `src/diagnostics.py`, the `status` and `doctor` output in
   the facts above: a check that reports ok without checking, or a real
   problem doctor does not report.
10. **Setup review.** `src/setup_review.py` and `review/`: the code that ran
    this review; check it with the same care as the rest.
11. **Validation and CI.** `tests/run.sh`, `.github/workflows/test.yml`,
    `pyproject.toml`: Actions pinned to commit SHAs, least privilege, and
    checks that fail when they should.
