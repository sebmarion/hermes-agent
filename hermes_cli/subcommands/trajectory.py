"""``hermes trajectory`` subcommands."""

from __future__ import annotations

from typing import Callable


def build_trajectory_parser(subparsers, *, cmd_trajectory: Callable) -> None:
    """Attach trajectory analysis commands to ``subparsers``."""
    trajectory_parser = subparsers.add_parser(
        "trajectory",
        help="Analyze local session history for action candidates",
        description=(
            "Mine the local Hermes state.db for privacy-preserving action candidates. "
            "Default output contains session/message evidence refs, not raw transcripts."
        ),
    )
    trajectory_subparsers = trajectory_parser.add_subparsers(dest="trajectory_command", required=True)

    radar = trajectory_subparsers.add_parser(
        "radar",
        help="Generate a session-to-action radar report",
        description="Rank recurring session friction into FIX/CONFIG/SKILL_PATCH/CRON/DECIDE candidates.",
    )
    radar.add_argument("--days", type=int, default=14, help="Number of days to analyze (default: 14)")
    radar.add_argument("--source", help="Filter by platform/source (webui, cli, cron, etc.)")
    radar.add_argument("--limit", type=int, default=10, help="Maximum candidates to emit (default: 10; 0 = all)")
    radar.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="Output format (default: markdown)",
    )
    radar.add_argument("--out", help="Write report to this path instead of stdout")
    radar.add_argument(
        "--include-snippets",
        action="store_true",
        help="Include capped/redacted message snippets. Off by default for privacy.",
    )

    trajectory_parser.set_defaults(func=cmd_trajectory)
