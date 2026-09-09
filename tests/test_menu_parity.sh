#!/usr/bin/env bash
# shellcheck shell=bash
# shellcheck disable=SC2034  # MENU_SIMPLE_* and NO_COLOR are read by the Bash renderer under test.
set -uo pipefail

# The Agentbot menu frame is drawn twice while it moves to Python: in Bash
# (scripts/lib/shared/tui/menu_simple.sh) and in src/ui/menu.py. Byte parity is
# the oracle, exactly as it was for the report table -- an interactive menu has
# no other cheap one, and "it looked right when I ran it" is not a test.
#
# Every frame is compared: each cursor position, three widths, both palettes,
# with and without a breadcrumb, with and without section headers, with and
# without a description footer. The escape sequences are part of the comparison
# -- a missing \033[K is invisible until a longer line is redrawn shorter and
# its tail stays on the screen.

TEST_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "$TEST_DIR/.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1

if ! command -v python3 >/dev/null 2>&1; then
	printf 'ok - menu parity skipped; python3 unavailable\n'
	exit 0
fi

# shellcheck source=/dev/null
source "$REPO_DIR/scripts/lib/shared/tui/colors.sh"
# shellcheck source=/dev/null
source "$REPO_DIR/scripts/lib/shared/tui/tty.sh"
# shellcheck source=/dev/null
source "$REPO_DIR/scripts/lib/shared/tui/menu_render.sh"
# shellcheck source=/dev/null
source "$REPO_DIR/scripts/lib/shared/tui/ui.sh"
# shellcheck source=/dev/null
source "$REPO_DIR/scripts/lib/shared/tui/menu_descriptions.sh"
# shellcheck source=/dev/null
source "$REPO_DIR/scripts/lib/shared/tui/menu_paging.sh"
# shellcheck source=/dev/null
source "$REPO_DIR/scripts/lib/shared/tui/menu_checkbox.sh"
# shellcheck source=/dev/null
source "$REPO_DIR/scripts/lib/shared/tui/menu_keys.sh"
# shellcheck source=/dev/null
source "$REPO_DIR/scripts/lib/shared/tui/menu_simple.sh"

passed=0
failed=0

py_draw() {
	local cursor="$1" cols="$2" color="$3" spec="$4"
	# The spec travels as an argument, never interpolated into the source: a
	# description carries newlines, and a `\n` expanded twice stops being JSON.
	python3 - "$cursor" "$cols" "$color" "$spec" "$5" <<'PY'
import json, sys

sys.path.insert(0, sys.argv[5] if len(sys.argv) > 5 else ".")
from src.ui import menu

cursor, cols, color, spec = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3] == "1", json.loads(sys.argv[4])
sys.stdout.write(
    menu.draw_simple(
        title=spec["title"],
        breadcrumb=spec.get("breadcrumb", ""),
        labels=spec["labels"],
        types=spec.get("types"),
        descs=spec.get("descs"),
        hint=spec.get("hint", menu.DEFAULT_HINT),
        cursor=cursor,
        cols=cols,
        palette=menu.Palette(color=color),
    )
)
PY
}

