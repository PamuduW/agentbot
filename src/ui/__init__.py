"""Terminal output for Agentbot.

Split in two: `table` holds the generic terminal primitives (colour, width
maths, tables, rollups); `reports` holds one printer per domain object. Both
are re-exported here so callers keep importing from `src.ui`.
"""

from __future__ import annotations

from .reports import (
    print_boost_status,
    print_command_help,
    print_doctor_summary,
    print_graphify_status,
    print_manual_skill_removal_report,
    print_output_refresh_report,
    print_reconciliation_report,
    print_skill_prune_report,
    print_skills_report,
    print_skills_update_report,
    print_status_summary,
    print_update_outcome,
    print_update_plan,
    print_vscode_report,
    print_workspace_list,
    print_workspace_removed,
    print_workspace_report,
    print_workspace_resync_report,
)
from .table import (
    color_result,
    highlight_manual_skill_name,
    print_header,
    print_rollup,
    print_section,
    print_section_block,
    print_table,
    print_table_columns,
    print_table_model,
    shorten_detail,
    strip_ansi,
    table_rows,
    terminal_columns,
    use_color,
)

__all__ = [
    "color_result",
    "highlight_manual_skill_name",
    "print_boost_status",
    "print_command_help",
    "print_doctor_summary",
    "print_graphify_status",
    "print_header",
    "print_manual_skill_removal_report",
    "print_output_refresh_report",
    "print_reconciliation_report",
    "print_rollup",
    "print_section",
    "print_section_block",
    "print_skill_prune_report",
    "print_skills_report",
    "print_skills_update_report",
    "print_status_summary",
    "print_table",
    "print_table_columns",
    "print_table_model",
    "print_update_outcome",
    "print_update_plan",
    "print_vscode_report",
    "print_workspace_list",
    "print_workspace_removed",
    "print_workspace_report",
    "print_workspace_resync_report",
    "shorten_detail",
    "strip_ansi",
    "table_rows",
    "terminal_columns",
    "use_color",
]
