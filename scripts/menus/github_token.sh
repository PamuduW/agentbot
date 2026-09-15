# shellcheck shell=bash
# shellcheck disable=SC2034  # The GITHUB_TOKEN_MENU_* settings are this file's published output, read by the shared screen.
# Agentbot binding for the shared GitHub token screen.
#
# The screen itself lives in the dotfiles-shared repository, which this CLI and
# the Dotfiles installer both resolve at runtime. This file supplies only the
# Agentbot identity, and keeps the entry point names Agentbot's callers use.
#
# Agentbot addresses the terminal through its own AGENTBOT_* seam, so it maps
# that onto the shared names and hands the screen tui_refresh_tty_seam: an
# inherited descriptor is only correct once the seam pointing at it is current.

GITHUB_TOKEN_MENU_ROOT=Agentbot
GITHUB_TOKEN_MENU_COLS_FN=tui_cols
GITHUB_TOKEN_MENU_REFRESH_FN=tui_refresh_tty_seam

if [[ -z "${DOTFILES_SHARED_LIB:-}" ]]; then
	# shellcheck source=scripts/lib/shared_resolve.sh
	source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../lib" && pwd)/shared_resolve.sh"
	dotfiles_shared_require "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)" || return 1
fi
# shellcheck source=/dev/null
source "$DOTFILES_SHARED_LIB/github_token_menu.sh"

# Agentbot's callers use these names; the shared screen is github_token_menu.
#
# The AGENTBOT_TOKEN_TTY_* overrides are mapped here rather than at source time:
# a caller sets them per invocation, and reading them when this file loaded
# captured whatever they were before the run began -- which is nothing. `local`
# is dynamic scope, so the screen sees them for the length of the call.
agentbot_token_config_menu() {
	# shellcheck disable=SC2034  # Read through dynamic scope by the screen.
	local GITHUB_TOKEN_TTY_INPUT="${AGENTBOT_TOKEN_TTY_INPUT:-}"
	# shellcheck disable=SC2034  # Read through dynamic scope by the screen.
	local GITHUB_TOKEN_TTY_OUTPUT="${AGENTBOT_TOKEN_TTY_OUTPUT:-}"
	# shellcheck disable=SC2034  # Read through dynamic scope by the screen.
	local GITHUB_TOKEN_TTY_COLS="${AGENTBOT_TOKEN_TTY_COLS:-}"
	github_token_menu
}

agentbot_menu_token() {
	tui_menu_declare_owns_pause
	agentbot_token_config_menu
}
