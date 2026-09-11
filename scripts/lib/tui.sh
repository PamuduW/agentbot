#!/usr/bin/env bash
# shellcheck shell=bash
# shellcheck disable=SC1091,SC2034  # Loader paths are rooted beside this file; palette globals are published.

# Agentbot TUI.
#
# The implementation is the shared terminal stack in scripts/lib/shared/tui/,
# which is kept byte-identical with the sibling repository (see
# scripts/sync-shared.sh; the gate fails if the copies diverge). This file is
# the Agentbot-facing naming layer over it, so existing tui_* callers and the
# AGENTBOT_* environment seams keep working.

_AGENTBOT_TUI_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/shared/tui" && pwd)"

# One TTY adapter, two addressing forms. Preserve the caller's Dotfiles seam
# before resolving Agentbot overrides: refresh must restore that stable base
# after a scoped token/menu override ends.
_AGENTBOT_TUI_BASE_INPUT="${DOTFILES_TTY_INPUT:-/dev/tty}"
_AGENTBOT_TUI_BASE_OUTPUT="${DOTFILES_TTY_OUTPUT:-/dev/tty}"
_AGENTBOT_TUI_BASE_IN_FD="${DOTFILES_TTY_IN_FD:-}"
_AGENTBOT_TUI_BASE_OUT_FD="${DOTFILES_TTY_OUT_FD:-}"
DOTFILES_TTY_INPUT="${AGENTBOT_TUI_INPUT:-$_AGENTBOT_TUI_BASE_INPUT}"
DOTFILES_TTY_OUTPUT="${AGENTBOT_TUI_OUTPUT:-$_AGENTBOT_TUI_BASE_OUTPUT}"
DOTFILES_TTY_IN_FD="${AGENTBOT_TUI_IN_FD:-$_AGENTBOT_TUI_BASE_IN_FD}"
DOTFILES_TTY_OUT_FD="${AGENTBOT_TUI_OUT_FD:-$_AGENTBOT_TUI_BASE_OUT_FD}"
export DOTFILES_TTY_INPUT DOTFILES_TTY_OUTPUT DOTFILES_TTY_IN_FD DOTFILES_TTY_OUT_FD

source "$_AGENTBOT_TUI_DIR/colors.sh"
source "$_AGENTBOT_TUI_DIR/tty.sh"
source "$_AGENTBOT_TUI_DIR/menu_render.sh"
source "$_AGENTBOT_TUI_DIR/report_table.sh"
source "$_AGENTBOT_TUI_DIR/ui.sh"
source "$_AGENTBOT_TUI_DIR/menu_descriptions.sh"
source "$_AGENTBOT_TUI_DIR/menu_keys.sh"
source "$_AGENTBOT_TUI_DIR/menu_simple.sh"
source "$_AGENTBOT_TUI_DIR/menu_paging.sh"
source "$_AGENTBOT_TUI_DIR/menu_checkbox.sh"
source "$_AGENTBOT_TUI_DIR/menu_runner.sh"

# Submenus accept `q` to go back and never said so, while the sibling
# repository's submenus have always advertised it. Same shared runner, two
# different levels of discoverability -- roadmap 4.1 asks for navigation
# consistency, and this is the whole of the gap.
: "${MENU_SUBMENU_HINT:=Up/Down navigate   Enter confirm   q back}"
export MENU_SUBMENU_HINT

# Re-resolve the TTY seam after a caller changes AGENTBOT_TUI_INPUT/OUTPUT
# mid-process (the token and workspace menus do this to capture output).
tui_refresh_tty_seam() {
	DOTFILES_TTY_INPUT="${AGENTBOT_TUI_INPUT:-$_AGENTBOT_TUI_BASE_INPUT}"
	DOTFILES_TTY_OUTPUT="${AGENTBOT_TUI_OUTPUT:-$_AGENTBOT_TUI_BASE_OUTPUT}"
	DOTFILES_TTY_IN_FD="${AGENTBOT_TUI_IN_FD:-$_AGENTBOT_TUI_BASE_IN_FD}"
	DOTFILES_TTY_OUT_FD="${AGENTBOT_TUI_OUT_FD:-$_AGENTBOT_TUI_BASE_OUT_FD}"
	export DOTFILES_TTY_INPUT DOTFILES_TTY_OUTPUT DOTFILES_TTY_IN_FD DOTFILES_TTY_OUT_FD
	menu_tty_invalidate_size
}

tui_init_colors() { ui_init_colors; }
tui_init_colors

