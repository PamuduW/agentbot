#!/usr/bin/env bash
# The vendored copy is gone, so there is no drift to check. What can still go
# wrong is version skew: this checkout and the dotfiles-shared checkout beside
# it moving independently. Both resolvers -- Bash and Python -- must agree that
# the CONTRACT revision they require is the one on disk.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=scripts/lib/shared_resolve.sh
source "$ROOT/scripts/lib/shared_resolve.sh"
dotfiles_shared_require "$ROOT" || exit 1

python_required="$("${AGENTBOT_PYTHON:-python3}" -c \
	'import sys; sys.path.insert(0, sys.argv[1]); from src.shared_paths import CONTRACT_REQUIRED; print(CONTRACT_REQUIRED)' \
	"$ROOT")"

if [[ "$python_required" != "$DOTFILES_SHARED_CONTRACT_REQUIRED" ]]; then
	printf '  Error: the two resolvers disagree: shell requires %s, Python requires %s.\n' \
		"$DOTFILES_SHARED_CONTRACT_REQUIRED" "$python_required" >&2
	exit 1
fi

printf 'Shared CONTRACT %s satisfied by %s\n' \
	"$DOTFILES_SHARED_CONTRACT_REQUIRED" "$DOTFILES_SHARED_ROOT"
