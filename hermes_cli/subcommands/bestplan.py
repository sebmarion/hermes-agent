"""``hermes bestplan`` subcommand parser.

Read-only view of the configured BestPlan SOTA lanes plus validation.
The orchestrator code lives in ``agent.bestplan_orchestrator``; this CLI
surface lets the operator inspect and validate the active configuration
without running an actual ``/bestplan``.

Usage::

    hermes bestplan lanes   # Show lanes + validate
"""

from __future__ import annotations

from typing import Callable


def build_bestplan_parser(subparsers, *, cmd_bestplan: Callable) -> None:
    """Attach the ``bestplan`` subcommand to ``subparsers``."""
    bestplan_parser = subparsers.add_parser(
        "bestplan",
        help="Inspect BestPlan SOTA lane configuration",
        description=(
            "View and validate the heterogeneous intelligence lanes that "
            "the /bestplan orchestration uses for explorer dispatch and "
            "synthesis.  Lane definitions live in config.yaml under "
            "'bestplan.lanes'.\n\n"
            "This is a read-only view — update config.yaml directly to "
            "change the active SOTA models."
        ),
    )
    bestplan_sub = bestplan_parser.add_subparsers(dest="bestplan_command")
    bestplan_sub.add_parser(
        "lanes",
        help="Show configured lanes and validate the runtime",
    )
    bestplan_parser.set_defaults(func=cmd_bestplan)


def cmd_bestplan(args) -> int:
    """Handler for ``hermes bestplan`` — prints lanes + validation status."""
    sub = getattr(args, "bestplan_command", None)
    if sub is None:
        print("Usage: hermes bestplan lanes")
        print("  Shows the configured SOTA lanes and validates them.")
        return 0

    if sub != "lanes":
        print(f"Unknown bestplan subcommand: {sub}")
        print("Available: lanes")
        return 1

    from hermes_cli.config import load_config
    from agent.bestplan_orchestrator import (
        BestPlanUnavailable, validate_runtime,
    )

    config = None
    try:
        config = load_config().get("bestplan")
    except Exception:
        pass

    source = "config.yaml" if config else "DEFAULT (no bestplan config block)"
    try:
        resolved = validate_runtime(config)
    except BestPlanUnavailable as exc:
        print(f"\n  BestPlan SOTA Lanes — source: {source}")
        print(f"  Validation: FAIL — {exc}\n")
        return 1

    lanes = resolved["lanes"]
    synthesizer = resolved.get("synthesizer", "strongest")
    print(f"\n  BestPlan SOTA Lanes — source: {source}")
    print(f"  {'Name':<8} {'Provider':<22} {'Model':<20} {'API Mode':<22} {'Reasoning':<12}")
    print(f"  {'─'*8} {'─'*22} {'─'*20} {'─'*22} {'─'*12}")
    for lane in lanes:
        print(
            f"  {lane.get('name',''):<8} "
            f"{lane.get('provider',''):<22} "
            f"{lane.get('model',''):<20} "
            f"{lane.get('api_mode',''):<22} "
            f"{lane.get('reasoning_effort',''):<12}"
        )
    print(f"\n  Synthesizer: {synthesizer} (strongest available lane)")

    print("  Validation: PASS\n")
    return 0