# Agentbot reads its width override from AGENTBOT_MENU_COLS.
tui_cols() {
	if [[ -n "${AGENTBOT_MENU_COLS:-}" ]]; then
		printf '%s\n' "$AGENTBOT_MENU_COLS"
		return 0
	fi
	menu_tty_cols
}

tui_fit() { menu_fit_line "$1" "$(($2 + 1))"; }
tui_clear() { ui_clear; }
tui_header() { ui_print_header "$1" "${2:-}" "${3:-$(tui_cols)}"; }
tui_section() { ui_print_section "$1" "${2:-$(tui_cols)}"; }
tui_color_input_hint() { ui_color_input_hint "$1"; }
tui_print() {
	tui_refresh_tty_seam
	tty_printf "$@"
}

_tui_color_backend_line() {
	local line="$1" terminator="${2-$'\n'}"
	line="${line//\[STEP\]/${C_BOLD}${C_CYAN}[STEP]${C_RESET}}"
	line="${line//\[OK\]/${C_BOLD}${C_GREEN}[OK]${C_RESET}}"
	# Dim, matching the result vocabulary: a skip is a deliberate non-event.
	# Missing here while the backend prints [SKIP] for a deselected component,
	# so the one marker meaning "nothing happened" was the one drawn plainest.
	line="${line//\[SKIP\]/${C_DIM}[SKIP]${C_RESET}}"
	line="${line//\[FAIL\]/${C_BOLD}${C_RED}[FAIL]${C_RESET}}"
	line="${line//\[ERR\]/${C_BOLD}${C_RED}[ERR]${C_RESET}}"
	line="${line//\[WARN\]/${C_BOLD}${C_YELLOW}[WARN]${C_RESET}}"
	line="${line//\[INFO\]/${C_CYAN}[INFO]${C_RESET}}"
	# The legend names the four markers without bracketing them, so the
	# substitutions above cannot reach it. Coloured word by word instead, which
	# is the whole point of a legend: the colour is the key.
	if [[ "$line" == *'[Legend]'* ]]; then
		line="${line//STEP=starting/${C_CYAN}STEP=starting${C_RESET}}"
		line="${line//OK=completed/${C_GREEN}OK=completed${C_RESET}}"
		line="${line//SKIP=already satisfied/${C_DIM}SKIP=already satisfied${C_RESET}}"
		line="${line//WARN=needs attention/${C_YELLOW}WARN=needs attention${C_RESET}}"
	fi
	printf '%s%s' "$line" "$terminator"
}

_tui_color_backend_stream() {
	local line=''
	while IFS= read -r line; do
		_tui_color_backend_line "$line" $'\n'
	done
	[[ -z "$line" ]] || _tui_color_backend_line "$line" ''
}

tui_run_to_output() {
	local -a pipeline_status
	tui_refresh_tty_seam
	if tty_use_output_fd; then
		"$@" 2>&1 | _tui_color_backend_stream >&"$DOTFILES_TTY_OUT_FD"
		pipeline_status=("${PIPESTATUS[@]}")
	else
		local output_path
		tty_output_available || return 1
		output_path="$(tty_output_path)"
		"$@" 2>&1 | _tui_color_backend_stream >>"$output_path"
		pipeline_status=("${PIPESTATUS[@]}")
	fi
	if ((pipeline_status[0] != 0)); then
		return "${pipeline_status[0]}"
	fi
	return "${pipeline_status[1]}"
}
tui_pause() {
	tui_refresh_tty_seam
	ui_pause
}
tui_wait_back() {
	tui_refresh_tty_seam
	ui_wait_back
}
tui_redraw_up() { menu_redraw_up "$1"; }

tui_confirm() {
	tui_refresh_tty_seam
	ui_confirm_yes_no "$1" true
}

# --- Menu ---------------------------------------------------------------
#
# menu_simple_run comes from the shared stack and reports its choice in
# MENU_SIMPLE_RESULT. These remain for the Agentbot suites that assert on
# geometry directly.

# agentbot_menu_run: the interactive selection, in Python.
#
# ADR-0001. The Dotfiles menus cannot move -- they draw before that machine has
# an interpreter -- but this repository is not installed until python3 and
# PyYAML exist, so its menus can. One process per menu display, not one per
# keystroke: Python owns the loop and returns the chosen key, and dispatch stays
# here.
#
# The shared Bash loop remains the fallback and is not modified: scripts/lib/
# shared/tui is byte-identical with Dotfiles, and a Python path in there would
# put Python back on the pre-install path the boundary keeps clear.
#
# Reports its choice in MENU_SIMPLE_RESULT and returns non-zero when the
# operator cancelled, which is menu_simple_run's contract exactly.
# Shared code that runs a menu takes the loop from here rather than naming one,
# so menu_runner.sh stays language-neutral and Dotfiles keeps the Bash default.
MENU_SIMPLE_RUNNER=agentbot_menu_run

