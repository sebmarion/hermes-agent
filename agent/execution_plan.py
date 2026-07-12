"""Typed, deterministic contract for local-first execution planning.

The planner model may propose a graph, but this module is the authority that
accepts or rejects it.  It deliberately has no executor: Phase 1 can generate,
repair, and inspect plans without granting them side effects.
"""

from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


PLAN_MODES = ("direct", "delegate", "kanban", "sota")
RISK_LEVELS = ("low", "medium", "high")
SLICE_KINDS = ("scout", "implement", "verify", "review")
CAPABILITIES = ("local_execution", "fast_fallback", "frontier_review")
MAX_SLICES = 6

EXECUTION_PLAN_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "version",
        "mode",
        "risk",
        "slices",
        "merge_policy",
        "stop_condition",
        "escalation_predicates",
    ],
    "properties": {
        "version": {"type": "integer", "const": 1},
        "mode": {"type": "string", "enum": list(PLAN_MODES)},
        "risk": {"type": "string", "enum": list(RISK_LEVELS)},
        "slices": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_SLICES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "kind",
                    "goal",
                    "depends_on",
                    "capability",
                    "workspace",
                    "allowed_paths",
                    "read_only",
                    "expected_artifacts",
                    "acceptance",
                ],
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 64},
                    "kind": {"type": "string", "enum": list(SLICE_KINDS)},
                    "goal": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "uniqueItems": True,
                    },
                    "capability": {
                        "type": "string",
                        "enum": list(CAPABILITIES),
                    },
                    "workspace": {"type": "string"},
                    "allowed_paths": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "uniqueItems": True,
                    },
                    "read_only": {"type": "boolean"},
                    "expected_artifacts": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "acceptance": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
        "merge_policy": {"type": "string", "minLength": 1},
        "stop_condition": {"type": "string", "minLength": 1},
        "escalation_predicates": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
    },
}


def _planner_generation_schema(value: Any) -> Any:
    """Copy validation schema without llama.cpp-incompatible string bounds.

    Local llama.cpp grammar generation rejects nested ``minLength`` and
    ``maxLength`` constraints. Generation remains structurally constrained;
    ``compile_execution_plan`` remains authoritative after decoding.
    """
    if isinstance(value, dict):
        return {
            key: _planner_generation_schema(item)
            for key, item in value.items()
            if key not in {"minLength", "maxLength"}
        }
    if isinstance(value, list):
        return [_planner_generation_schema(item) for item in value]
    return value


EXECUTION_PLAN_GENERATION_SCHEMA: dict[str, Any] = _planner_generation_schema(
    EXECUTION_PLAN_JSON_SCHEMA
)


class PlanValidationError(ValueError):
    """Raised when a proposed plan violates deterministic policy."""


@dataclass(frozen=True)
class ExecutionSlice:
    id: str
    kind: str
    goal: str
    depends_on: tuple[str, ...]
    capability: str
    workspace: str
    allowed_paths: tuple[str, ...]
    read_only: bool
    expected_artifacts: tuple[str, ...]
    acceptance: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionPlan:
    version: int
    mode: str
    risk: str
    slices: tuple[ExecutionSlice, ...]
    merge_policy: str
    stop_condition: str
    escalation_predicates: tuple[str, ...]
    dependency_waves: tuple[tuple[str, ...], ...]

    def to_manifest(self) -> dict[str, Any]:
        """Return a canonical serializable manifest for signing/ persistence."""
        return {
            "version": self.version,
            "mode": self.mode,
            "risk": self.risk,
            "slices": [
                {
                    "id": s.id,
                    "kind": s.kind,
                    "goal": s.goal,
                    "depends_on": list(s.depends_on),
                    "capability": s.capability,
                    "workspace": s.workspace,
                    "allowed_paths": list(s.allowed_paths),
                    "read_only": s.read_only,
                    "expected_artifacts": list(s.expected_artifacts),
                    "acceptance": list(s.acceptance),
                }
                for s in self.slices
            ],
            "merge_policy": self.merge_policy,
            "stop_condition": self.stop_condition,
            "escalation_predicates": list(self.escalation_predicates),
        }


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PlanValidationError(f"{field} must be an object")
    return value


def _text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise PlanValidationError(f"{field} must be a string")
    result = value.strip()
    if not allow_empty and not result:
        raise PlanValidationError(f"{field} must not be empty")
    return result


