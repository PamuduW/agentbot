# Validation

Run the complete repository gate from the Agentbot checkout:

```bash
env -u NO_COLOR bash tests/run.sh
```

The gate runs Ruff and mypy, the Python suite under coverage, the shell suites,
Bash syntax, ShellCheck and shfmt, `zizmor` over the workflows, the
shared-contract check, and `git diff --check`. A missing tool fails the gate
unless `AGENTBOT_ALLOW_SKIP=1`.

The Python suite runs in up to four parallel shards, balanced by file size
(`AGENTBOT_TEST_JOBS` sets the number); a passing test's output is buffered
and a failing shard's is printed in full. The seeded memory stress test, about
a minute and a half on its own, is a slow lane: it runs when
`AGENTBOT_SLOW_TESTS=1`, which CI sets on its daily and dispatched runs but not
on a push. Run it locally before changing the sync engine:

```bash
AGENTBOT_SLOW_TESTS=1 env -u NO_COLOR bash tests/run.sh
```

CI runs the gate on every push to `main` and `development`, on Python 3.10,
and daily on 3.10 and 3.12 with the slow lane.

It needs a `dotfiles-shared` checkout: Agentbot resolves it as a sibling, at
`$HOME/dotfiles-shared`, or wherever `DOTFILES_SHARED_DIR` points. The
`Shared contract` step fails early and names the revision mismatch if the two
have drifted apart, rather than letting a suite die on a missing source.

## Complexity limits are a ratchet

`C901`, `PLR0912` and `PLR0915` are in the Ruff selection with their limits set
to the worst function in each category at the time they were added — 24
branches of complexity, 23 branches, 103 statements. Nothing existing had to be
rewritten, and nothing new may exceed them.

They are not a target. Twenty-one functions sit above the conventional
complexity of 10, and the worst of them — `apply_reconcile_plan`,
`_handle_gitlab_token`, `apply_prune` — are the reconciliation, token
validation and removal paths, where a refactor trades behaviour for a number.
Lower a limit when a function that set it is split for a reason of its own;
raising one is the thing the ratchet exists to stop.

To match CI's Ruff and coverage checks, install development tools locally:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
PATH="$PWD/.venv/bin:$PATH" env -u NO_COLOR bash tests/run.sh
```

Useful focused checks are:

```bash
python3 -m unittest discover -s tests
python3 -m unittest tests.test_cli
bash tests/shell/test_public_commands.sh
bash tests/test_menu_parity.sh
./install.sh doctor
```

Suite names under `tests/` and `tests/shell/` are authoritative; list the
directories rather than trusting a name quoted here. Doctor is a runtime
diagnostic and does not replace the repository gate.