# With a name, the menu is defined in src/ui/menus.py and nothing here builds
# it. Without one, the caller has already filled MENU_SIMPLE_* -- which is how
# the menus whose entries are computed at runtime still work.
agentbot_menu_run() {
	local name="${1:-}" choice rc=0
	local -a args=()

	if ! _agentbot_python_available; then
		# Only a caller-built menu can fall back: a named one is defined on the
		# other side of this call. That is not a gap worth closing by writing
		# the definitions twice -- every action behind these menus runs the
		# Python CLI, so a machine that cannot run this cannot run them either.
		if [[ -z "$name" ]]; then
			menu_simple_run
			return $?
		fi
		printf '  %sThe menu needs python3, which is not available.%s\n' \
			"${C_RED:-}" "${C_RESET:-}" >&2
		return 1
	fi

	args=(--cols "$(tui_cols)")
	[[ -n "${C_RESET:-}" ]] && args+=(--color)
	[[ -n "$name" ]] && args+=(--menu "$name")

	if [[ -n "$name" ]]; then
		choice="$(_agentbot_menu_python "${args[@]}" </dev/null)" || rc=$?
	else
		choice="$(_agentbot_menu_spec | _agentbot_menu_python "${args[@]}")" || rc=$?
	fi

	if ((rc == 3)); then
		if [[ -z "$name" ]]; then
			menu_simple_run
			return $?
		fi
		return 1
	fi
	if ((rc != 0)); then
		MENU_SIMPLE_RESULT=''
		return 1
	fi
	MENU_SIMPLE_RESULT="$choice"
}

