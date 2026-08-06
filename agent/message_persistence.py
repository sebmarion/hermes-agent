"""Helpers for copying live/replayed messages into durable session rows."""

from __future__ import annotations

from typing import Any, Mapping


def copy_message_for_persistence(message: Mapping[str, Any]) -> dict[str, Any]:
    """Return a fresh persistence row without dropping supported metadata.

    ``SessionDB`` reads only its supported keys, so preserving the complete
    message mapping is safer than rebuilding a partial row at every branch
    surface.  The small normalizations retain the legacy role/name behavior
    and reject a non-string API sidecar exactly like the previous serializers.
    """
    row = dict(message)
    row["role"] = message.get("role", "user")
    if not row.get("tool_name") and message.get("name"):
        row["tool_name"] = message.get("name")
    if "api_content" in row and not isinstance(row["api_content"], str):
        row["api_content"] = None
    return row
