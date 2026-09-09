"""The Graphify command reference.

What Graphify can be asked to do, in three sections, with the exact command for
each. Reference material, not behaviour: nothing here runs Graphify, and
Agentbot's own lifecycle boundary -- what Install and Update do for you, and
what stays manual -- is stated alongside because that is the question the
reference is usually opened to answer.

The commands are checked against `graphify --help` by
tests/shell/test_menu_actions.sh, so a row naming a command Graphify no longer
has fails rather than being offered to an operator.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Row:
    label: str
    command: str
    description: str


ASSISTANT: tuple[Row, ...] = (
    Row("Build or refresh", "/graphify .", "Create or update the current project graph."),
    Row("Codex skill", "$graphify .", "Run the same workflow through the Codex skill form."),
    Row(
        "Query",
        '/graphify query "what connects auth to the database?"',
        "Ask a graph-grounded architecture question.",
    ),
    Row(
        "Path",
        '/graphify path "UserService" "DatabasePool"',
        "Find the shortest connection between two nodes.",
    ),
    Row(
        "Explain",
        '/graphify explain "RateLimiter"',
        "Explain one node and its immediate relationships.",
    ),
    Row("Wiki", "/graphify . --wiki", "Generate the graph and its navigable wiki output."),
)

SHELL: tuple[Row, ...] = (
    Row("Extract", "graphify extract .", "Run a headless full extraction."),
    Row("Update", "graphify update .", "Refresh a graph without semantic LLM extraction."),
    Row(
        "Recluster",
        "graphify cluster-only . --resolution 1.5",
        "Recluster an existing graph and regenerate its report.",
    ),
    Row(
        "Query",
        'graphify query "what connects auth to the database?"',
        "Query the local graph from the shell.",
    ),
    Row(
        "Path",
        'graphify path "UserService" "DatabasePool"',
        "Find the shortest path from the shell.",
    ),
    Row("Explain", 'graphify explain "RateLimiter"', "Explain a graph node from the shell."),
    Row(
        "Export call flow",
        "graphify export callflow-html",
        "Export the Mermaid call-flow report.",
    ),
    Row("Hook status", "graphify hook status", "Inspect repository hook integration."),
    Row(
        "Merge graphs",
        "graphify merge-graphs a.json b.json --out merged.json",
        "Merge two graph files without altering their sources.",
    ),
)

PLATFORM: tuple[Row, ...] = (
    Row(
        "Agent Skills",
        "graphify install --platform agents",
        "Copy the generic skill into the Agent Skills store.",
    ),
    Row("Claude", "graphify install --platform claude", "Copy the skill into Claude configuration."),
    Row("Codex", "graphify install --platform codex", "Copy the skill into Codex configuration."),
    Row(
        "Cursor",
        "graphify install --platform cursor",
        "Copy the skill into Cursor configuration.",
    ),
)

SECTIONS: dict[str, tuple[Row, ...]] = {
    "assistant": ASSISTANT,
    "shell": SHELL,
    "platform": PLATFORM,
}


def rows(section: str) -> tuple[Row, ...]:
    try:
        return SECTIONS[section]
    except KeyError:
        raise KeyError(f"unknown Graphify section: {section}") from None


def row(section: str, command: str) -> Row | None:
    for candidate in rows(section):
        if candidate.command == command:
            return candidate
    return None


def shell_command_names() -> tuple[str, ...]:
    """The Graphify subcommand each shell and platform row invokes.

    What `graphify --help` is checked against: the first word after `graphify`,
    which is the name that has to still exist.
    """
    return tuple(
        candidate.command.removeprefix("graphify ").split(" ", 1)[0]
        for candidate in (*SHELL, *PLATFORM)
    )


def main(argv: list[str] | None = None) -> int:
    """A read hatch for the Bash that still draws the detail page.

    Two questions only: which row is this command, and which Graphify
    subcommands do the rows name. Kept narrow on purpose -- the reference data
    lives here, and everything else about it is drawn from the menu.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Read the Graphify reference.")
    parser.add_argument("--section")
    parser.add_argument("--command")
    parser.add_argument("--commands", action="store_true")
    args = parser.parse_args(argv)

    if args.commands:
        print("\n".join(shell_command_names()))
        return 0

    if not args.section or args.command is None:
        parser.error("--section and --command are required without --commands")
    found = row(args.section, args.command)
    if found is None:
        return 1
    print(f"{found.label}\x1f{found.description}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
