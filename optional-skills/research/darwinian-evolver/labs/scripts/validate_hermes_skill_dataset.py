#!/usr/bin/env python3
"""Fail-closed validator for hermes_skill_dataset.jsonl rows.

Usage:
    python validate_hermes_skill_dataset.py <dataset.jsonl>

Exit codes:
    0  every row valid
    1  at least one row invalid (details printed to stderr)
    2  usage / IO error

The validator is intentionally fail-closed: any structural deviation, unknown
field, schema-version mismatch, or suspected embedded credential aborts the run.
It validates each JSONL row against the co-located JSON Schema
(schemas/hermes_skill_dataset.schema.json).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

LAB_DIR = Path(__file__).resolve().parents[1]
SCHEMA_PATH = LAB_DIR / "schemas" / "hermes_skill_dataset.schema.json"

# Heuristic credential scanner. Runs AFTER schema validation; catching a secret
# is always an abort, never a warning.
#
# NOTE (F3): this is a best-effort *pattern* detector, not a full secret
# scanner. The plan's "abort on suspected embedded secrets" is met for these
# known shapes; bare high-entropy tokens with no recognizable prefix are
# deliberately NOT matched (too many false positives in real Hermes
# transcripts). Unknown-field rejection (additionalProperties=false) is the
# primary defense against new data-exfil channels.
_CRED_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key|apikey)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"), "api key literal"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "OpenAI/Anthropic-style secret"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "GitHub personal access token"),
    (re.compile(r"\bxo[a-z]+-[A-Za-z0-9\-]{10,}\b"), "Slack/xox token"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "JWT"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{20,}"), "bearer token"),
]


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _iter_errors(instance, schema, path=""):
    """Minimal structural JSON-schema validator for the subset we use.

    Supports: type, const, enum, required, properties, additionalProperties,
    minLength, maxLength, minItems, pattern, minimum, maximum, items.
    Returns a list of human-readable violation strings (empty == valid).
    """
    errors = []
    p = path or "<root>"

    t = schema.get("type")
    if t:
        checkers = {
            "object": lambda v: isinstance(v, dict),
            "array": lambda v: isinstance(v, list),
            "string": lambda v: isinstance(v, str),
            "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "boolean": lambda v: isinstance(v, bool),
        }
        checker = checkers.get(t)
        if checker is None:
            errors.append(f"{p}: schema type {t!r} not supported by minimal validator")
            return errors
        if not checker(instance):
            errors.append(f"{p}: expected type {t}, got {type(instance).__name__}")
            return errors

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{p}: must equal const {schema['const']!r}, got {instance!r}")

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{p}: {instance!r} not in enum {schema['enum']}")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{p}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{p}: longer than maxLength {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{p}: does not match pattern {schema['pattern']!r} (value {instance[:40]!r})")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{p}: below minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{p}: above maximum {schema['maximum']}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{p}: fewer than minItems {schema['minItems']}")
        items = schema.get("items")
        if items:
            for i, el in enumerate(instance):
                errors.extend(_iter_errors(el, items, f"{p}[{i}]"))

    if isinstance(instance, dict):
        for req in schema.get("required", []):
            if req not in instance:
                errors.append(f"{p}: missing required field {req!r}")
        props = schema.get("properties", {})
        for key, val in instance.items():
            if key in props:
                errors.extend(_iter_errors(val, props[key], f"{p}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{p}: unknown field {key!r} (additionalProperties=false)")

    return errors


def _scan_for_credentials(row: dict):
    """Scan every scalar leaf (recursively) for credential-shaped values.

    Returns a list of (dotted_path, label). Numbers are stringified before
    matching so a secret stored as a JSON number is not skipped; keys are also
    scanned (a secret could be smuggled into a field name)."""
    found = []

    def walk(obj, prefix):
        if isinstance(obj, dict):
            for k, v in obj.items():
                for pat, label in _CRED_PATTERNS:
                    if pat.search(k):
                        found.append((prefix + str(k), f"credential-shaped key ({label})"))
                walk(v, prefix + str(k) + ".")
        elif isinstance(obj, list):
            for i, el in enumerate(obj):
                walk(el, prefix + str(i) + ".")
        elif isinstance(obj, bool):
            return
        else:
            text = obj if isinstance(obj, str) else (None if obj is None else repr(obj))
            if text is None:
                return
            for pat, label in _CRED_PATTERNS:
                if pat.search(text):
                    found.append((prefix.rstrip("."), label))

    walk(row, "")
    return found


def validate_row(row, schema: dict):
    if not isinstance(row, dict):
        # Fail-closed in the standard shape: a JSONL line that is a bare string,
        # number, or null is a clean per-line error, not a traceback (F2).
        return [f"<root>: expected an object (JSON row), got {type(row).__name__}"]
    errors = _iter_errors(row, schema)
    creds = _scan_for_credentials(row)
    for key, label in creds:
        errors.append(f"<root>.{key}: suspected embedded credential ({label})")
    return errors


def main(argv) -> int:
    if len(argv) != 2:
        print("usage: validate_hermes_skill_dataset.py <dataset.jsonl>", file=sys.stderr)
        return 2

    path = Path(argv[1])
    if not path.is_file():
        print(f"error: dataset not found: {path}", file=sys.stderr)
        return 2

    schema = _load_schema()
    bad = 0
    total = 0
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        total += 1
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"[line {lineno}] invalid JSON: {exc}", file=sys.stderr)
            bad += 1
            continue
        errors = validate_row(row, schema)
        if errors:
            bad += 1
            for e in errors:
                print(f"[line {lineno}] {e}", file=sys.stderr)

    if bad:
        print(f"RESULT: FAIL ({bad}/{total} rows invalid)", file=sys.stderr)
        return 1
    print(f"RESULT: OK ({total} rows valid)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