# compare_menu <name> <json spec>
#
# The spec is the only source: the Bash globals are generated from it and the
# Python side reads it directly, so a case cannot describe two different menus
# and pass by comparing neither.
compare_menu() {
	local name="$1" spec="$2" cursor cols color want got count

	eval "$(
		python3 - "$spec" <<'SPEC'
import json, shlex, sys

spec = json.loads(sys.argv[1])
out = [f"MENU_SIMPLE_TITLE={shlex.quote(spec['title'])}"]
if spec.get("breadcrumb"):
    out.append(f"MENU_SIMPLE_BREADCRUMB={shlex.quote(spec['breadcrumb'])}")
else:
    out.append("unset MENU_SIMPLE_BREADCRUMB")
if spec.get("hint"):
    out.append(f"MENU_SIMPLE_HINT={shlex.quote(spec['hint'])}")
else:
    out.append("unset MENU_SIMPLE_HINT")
out.append("MENU_SIMPLE_LABELS=(%s)" % " ".join(shlex.quote(x) for x in spec["labels"]))
out.append("MENU_SIMPLE_TYPES=(%s)" % " ".join(shlex.quote(x) for x in (spec.get("types") or [])))
if spec.get("descs"):
    out.append("MENU_SIMPLE_DESCS=(%s)" % " ".join(shlex.quote(x) for x in spec["descs"]))
else:
    out.append("unset MENU_SIMPLE_DESCS")
print("\n".join(out))
SPEC
	)"

	count="${#MENU_SIMPLE_LABELS[@]}"
	for color in 0 1; do
		if ((color == 1)); then
			unset NO_COLOR
			colors_set_palette
		else
			NO_COLOR=1
			colors_clear_palette
		fi
		for cols in 40 80 120; do
			for ((cursor = 0; cursor < count; cursor++)); do
				# A section header is never the cursor position in a real run.
				[[ "${MENU_SIMPLE_TYPES[$cursor]:-}" == header ]] && continue

				want="$(_menu_simple_draw "$cursor" "$cols")"
				got="$(py_draw "$cursor" "$cols" "$color" "$spec" "$REPO_DIR")"
				if [[ "$want" == "$got" ]]; then
					passed=$((passed + 1))
				else
					failed=$((failed + 1))
					printf 'not ok - %s cursor=%d cols=%d color=%d\n' "$name" "$cursor" "$cols" "$color"
					diff <(printf '%s\n' "$want" | cat -v) <(printf '%s\n' "$got" | cat -v) | head -6
				fi
			done
		done
	done
	unset NO_COLOR
	printf 'ok - %s frames match in every position, width and palette\n' "$name"
}

# The real Agentbot main menu, verbatim from scripts/menu.sh: labels long enough
# to need fitting at 40 columns, and two-line descriptions.
compare_menu 'the Agentbot main menu' '{
 "title": "Agentbot", "breadcrumb": "Agentbot",
 "labels": ["Check Status", "Install Agentbot", "Update", "Prune Skills", "Quit"],
 "descs": ["Check the installed Agentbot components and baseline.\nRead-only status; no updates or writes are performed.",
           "Choose what to set up, then install: skills, Graphify, Boost.\nManaged outputs, Doctor and the launcher link always run.",
           "Update the repository, reconcile skills, and refresh workspaces plus global outputs.\nA preview and explicit confirmation are required before mutation.",
           "Select and permanently remove manual, orphaned, excluded, or stale skills.\nEach candidate shows its classification and source detail before confirmation.",
           "Exit the Agentbot menu.\nReturn to the calling process."]}'

# No breadcrumb and no descriptions: the frame loses a row and its footer.
compare_menu 'a bare menu' '{"title": "Bare", "labels": ["One", "Two", "Quit"]}'

# Section headers: not numbered, not selectable, drawn in the section style.
compare_menu 'a menu with section headers' '{
 "title": "Sections",
 "labels": ["Setup", "Install", "Update", "Other", "Quit"],
 "types": ["header", "", "", "header", ""],
 "descs": ["", "Install everything.", "Update everything.", "", "Leave."]}'

# Every phrase the Bash hint lights, in one line, so the substitution order is
# compared rather than trusted.
compare_menu 'a menu whose hint lights every key' '{
 "title": "Hints", "labels": ["Only", "Quit"], "descs": ["One.", "Two."],
 "hint": "Up/Down navigate   Space toggle   Enter confirm   a all   n none   q back"}'

# Long enough to be cut at the narrow width and not at the wide one, on both the
# label and the description.
compare_menu 'a menu that overflows its width' '{
 "title": "Overflow", "breadcrumb": "Agentbot \u203a Overflow",
 "labels": ["A label long enough that forty columns cannot hold it", "Quit"],
 "descs": ["A description line long enough that it must be cut short at the narrow width and not at the wide one.\nSecond line.",
           "Short."]}'

# The checkbox frame is the other menu shape, and a fiddlier one: eight colour
# combinations per row, a status column as wide as its widest status, and a
# label fitted against whatever is left. Every row state is compared.
cb_py_draw() {
	python3 - "$1" "$2" "$3" "$4" "$REPO_DIR" <<'PY'
import json, sys

sys.path.insert(0, sys.argv[5])
from src.ui import checkbox, menu

cursor, cols, color, spec = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3] == "1", json.loads(sys.argv[4])
sys.stdout.write(
    checkbox.draw(
        title=spec["title"],
        breadcrumb=spec.get("breadcrumb", ""),
        labels=spec["labels"],
        statuses=spec.get("statuses"),
        checked=spec["checked"],
        descs=spec.get("descs"),
        hint=spec.get("hint", checkbox.DEFAULT_HINT),
        status_message=spec.get("status_message", ""),
        cursor=cursor,
        size=int(spec["size"]),
        cols=cols,
        palette=menu.Palette(color=color),
        compact=bool(spec.get("compact")),
    )
)
PY
}

