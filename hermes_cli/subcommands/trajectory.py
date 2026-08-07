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
    sync = radar.add_mutually_exclusive_group()
    sync.add_argument(
        "--sync",
        "--sync-store",
        dest="sync_store",
        action="store_true",
        help="Sync report candidates into the profile-local lifecycle store (default).",
    )
    sync.add_argument(
        "--no-sync",
        "--no-sync-store",
        dest="sync_store",
        action="store_false",
        help="Generate the report without updating the lifecycle store.",
    )
    radar.set_defaults(sync_store=True)

    candidates = trajectory_subparsers.add_parser(
        "candidates",
        help="Manage profile-local radar candidate lifecycle state",
    )
    candidate_commands = candidates.add_subparsers(
        dest="candidates_command", required=True
    )

    candidate_list = candidate_commands.add_parser(
        "list", aliases=["ls"], help="List tracked candidates"
    )
    candidate_list.add_argument(
        "--status",
        choices=("new", "accepted", "deferred", "resolved", "ignored", "regressed"),
    )
    candidate_list.add_argument(
        "--all", action="store_true", help="Include resolved and ignored candidates"
    )
    candidate_list.add_argument(
        "--json", action="store_true", help="Emit JSON instead of markdown"
    )

    candidate_show = candidate_commands.add_parser(
        "show", help="Show one candidate record as JSON"
    )
    candidate_show.add_argument("fingerprint")

    for command, help_text in (
        ("accept", "Mark a candidate accepted"),
        ("defer", "Defer a candidate"),
        ("resolve", "Mark a candidate resolved pending confirmation"),
        ("ignore", "Ignore a candidate"),
    ):
        action = candidate_commands.add_parser(command, help=help_text)
        action.add_argument("fingerprint")

    trajectory_parser.set_defaults(func=cmd_trajectory)
