#!/usr/bin/env python3
"""Shared state persistence for the autonomous improve loop.

Watermarks and manifests are tiny JSON files under a state dir. All writes are
atomic (tmp file + rename) so a crash or torn write can never leave a
half-written JSON that a later run would misread. Reads are fail-closed:
a missing file is None; a *present but corrupt* file raises (callers must
decide to halt, not silently treat corruption as "no state").

Layout:
    <state_dir>/watermark.<key>.json   -> {"value": <int>}
    <state_dir>/manifest.json          -> any JSON object (swap/apply manifest)
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

# Only allow safe filename characters in a watermark key so a caller cannot
# escape the state dir via a crafted key ("../evil" must stay inside).
_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _watermark_path(state_dir: Path, key: str) -> Path:
    if not isinstance(key, str) or not _SAFE_KEY.match(key):
        raise ValueError(f"unsafe watermark key: {key!r}")
    return Path(state_dir) / f"watermark.{key}.json"


def read_watermark(state_dir, key: str):
    """Return the int watermark for key, or None if never written.

    Raises ValueError if the file exists but is corrupt (fail-closed: a torn
    or tampered state file must not silently read as 'no state')."""
    path = _watermark_path(state_dir, key)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"corrupt watermark file {path}: {exc}") from exc
    value = data.get("value")
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"corrupt watermark file {path}: expected int 'value'")
    return value


def write_watermark(state_dir, key: str, value: int) -> None:
    """Atomically persist an int watermark for key (tmp + rename)."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"watermark value must be an int, got {type(value).__name__}")
    path = _watermark_path(state_dir, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"value": value}))
    os.replace(tmp, path)


def read_manifest(state_dir):
    """Return the manifest dict, or None if never written. Raises ValueError
    on corrupt JSON (fail-closed)."""
    path = Path(state_dir) / "manifest.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"corrupt manifest file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"corrupt manifest file {path}: expected a JSON object")
    return data


def write_manifest(state_dir, manifest: dict) -> None:
    """Atomically persist the apply/swap manifest (tmp + rename)."""
    if not isinstance(manifest, dict):
        raise TypeError("manifest must be a dict")
    path = Path(state_dir) / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, sort_keys=True))
    os.replace(tmp, path)