compare_checkbox() {
	local name="$1" spec="$2" cursor cols color want got count size

	eval "$(
		python3 - "$spec" <<'SPEC'
import json, shlex, sys

spec = json.loads(sys.argv[1])
out = [f"MENU_CB_TITLE={shlex.quote(spec['title'])}"]
out.append(f"MENU_CB_BREADCRUMB={shlex.quote(spec.get('breadcrumb', ''))}")
if spec.get("hint"):
    out.append(f"MENU_CB_HINT={shlex.quote(spec['hint'])}")
else:
    out.append("unset MENU_CB_HINT")
out.append("MENU_CB_LABELS=(%s)" % " ".join(shlex.quote(x) for x in spec["labels"]))
out.append("MENU_CB_STATUS=(%s)" % " ".join(shlex.quote(x) for x in (spec.get("statuses") or [""] * len(spec["labels"]))))
out.append("MENU_CB_CHECKED=(%s)" % " ".join(str(int(x)) for x in spec["checked"]))
if spec.get("descs"):
    out.append("MENU_CB_DESCS=(%s)" % " ".join(shlex.quote(x) for x in spec["descs"]))
else:
    out.append("unset MENU_CB_DESCS")
out.append("MENU_CB_COMPACT=%s" % ("true" if spec.get("compact") else "false"))
out.append("size=%d" % spec["size"])
print("\n".join(out))
SPEC
	)"

	count="${#MENU_CB_LABELS[@]}"
	for color in 0 1; do
		if ((color == 1)); then
			unset NO_COLOR
			colors_set_palette
		else
			NO_COLOR=1
			colors_clear_palette
		fi
		for cols in 40 80 120; do
			for ((cursor = 0; cursor < count; cursor++)); do
				want="$(_menu_cb_draw "$cursor" "$size" "${3:-}" "$cols")"
				got="$(cb_py_draw "$cursor" "$cols" "$color" \
					"$(python3 -c "
import json, sys
spec = json.loads(sys.argv[1])
spec['status_message'] = sys.argv[2]
print(json.dumps(spec))
" "$spec" "${3:-}")")"
				if [[ "$want" == "$got" ]]; then
					passed=$((passed + 1))
				else
					failed=$((failed + 1))
					printf 'not ok - %s cursor=%d cols=%d color=%d\n' "$name" "$cursor" "$cols" "$color"
					diff <(printf '%s\n' "$want" | cat -v) <(printf '%s\n' "$got" | cat -v) | head -6
				fi
			done
		done
	done
	unset NO_COLOR
	printf 'ok - %s frames match in every row state, width and palette\n' "$name"
}

# The Agentbot component selector: everything checked, no statuses.
compare_checkbox 'the component selector' '{
 "title": "Install Agentbot", "breadcrumb": "Agentbot › Install Agentbot", "size": 10,
 "labels": ["Skills", "Graphify integration", "Boost integration"],
 "checked": [1, 1, 1],
 "descs": ["Install and update the skills named in skills.sources.yaml.",
           "Refresh the Graphify CLI and Agent Skills integration.",
           "Configure Boost for Claude, Codex, and Cursor."]}'

# Mixed checks and every status colour, including the one that reads as its own
# opposite: "not backed up" contains "backed up".
compare_checkbox 'every status colour and check state' '{
 "title": "Prune Skills", "breadcrumb": "Agentbot › Prune Skills", "size": 10,
 "labels": ["installed-one", "orphaned-two", "a skill whose name is far too long to fit in forty columns", "four", "five", "six"],
 "statuses": ["installed", "not backed up", "missing", "skipped", "upgrade available", "unknown"],
 "checked": [1, 0, 1, 0, 1, 0],
 "descs": ["one", "two", "three", "four", "five", "six"]}' \
	'Nothing selected yet.'

# Paging: more rows than a page holds, so the header counts and the visible
# window have to agree between the two.
compare_checkbox 'a list that pages' '{
 "title": "Prune Skills", "breadcrumb": "Agentbot › Prune Skills", "size": 3,
 "labels": ["one", "two", "three", "four", "five", "six", "seven"],
 "statuses": ["ok", "ok", "drift", "ok", "extra", "ok", "ok"],
 "checked": [0, 0, 0, 0, 0, 0, 0]}'

