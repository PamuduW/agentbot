#!/usr/bin/env bash
# shellcheck shell=bash
set -uo pipefail

# One left edge, swept from the source.
#
# docs/designs/presentation/README.md: every line a run prints starts at two
# spaces, the column the shared header and the report tables already use. The
# rule was established by sweeping the product by hand, which is why it drifted
# back -- the menus and bindings each print their own variants of the same
# messages, and nine of them still opened at column zero after the shared logic
# had been corrected.
#
# This sweeps instead of trusting the sweep. It reads the format strings rather
# than running the screens, so a menu that needs a terminal, a Python child or a
# GitHub reply is covered like any other.

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
passed=0
failed=0

check() {
	local label="$1"
	shift
	if "$@"; then
		printf 'ok - %s\n' "$label"
		passed=$((passed + 1))
	else
		printf 'not ok - %s\n' "$label"
		failed=$((failed + 1))
	fi
}

# The two exemptions, both deliberate and both matched by the sibling product:
#
#   "Unknown <menu> action:" is a dispatch failure -- a caller passed a key no
#   branch owns. It is a programmer's message on stderr, not a screen, and
#   Dotfiles prints its own at column zero for the same reason.
#
#   A line carrying \x1f is a field-separated record read by another process --
#   the graphify detail row, the menu spec -- not a line anybody sees.
sweep() {
	python3 - "$ROOT" <<'PY'
import ast
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])

# The screens: the bootstrap entrypoint, the launcher, and the menus. The
# files under scripts/lib/ are swept by the suites that run them instead --
# most of their printfs return a value through $( ) or write a field-separated
# protocol to a Python child, and neither is a line anybody reads.
SHELL_SURFACES = [
    "install.sh",
    "bin/agentbot",
    "scripts/menu.sh",
]
SHELL_SURFACES += sorted(str(p.relative_to(root)) for p in (root / "scripts/menus").glob("*.sh"))
PY_SURFACES = sorted(str(p.relative_to(root)) for p in (root / "src").rglob("*.py"))

EXEMPT = (
    re.compile(r"^Unknown .* action: "),
    re.compile(r"\x1f"),
    # Nothing is exempt for going to stderr. The messages there are the ones an
    # operator sees when a command is refused -- a bad help topic, an archived
    # command -- and they used to be the last four lines in the product still
    # printing at column zero, precisely because the first sweep skipped the
    # stream rather than the message.
)

# printf -v writes into a variable rather than to a stream. The Python side
# is parsed instead of matched; see leading_literal below.
SHELL_FMT = re.compile(r"(?<!-v )\bprintf\s+'((?:[^'\\]|\\.)*)'")


def opens_at_column_zero(fmt: str) -> bool:
    """True when the format starts visible text before the two-space indent."""
    while fmt.startswith("\\n"):
        fmt = fmt[2:]
    # A leading %s is a colour slot: `printf '%sSaved.%s\n' "$C_GREEN" ...`
    # prints an escape sequence of zero width and then the text.
    while fmt.startswith("%s"):
        fmt = fmt[2:]
    return bool(re.match(r"[A-Za-z0-9\[\-{]", fmt))


offenders = []
for name in SHELL_SURFACES:
    path = root / name
    if not path.exists():
        continue
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if line.strip().startswith("#"):
            continue
        for match in SHELL_FMT.finditer(line):
            fmt = match.group(1)
            if not opens_at_column_zero(fmt):
                continue
            if any(rule.search(fmt) for rule in EXEMPT):
                continue
            offenders.append(f"{name}:{number}: {fmt!r}")


def leading_literal(node):
    """The text a print() argument starts with, before any interpolation.

    Parsed rather than matched: _archived_command_error writes its message as a
    two-line implicitly-concatenated f-string, and a line-oriented regex cannot
    see the `print(` above it. That one sat at column zero through a sweep that
    claimed to cover the file.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # Rebuilt with the interpolations as `{}`, so an offender is reported as
        # something recognisable and an exemption can still match the literal
        # parts. An f-string opening on a replacement field puts a value at the
        # margin just as surely as a literal would, so `{}` counts as visible.
        return "".join(
            part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else "{}"
            for part in node.values
        )
    return None


for name in PY_SURFACES:
    path = root / name
    if not path.exists():
        continue
    for node in ast.walk(ast.parse(path.read_text(), filename=name)):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "print"):
            continue
        if not node.args:
            continue
        text = leading_literal(node.args[0])
        if text is None or not opens_at_column_zero(text):
            continue
        if any(rule.search(text) for rule in EXEMPT):
            continue
        offenders.append(f"{name}:{node.lineno}: {text!r}")

for offender in offenders:
    print(offender)
sys.exit(1 if offenders else 0)
PY
}

test_every_printed_line_starts_at_the_indent() {
	local offenders
	offenders="$(sweep)" && return 0
	printf 'lines opening at column zero:\n%s\n' "$offenders" >&2
	return 1
}

# The sweep reads format strings, so it can only catch what is written as one.
# These two run the code, on the paths where the message is assembled elsewhere.
test_menu_dispatch_failure_reports_at_the_indent() (
	export NO_COLOR=1 AGENTBOT_HOME="$ROOT"
	local output
	# shellcheck disable=SC1091
	source "$ROOT/scripts/menu.sh"
	# Overridden after the source: menu.sh brings its own definition in.
	agentbot_menu_status() { return 4; }
	output="$(agentbot_menu_dispatch status 2>&1)"
	[[ "$output" == "  Action failed (exit 4)." ]]
)

test_missing_python_notice_reports_at_the_indent() (
	export NO_COLOR=1 AGENTBOT_HOME="$ROOT"
	local output
	# shellcheck disable=SC1091
	source "$ROOT/scripts/lib/tui.sh"
	_agentbot_python_available() { return 1; }
	output="$(agentbot_menu_run main 2>&1)"
	[[ "$output" == "  The menu needs python3, which is not available." ]]
)

check 'every printed line starts at the two-space indent' \
	test_every_printed_line_starts_at_the_indent
check 'a failed menu action reports at the indent' \
	test_menu_dispatch_failure_reports_at_the_indent
check 'the missing-python notice reports at the indent' \
	test_missing_python_notice_reports_at_the_indent

printf '\nRan %d left-edge test(s); %d failure(s).\n' "$((passed + failed))" "$failed"
((failed == 0))
