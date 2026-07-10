"""``hermes trajectory`` subcommands.

Attach as::

    build_trajectory_parser(subparsers, cmd_trajectory=cmd_trajectory)
"""

from __future__ import annotations

from typing import Callable


def build_trajectory_parser(subparsers, *, cmd_trajectory: Callable) -> None:
    """Attach trajectory analysis commands to ``subparsers``.

    Two subcommands:

    * ``radar`` — generate the session-to-action report (privacy-preserving
      by default; evidence refs only, snippets opt-in and redacted).
    * ``candidates`` — manage the local candidate lifecycle
      (list / show / accept / defer / resolve / ignore).
    """
    trajectory_parser = subparsers.add_parser(
        "trajectory",
        help="Analyze local session history for action candidates",
        description=(
            "Mine the local Hermes state.db for privacy-preserving action candidates. "
            "Default output contains session/message evidence refs, not raw transcripts."
        ),
    )
    trajectory_subparsers = trajectory_parser.add_subparsers(dest="trajectory_command", required=True)

    # ---- radar -----------------------------------------------------------
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
    radar.add_argument(
        "--sync-store",
        action="store_true",
        default=True,
        help="Sync candidates into the local lifecycle store (default: on).",
    )
    radar.add_argument(
        "--no-sync-store",
        dest="sync_store",
        action="store_false",
        help="Do not sync candidates into the local lifecycle store.",
    )

    # ---- candidates lifecycle -------------------------------------------
    candidates = trajectory_subparsers.add_parser(
        "candidates",
        help="Manage local candidate lifecycle (accept/defer/resolve/ignore)",
        description=(
            "Manage the local radar candidate store.  Candidates are tracked by "
            "stable fingerprint; resolved candidates regress when fresh evidence resurfaces."
        ),
    )
    candidates_sub = candidates.add_subparsers(dest="candidates_command", required=True)

    c_list = candidates_sub.add_parser("list", aliases=["ls"], help="List tracked candidates (default)")
    c_list.add_argument("--status", help="Filter by status (new, accepted, deferred, resolved, ignored, regressed)")
    c_list.add_argument("--all", action="store_true", help="Include resolved and ignored candidates")
    c_list.add_argument("--json", action="store_true", help="Emit JSON instead of markdown")

    c_show = candidates_sub.add_parser("show", help="Show details for one candidate")
    c_show.add_argument("fingerprint", help="Candidate fingerprint (e.g. done-means-proven-gatekeeper)")

    c_accept = candidates_sub.add_parser("accept", help="Mark a candidate as accepted (working it)")
    c_accept.add_argument("fingerprint", help="Candidate fingerprint")
    c_accept.add_argument("--note", default="", help="Optional note")

    c_defer = candidates_sub.add_parser("defer", help="Defer a candidate (address later)")
    c_defer.add_argument("fingerprint", help="Candidate fingerprint")
    c_defer.add_argument("--note", default="", help="Optional note")

    c_resolve = candidates_sub.add_parser("resolve", help="Mark a candidate as resolved")
    c_resolve.add_argument("fingerprint", help="Candidate fingerprint")
    c_resolve.add_argument("--note", default="", help="Optional note")

    c_ignore = candidates_sub.add_parser("ignore", help="Ignore a candidate (stop tracking actively)")
    c_ignore.add_argument("fingerprint", help="Candidate fingerprint")
    c_ignore.add_argument("--note", default="", help="Optional note")

    trajectory_parser.set_defaults(func=cmd_trajectory)
