### Checklist: dotfiles-shared

The shared library holds the Bash and Python code both Dotfiles and Agentbot
load at runtime. A change here reaches both products. Paths are relative to
its checkout.

1. **The contract.** `CONTRACT` and `docs/consuming.md` here, and in each
   consumer (Dotfiles and Agentbot) its `shared_resolve.sh` and
   `check_shared_contract.sh`: the version both consumers require must match,
   and a change that is not backward compatible must have raised it.
2. **Credentials and prompts.** `scripts/lib/shared/github_token.sh`,
   `github_token_menu.sh`, `askpass.sh`, `sudo_prime.sh`: a token or password
   must never reach argv, a log, the terminal title or a world-readable file;
   input must be hidden; the sudo cache must not outlive the run.
3. **Repository gate.** `scripts/lib/shared/repo_gate.sh`, `repo_update.sh`:
   fast-forward only, dirty or diverged stops, and no path where a failed
   fetch reads as "up to date".
4. **Python service.** `scripts/lib/shared/py_service.sh` and
   `scripts/lib/shared/python/*.py`: the Bash and Python halves must give the
   same answer for the same input (each consumer has parity tests); look for
   a request format change made on one side only.
5. **Terminal UI.** `scripts/lib/shared/tui/`: anything that reads from or
   writes to the terminal in a way that breaks when there is no terminal
   (CI, a pipe, an agent's shell).
6. **Validation and CI.** `.github/workflows/validate.yml`: does it test
   both consumers against this branch, with Actions pinned to commit SHAs?