def _text_list(value: Any, field: str, *, require_one: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise PlanValidationError(f"{field} must be a list")
    result = tuple(_text(item, f"{field}[]") for item in value)
    if require_one and not result:
        raise PlanValidationError(f"{field} must contain at least one item")
    if len(set(result)) != len(result):
        raise PlanValidationError(f"{field} must not contain duplicates")
    return result


def _choice(value: Any, field: str, choices: Sequence[str]) -> str:
    result = _text(value, field)
    if result not in choices:
        raise PlanValidationError(
            f"{field} must be one of {', '.join(choices)} (got {result!r})"
        )
    return result


def _normalized_path(path: str) -> str:
    normalized = posixpath.normpath(path.replace("\\", "/")).strip("/")
    return normalized if normalized != "." else ""


def _paths_overlap(left: str, right: str) -> bool:
    left_norm = _normalized_path(left)
    right_norm = _normalized_path(right)
    if not left_norm or not right_norm:
        return True
    return (
        left_norm == right_norm
        or left_norm.startswith(right_norm + "/")
        or right_norm.startswith(left_norm + "/")
    )


def _dependency_waves(
    slices: Sequence[ExecutionSlice],
) -> tuple[tuple[str, ...], ...]:
    by_id = {item.id: item for item in slices}
    remaining = set(by_id)
    completed: set[str] = set()
    waves: list[tuple[str, ...]] = []
    while remaining:
        ready = tuple(
            item.id
            for item in slices
            if item.id in remaining and set(item.depends_on) <= completed
        )
        if not ready:
            raise PlanValidationError("dependency cycle detected")
        waves.append(ready)
        completed.update(ready)
        remaining.difference_update(ready)
    return tuple(waves)


def _transitive_dependencies(slices: Sequence[ExecutionSlice]) -> dict[str, set[str]]:
    parents = {item.id: set(item.depends_on) for item in slices}
    closure: dict[str, set[str]] = {slice_id: set() for slice_id in parents}
    changed = True
    while changed:
        changed = False
        for slice_id, direct in parents.items():
            expanded = set(direct)
            for parent in direct:
                expanded.update(closure[parent])
            if not expanded <= closure[slice_id]:
                closure[slice_id].update(expanded)
                changed = True
    return closure


def _validate_write_leases(slices: Sequence[ExecutionSlice]) -> None:
    closure = _transitive_dependencies(slices)
    writers = [item for item in slices if not item.read_only]
    for index, left in enumerate(writers):
        for right in writers[index + 1 :]:
            serialized = (
                left.id in closure[right.id] or right.id in closure[left.id]
            )
            if serialized:
                continue
            conflicts = [
                (left_path, right_path)
                for left_path in left.allowed_paths
                for right_path in right.allowed_paths
                if _paths_overlap(left_path, right_path)
            ]
            if conflicts:
                first = conflicts[0]
                raise PlanValidationError(
                    "parallel slices have overlapping write leases: "
                    f"{left.id}:{first[0]} and {right.id}:{first[1]}"
                )


def compile_execution_plan(raw: Any) -> ExecutionPlan:
    """Compile untrusted planner JSON into an immutable validated plan."""
    value = _mapping(raw, "plan")
    unknown = set(value) - set(EXECUTION_PLAN_JSON_SCHEMA["properties"])
    if unknown:
        raise PlanValidationError(
            f"plan contains unknown fields: {', '.join(sorted(unknown))}"
        )

    version = value.get("version")
    if version != 1 or isinstance(version, bool):
        raise PlanValidationError("version must be integer 1")
    mode = _choice(value.get("mode"), "mode", PLAN_MODES)
    risk = _choice(value.get("risk"), "risk", RISK_LEVELS)
    merge_policy = _text(value.get("merge_policy"), "merge_policy")
    stop_condition = _text(value.get("stop_condition"), "stop_condition")
    escalation_predicates = _text_list(
        value.get("escalation_predicates"), "escalation_predicates"
    )

    raw_slices = value.get("slices")
    if not isinstance(raw_slices, list) or not raw_slices:
        raise PlanValidationError("slices must contain at least one slice")
    if len(raw_slices) > MAX_SLICES:
        raise PlanValidationError(f"slices must contain at most {MAX_SLICES} slices")

    slices: list[ExecutionSlice] = []
    slice_fields = set(
        EXECUTION_PLAN_JSON_SCHEMA["properties"]["slices"]["items"]["properties"]
    )
    seen_ids: set[str] = set()
    for index, raw_slice in enumerate(raw_slices):
        item = _mapping(raw_slice, f"slices[{index}]")
        unknown_slice_fields = set(item) - slice_fields
        if unknown_slice_fields:
            raise PlanValidationError(
                f"slices[{index}] contains unknown fields: "
                + ", ".join(sorted(unknown_slice_fields))
            )
        slice_id = _text(item.get("id"), f"slices[{index}].id")
        if slice_id in seen_ids:
            raise PlanValidationError(f"duplicate slice id: {slice_id}")
        seen_ids.add(slice_id)
        read_only = item.get("read_only")
        if not isinstance(read_only, bool):
            raise PlanValidationError(f"slices[{index}].read_only must be boolean")
        allowed_paths = _text_list(
            item.get("allowed_paths"), f"slices[{index}].allowed_paths"
        )
        if not read_only and not allowed_paths:
            raise PlanValidationError(
                f"slices[{index}].allowed_paths must contain a write lease"
            )
        slices.append(
            ExecutionSlice(
                id=slice_id,
                kind=_choice(item.get("kind"), f"slices[{index}].kind", SLICE_KINDS),
                goal=_text(item.get("goal"), f"slices[{index}].goal"),
                depends_on=_text_list(
                    item.get("depends_on"), f"slices[{index}].depends_on"
                ),
                capability=_choice(
                    item.get("capability"),
                    f"slices[{index}].capability",
                    CAPABILITIES,
                ),
                workspace=_text(
                    item.get("workspace"),
                    f"slices[{index}].workspace",
                    allow_empty=True,
                ),
                allowed_paths=allowed_paths,
                read_only=read_only,
                expected_artifacts=_text_list(
                    item.get("expected_artifacts"),
                    f"slices[{index}].expected_artifacts",
                    require_one=True,
                ),
                acceptance=_text_list(
                    item.get("acceptance"),
                    f"slices[{index}].acceptance",
                    require_one=True,
                ),
            )
        )

    if mode == "direct" and len(slices) != 1:
        raise PlanValidationError("direct mode must contain exactly one slice")
    if mode == "sota" and (
        risk != "high"
        or len(slices) != 1
        or slices[0].capability != "frontier_review"
    ):
        raise PlanValidationError(
            "sota mode must be high risk with exactly one frontier_review slice"
        )

    for item in slices:
        if item.id in item.depends_on:
            raise PlanValidationError(f"dependency cycle detected at {item.id}")
        for dependency in item.depends_on:
            if dependency not in seen_ids:
                raise PlanValidationError(
                    f"slice {item.id} references unknown dependency {dependency}"
                )
        if item.capability == "frontier_review" and mode != "sota":
            raise PlanValidationError(
                "frontier_review must be invoked by an escalation predicate, "
                "not scheduled as an unconditional slice"
            )

    if risk == "high" and not escalation_predicates:
        raise PlanValidationError(
            "high-risk plans require at least one frontier escalation predicate"
        )

    waves = _dependency_waves(slices)
    _validate_write_leases(slices)
    return ExecutionPlan(
        version=version,
        mode=mode,
        risk=risk,
        slices=tuple(slices),
        merge_policy=merge_policy,
        stop_condition=stop_condition,
        escalation_predicates=escalation_predicates,
        dependency_waves=waves,
    )


_PLANNER_SYSTEM_PROMPT = """You produce a bounded local-first execution plan.
Return only JSON matching the supplied schema. Prefer independent local slices,
include observable acceptance checks, serialize overlapping write paths, and
never schedule frontier_review as a task. Frontier review is a conditional
escalation predicate only. Use direct mode for one indivisible unit. Maximum six
slices."""


def _response_content(response: Any) -> str:
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise PlanValidationError("planner returned no response content") from exc
    if not isinstance(content, str) or not content.strip():
        raise PlanValidationError("planner returned empty response content")
    return content


def generate_execution_plan(
    request: str,
    *,
    client: Any,
    model: str,
    context: str = "",
    max_repair_attempts: int = 1,
    timeout: int = 180,
    extra_body: Mapping[str, Any] | None = None,
) -> ExecutionPlan:
    """Generate and compile a plan, allowing at most one bounded repair by default."""
    if max_repair_attempts < 0 or max_repair_attempts > 1:
        raise ValueError("max_repair_attempts must be 0 or 1")
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _PLANNER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Request:\n{request.strip()}\n\nContext:\n{context.strip() or '(none)'}",
        },
    ]
    last_error: PlanValidationError | None = None
    for attempt in range(max_repair_attempts + 1):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0,
            max_tokens=5000,
            timeout=timeout,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "execution_plan",
                    "strict": True,
                    "schema": EXECUTION_PLAN_GENERATION_SCHEMA,
                },
            },
            extra_body=dict(extra_body or {}) or None,
        )
        raw_content = _response_content(response)
        try:
            decoded = json.loads(raw_content)
            return compile_execution_plan(decoded)
        except json.JSONDecodeError as exc:
            last_error = PlanValidationError(f"planner returned malformed JSON: {exc.msg}")
        except PlanValidationError as exc:
            last_error = exc
        if attempt < max_repair_attempts:
            messages.extend(
                [
                    {"role": "assistant", "content": raw_content},
                    {
                        "role": "user",
                        "content": (
                            "Validation failed: "
                            f"{last_error}. Return a corrected complete plan only."
                        ),
                    },
                ]
            )
    assert last_error is not None
    raise last_error