# agentbot_checkbox_run: the checkbox list, in Python.
#
# Reads and writes the same MENU_CB_* globals menu_checkbox_run does, so a
# caller changes one word. The Bash loop still answers when there is no python3,
# and when the caller uses the callback hooks -- MENU_CB_TOGGLE_FN and its
# siblings are Bash functions, and a loop in another process cannot call them.
# Neither Agentbot checkbox uses them; the Dotfiles component selector does, and
# it stays on the Bash loop for that reason as well as the boundary one.
agentbot_checkbox_run() {
	local result rc=0 index
	local -a flags=()

	if ! _agentbot_python_available ||
		[[ -n "${MENU_CB_TOGGLE_FN:-}${MENU_CB_ALL_FN:-}${MENU_CB_NONE_FN:-}${MENU_CB_DESC_FN:-}" ]]; then
		menu_checkbox_run
		return $?
	fi

	local -a args=(--checkbox --cols "$(tui_cols)")
	[[ -n "${C_RESET:-}" ]] && args+=(--color)

	result="$(_agentbot_checkbox_spec | _agentbot_menu_python "${args[@]}")" || rc=$?
	if ((rc == 3)); then
		menu_checkbox_run
		return $?
	fi
	((rc == 0)) || return 1

	IFS=',' read -r -a flags <<<"$result"
	((${#flags[@]} == ${#MENU_CB_LABELS[@]})) || return 1
	for index in "${!flags[@]}"; do
		MENU_CB_CHECKED[index]="${flags[$index]}"
	done
}

_agentbot_checkbox_spec() {
	local fs=$'\x1f' nl=$'\x1e' item index
	local color=0

	[[ -n "${C_RESET:-}" ]] && color=1
	printf 'title%s%s\n' "$fs" "${MENU_CB_TITLE:-}"
	printf 'breadcrumb%s%s\n' "$fs" "${MENU_CB_BREADCRUMB:-}"
	printf 'hint%s%s\n' "$fs" "${MENU_CB_HINT:-}"
	printf 'cols%s%s\n' "$fs" "$(tui_cols)"
	printf 'rows%s%s\n' "$fs" "$(menu_tty_rows)"
	printf 'color%s%s\n' "$fs" "$color"
	printf 'compact%s%s\n' "$fs" "$([[ "${MENU_CB_COMPACT:-false}" == true ]] && printf 1 || printf 0)"
	for item in "${MENU_CB_LABELS[@]}"; do
		printf 'label%s%s\n' "$fs" "${item//$'\n'/$nl}"
	done
	for index in "${!MENU_CB_LABELS[@]}"; do
		printf 'status%s%s\n' "$fs" "${MENU_CB_STATUS[$index]:-}"
		printf 'checked%s%s\n' "$fs" "${MENU_CB_CHECKED[$index]:-0}"
	done
	for item in ${MENU_CB_DESCS[@]+"${MENU_CB_DESCS[@]}"}; do
		printf 'desc%s%s\n' "$fs" "${item//$'\n'/$nl}"
	done
}

# agentbot_menu_load <name>: fill MENU_SIMPLE_* from a named definition, for the
# Bash that still reads those globals directly.
agentbot_menu_load() {
	local name="$1" line field value
	MENU_SIMPLE_TITLE=''
	MENU_SIMPLE_BREADCRUMB=''
	MENU_SIMPLE_LABELS=()
	MENU_SIMPLE_KEYS=()
	MENU_SIMPLE_TYPES=()
	MENU_SIMPLE_DESCS=()

	_agentbot_python_available || return 1
	while IFS= read -r line; do
		field="${line%%$'\x1f'*}"
		value="${line#*$'\x1f'}"
		value="${value//$'\x1e'/$'\n'}"
		case "$field" in
		title) MENU_SIMPLE_TITLE="$value" ;;
		breadcrumb) MENU_SIMPLE_BREADCRUMB="$value" ;;
		label) MENU_SIMPLE_LABELS+=("$value") ;;
		key) MENU_SIMPLE_KEYS+=("$value") ;;
		type) MENU_SIMPLE_TYPES+=("$value") ;;
		desc) MENU_SIMPLE_DESCS+=("$value") ;;
		esac
	done < <(_agentbot_menu_python --menu "$name" --dump </dev/null)
	((${#MENU_SIMPLE_LABELS[@]} > 0))
}

_agentbot_python_available() {
	command -v python3 >/dev/null 2>&1 && [[ -d "${AGENTBOT_HOME:-}/src/ui" ]]
}

_agentbot_menu_python() {
	(cd "$AGENTBOT_HOME" && PYTHONDONTWRITEBYTECODE=1 python3 -m src.ui.menu_select "$@")
}

# agentbot_submenu_loop <menu-name> <dispatch-fn>
#
# tui_submenu_loop's shape, taking a defined menu instead of a setup function.
# The shared one calls its setup on every pass, which for a named menu would
# mean fetching the definition once per redraw on top of running it -- two
# processes per display where the point of the port was one.
agentbot_submenu_loop() {
	local name="$1" dispatch_fn="$2" choice rc
	while true; do
		if ! agentbot_menu_run "$name"; then
			return 0
		fi
		choice="${MENU_SIMPLE_RESULT:-}"
		[[ "$choice" == back || "$choice" == quit ]] && return 0
		ui_clear
		rc=0
		"$dispatch_fn" "$choice" || rc=$?
		((rc == 0)) || ui_pause
	done
}

# The menu as `name<FS>value` lines. No quoting to get wrong, and a description
# with newlines travels as \x1e -- the same transport the probe classifier uses.
_agentbot_menu_spec() {
	local fs=$'\x1f' nl=$'\x1e' item
	local color=0

	[[ -n "${C_RESET:-}" ]] && color=1
	printf 'title%s%s\n' "$fs" "${MENU_SIMPLE_TITLE:-}"
	printf 'breadcrumb%s%s\n' "$fs" "${MENU_SIMPLE_BREADCRUMB:-}"
	printf 'hint%s%s\n' "$fs" "${MENU_SIMPLE_HINT:-}"
	printf 'cols%s%s\n' "$fs" "$(tui_cols)"
	printf 'color%s%s\n' "$fs" "$color"
	for item in "${MENU_SIMPLE_LABELS[@]}"; do
		printf 'label%s%s\n' "$fs" "${item//$'\n'/$nl}"
	done
	for item in "${MENU_SIMPLE_KEYS[@]}"; do
		printf 'key%s%s\n' "$fs" "$item"
	done
	for item in ${MENU_SIMPLE_TYPES[@]+"${MENU_SIMPLE_TYPES[@]}"}; do
		printf 'type%s%s\n' "$fs" "$item"
	done
	for item in ${MENU_SIMPLE_DESCS[@]+"${MENU_SIMPLE_DESCS[@]}"}; do
		printf 'desc%s%s\n' "$fs" "${item//$'\n'/$nl}"
	done
}
tui_menu_lines() { _menu_simple_menu_lines "${#MENU_SIMPLE_LABELS[@]}"; }
tui_menu_draw() { _menu_simple_draw "$1" "${2:-$(tui_cols)}"; }

# Submenu helpers are defined by the shared menu_runner.sh:
#   tui_submenu_loop, tui_menu_declare_owns_pause