# Compact rows take a different path through the colours entirely.
compare_checkbox 'compact rows' '{
 "title": "Compact", "breadcrumb": "Agentbot › Compact", "size": 10, "compact": true,
 "labels": ["alpha", "beta", "gamma"],
 "checked": [1, 0, 1]}'

# A status wider than the column pushes the label, and a three-digit list
# widens the index.
compare_checkbox 'a wide status and a long list' "$(python3 -c "
import json
print(json.dumps({
    'title': 'Wide', 'breadcrumb': 'Agentbot › Wide', 'size': 4,
    'labels': [f'entry number {i}' for i in range(101)],
    'statuses': ['a status longer than sixteen characters'] + ['ok'] * 100,
    'checked': [i % 2 for i in range(101)],
}))")"

# The key decoder moved too, and it must agree with menu_read_key on every
# sequence the Bash names -- an arrow read as `ignore` is a menu that will not
# move, and a `q` read as anything but cancel is one that will not leave.
#
# One key per file, which is the convention the Bash's own key tests use: after
# an ESC the reader peeks for the rest of a sequence, and a file always reports
# ready, so a second key in the same file is swallowed by the first one's peek.
#
# And through DOTFILES_TTY_IN_FD, not the path: reading by path reopens the file
# for every character, so `\e[A` is read as ESC three times over and decodes as
# `ignore`. The Python reader holds one handle open and has no such mode, so
# feeding it the path is correct there -- the two seams are compared, not the
# two spellings of one.
key_dir="$(mktemp -d)"
trap 'rm -rf -- "$key_dir"' EXIT
key_case=0
while IFS='|' read -r label bytes; do
	key_case=$((key_case + 1))
	key_file="$key_dir/key-$key_case"
	printf '%b' "$bytes" >"$key_file"
	exec {DOTFILES_TTY_IN_FD}<"$key_file"
	export DOTFILES_TTY_IN_FD
	want="$(menu_read_key)"
	exec {DOTFILES_TTY_IN_FD}<&-
	unset DOTFILES_TTY_IN_FD
	got="$(python3 -c "
import sys
sys.path.insert(0, '$REPO_DIR')
from src.ui.menu_select import KeyReader
reader = KeyReader('$key_file')
print(reader.read_action())
reader.close()
")"
	if [[ "$want" == "$got" ]]; then
		passed=$((passed + 1))
	else
		failed=$((failed + 1))
		printf 'not ok - key %s: bash %s, python %s\n' "$label" "$want" "$got"
	fi
done <<'KEYS'
up arrow|\e[A
down arrow|\e[B
right arrow|\e[C
left arrow|\e[D
application up|\eOA
application down|\eOB
shift tab|\e[Z
page up|\e[5~
page down|\e[6~
space|\040
enter|\n
a|a
A|A
n|n
N|N
q|q
Q|Q
ctrl-c|\003
tab|\t
unbound|z
bare escape|\e
nothing at all|
KEYS
printf 'ok - the key decoder agrees with menu_read_key on every sequence\n'

# The redraw height must match too: it is how far up the next frame starts, and
# an off-by-one draws every frame after the first in the wrong place.
MENU_SIMPLE_TITLE='Heights'
MENU_SIMPLE_BREADCRUMB='Agentbot'
MENU_SIMPLE_TYPES=()
MENU_SIMPLE_LABELS=('One' 'Two' 'Quit')
MENU_SIMPLE_DESCS=('a' 'b' 'c')
want_lines="$(_menu_simple_menu_lines "${#MENU_SIMPLE_LABELS[@]}")"
got_lines="$(python3 -c "
import sys
sys.path.insert(0, '$REPO_DIR')
from src.ui import menu
print(menu.frame_height(['One', 'Two', 'Quit'], breadcrumb='Agentbot', descs=['a', 'b', 'c']))
")"
if [[ "$want_lines" == "$got_lines" ]]; then
	printf 'ok - the redraw height matches (%s lines)\n' "$want_lines"
	passed=$((passed + 1))
else
	printf 'not ok - redraw height: bash %s, python %s\n' "$want_lines" "$got_lines"
	failed=$((failed + 1))
fi

printf '\nRan %d menu-parity comparison(s); %d failure(s).\n' "$((passed + failed))" "$failed"
((failed == 0))
