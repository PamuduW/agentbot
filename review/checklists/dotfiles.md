### Checklist: dotfiles

The Dotfiles repository installs and updates every tool on the machine and
deploys the shell configuration. Paths are relative to its checkout.

1. **Bootstrap and install entry points.** `bootstrap.sh`, `install.sh`,
   `scripts/install.sh`. Check that a fresh machine run fails closed on
   errors (`set -euo pipefail` honoured, no `|| true` hiding a failed
   install), that nothing pipes a downloaded script straight into a shell,
   and that the component selection the operator makes is the one that runs.
2. **Component registry and lifecycle.** `scripts/lib/components/registry.sh`,
   `probes.sh`, `install_dispatch.sh`, `plan.sh`. Every component key must have
   a probe, an install function and, where it updates, an update step; a
   probe must report what is really installed and who owns it (a copy
   Dotfiles did not install must be reported as external, never replaced).
   Look for keys present in one table and missing from another.
3. **Downloads and integrity.** `scripts/lib/installers/*.sh` (including
   `github_release.sh`, `release_cli.sh`, `boost.sh`, `antigravity.sh`,
   `obsidian.sh`, `fonts.sh`, `cli_tools.sh`, `remote_script.sh`) and
   `scripts/lib/github_api.sh`. For every download: is it HTTPS, is it
   verified against a checksum or digest the publisher signs or publishes,
   is an archive member that escapes its directory refused, does a failed
   verification leave the old binary in place, and does it go through the
   `github_curl` boundary so a GitHub token never reaches argv or logs?
   Compare each tool's install method with what its vendor currently
   recommends.
4. **Updates.** `scripts/lib/updates/*.sh`, `scripts/lib/update_components.sh`,
   `scripts/lib/update_workflow.sh`, `scripts/lib/full_update.sh`. Does the
   preview (installed, available, action) match what the apply step does?
   Does `full-update` respect a deselected component? Does a failed step
   stop or continue, and is that what the docs say?
5. **Repository gate.** `scripts/lib/repo_update.sh` and
   `scripts/lib/shared_resolve.sh`: pulls must be fast-forward only, a dirty
   or diverged checkout must stop the run, and the shared library must be
   resolved to the version its `CONTRACT` expects.
6. **Shell configuration.** `bash/.bashrc`, `bash/.bash_aliases`,
   `readline/`. Look for exported secrets or tokens, `PATH` entries that let a
   Windows binary shadow a Linux one (inside WSL, `cursor` and `code` on
   `PATH` can be Windows programs), and environment variables that silently
   turn a safety feature off or on for every shell.
7. **The Git wrapper.** `bin/bin/git` and `tests/test_git_wrapper.sh`: the
   guarded commands (`commit`, `add`, `clone`, `sub add`) must be caught
   through aliases too, commit-time pulls must stay fast-forward only, and
   any failure must stop the command rather than let it through.
8. **The `dotfiles` command.** `bin/bin/dotfiles`: each subcommand's
   behaviour against `docs/` and `README.md`; read-only commands (`status`,
   `doctor`) must not write.
9. **Packages.** `packages/packages.txt`: packages that are no longer
   needed, renamed in the current Ubuntu release, or duplicated by a
   component installed another way.
10. **Validation and CI.** `scripts/validate.sh`, `tests/run.sh`,
    `.github/workflows/validate.yml`: every Action pinned to a full commit
    SHA, least-privilege `permissions:`, no secrets in logs, and checks that
    actually fail when something is wrong.
11. **Windows side.** `windows/`: anything it changes outside WSL, and
    whether that is documented.
