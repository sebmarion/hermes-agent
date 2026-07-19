"""Hermes middleware contract helpers.

Observer hooks report what happened. Middleware can change what happens by
rewriting a request or wrapping the actual execution callback. Keep the small
contract helpers here so agent-loop call sites and plugins share one vocabulary.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import time
from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

from hermes_cli.tool_policy import (
    PolicyDecisionCode,
    PreparedToolRuntime,
    RequiredPolicyFailureCode,
    ToolDispatchPolicyInput,
    ToolPolicyBlock,
    create_tool_dispatch_policy_input,
)

logger = logging.getLogger(__name__)

OBSERVER_SCHEMA_VERSION = "hermes.observer.v1"
MIDDLEWARE_SCHEMA_VERSION = "hermes.middleware.v1"

TOOL_REQUEST_MIDDLEWARE = "tool_request"
TOOL_EXECUTION_MIDDLEWARE = "tool_execution"
LLM_REQUEST_MIDDLEWARE = "llm_request"
LLM_EXECUTION_MIDDLEWARE = "llm_execution"

# Back-compat aliases for older PoC branches that used API terminology.
API_REQUEST_MIDDLEWARE = LLM_REQUEST_MIDDLEWARE
API_EXECUTION_MIDDLEWARE = LLM_EXECUTION_MIDDLEWARE

VALID_MIDDLEWARE: set[str] = {
    TOOL_REQUEST_MIDDLEWARE,
    TOOL_EXECUTION_MIDDLEWARE,
    LLM_REQUEST_MIDDLEWARE,
    LLM_EXECUTION_MIDDLEWARE,
}


@dataclass
class RequestMiddlewareResult:
    """Result of applying request middleware to a mutable payload."""

    payload: Any
    original_payload: Any
    changed: bool = False
    trace: List[Dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class _AuthorizedToolDispatch:
    """Process-local, one-use proof for the registry terminal call."""

    pid: int
    policy_input: ToolDispatchPolicyInput
    prepared_runtime: PreparedToolRuntime
    active: bool = True
    registry_consumed: bool = False


@dataclass(slots=True)
class ToolDispatchDelegation:
    """Original request data and allow-only callback for a nested dispatch."""

    pid: int
    original_args: Dict[str, Any]
    on_authorized: Optional[Callable[[Dict[str, Any]], None]] = None
    active: bool = True


@dataclass(frozen=True, slots=True)
class RequiredPolicyBlockRecord:
    """Trusted host observation for one serialized policy-block result."""

    tool_call_id: str
    block: ToolPolicyBlock


class RequiredPolicyBlockCollector:
    """Thread-safe per-batch store for host-created policy blocks."""

    def __init__(self) -> None:
        self._pid = os.getpid()
        self._active = True
        self._lock = threading.Lock()
        self._records: dict[str, RequiredPolicyBlockRecord] = {}

    def record(self, tool_call_id: str, block: ToolPolicyBlock) -> bool:
        """Keep the first trusted block for *tool_call_id*."""
        if type(tool_call_id) is not str or not isinstance(block, ToolPolicyBlock):
            return False
        with self._lock:
            if not self._active or self._pid != os.getpid():
                return False
            if tool_call_id in self._records:
                return False
            self._records[tool_call_id] = RequiredPolicyBlockRecord(
                tool_call_id=tool_call_id,
                block=block,
            )
            return True

    def get(self, tool_call_id: str) -> RequiredPolicyBlockRecord | None:
        """Return the trusted record for *tool_call_id*, if observed."""
        with self._lock:
            return self._records.get(tool_call_id)

    def first_terminal(
        self,
        tool_call_ids: Iterable[str],
    ) -> RequiredPolicyBlockRecord | None:
        """Choose the first non-recoverable block in assistant call order."""
        with self._lock:
            records = dict(self._records)
        for tool_call_id in tool_call_ids:
            record = records.get(tool_call_id)
            if (
                record is not None
                and record.block.policy_code != PolicyDecisionCode.BLOCKED
            ):
                return record
        return None

    def close(self) -> None:
        """Prevent copied contexts from recording after the batch exits."""
        with self._lock:
            self._active = False


_AUTHORIZED_TOOL_DISPATCH: ContextVar[_AuthorizedToolDispatch | None] = ContextVar(
    "HERMES_AUTHORIZED_TOOL_DISPATCH",
    default=None,
)
_TOOL_DISPATCH_DELEGATION: ContextVar[ToolDispatchDelegation | None] = ContextVar(
    "HERMES_TOOL_DISPATCH_DELEGATION",
    default=None,
)
_REQUIRED_POLICY_BLOCK_COLLECTOR: ContextVar[
    RequiredPolicyBlockCollector | None
] = ContextVar(
    "HERMES_REQUIRED_POLICY_BLOCK_COLLECTOR",
    default=None,
)


@contextmanager
def bind_required_policy_block_collector(
) -> Iterator[RequiredPolicyBlockCollector]:
    """Bind one trusted policy-block collector around a tool-call batch."""
    collector = RequiredPolicyBlockCollector()
    token = _REQUIRED_POLICY_BLOCK_COLLECTOR.set(collector)
    try:
        yield collector
    finally:
        collector.close()
        _REQUIRED_POLICY_BLOCK_COLLECTOR.reset(token)


def record_required_policy_block(
    tool_call_id: str,
    block: ToolPolicyBlock,
) -> bool:
    """Record a host-created block when a batch collector is active."""
    collector = _REQUIRED_POLICY_BLOCK_COLLECTOR.get()
    if collector is None:
        return False
    return collector.record(tool_call_id, block)


def get_required_policy_block_record(
    tool_call_id: str,
) -> RequiredPolicyBlockRecord | None:
    """Return a trusted block record from the active batch collector."""
    collector = _REQUIRED_POLICY_BLOCK_COLLECTOR.get()
    if collector is None:
        return None
    return collector.get(tool_call_id)


def _active_authorized_tool_dispatch() -> _AuthorizedToolDispatch | None:
    authorized = _AUTHORIZED_TOOL_DISPATCH.get()
    if (
        authorized is None
        or authorized.pid != os.getpid()
        or not authorized.active
    ):
        return None
    return authorized


def get_authorized_tool_dispatch() -> ToolDispatchPolicyInput | None:
    """Return the active final-dispatch authorization, if any."""
    authorized = _active_authorized_tool_dispatch()
    return authorized.policy_input if authorized is not None else None


def get_tool_dispatch_delegation() -> ToolDispatchDelegation | None:
    """Return active nested-dispatch metadata for the current process."""
    delegation = _TOOL_DISPATCH_DELEGATION.get()
    if (
        delegation is None
        or delegation.pid != os.getpid()
        or not delegation.active
    ):
        return None
    return delegation


@contextmanager
def bind_tool_dispatch_delegation(
    original_args: Dict[str, Any],
    on_authorized: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Iterator[ToolDispatchDelegation]:
    """Carry audit-original args to a nested final dispatch without authority."""
    delegation = ToolDispatchDelegation(
        pid=os.getpid(),
        original_args=deepcopy(original_args),
        on_authorized=on_authorized,
    )
    token = _TOOL_DISPATCH_DELEGATION.set(delegation)
    try:
        yield delegation
    finally:
        delegation.active = False
        _TOOL_DISPATCH_DELEGATION.reset(token)


@contextmanager
def _bind_authorized_tool_dispatch(
    policy_input: ToolDispatchPolicyInput,
    prepared_runtime: PreparedToolRuntime,
) -> Iterator[None]:
    authorized = _AuthorizedToolDispatch(
        pid=os.getpid(),
        policy_input=policy_input,
        prepared_runtime=prepared_runtime,
    )
    token = _AUTHORIZED_TOOL_DISPATCH.set(authorized)
    try:
        yield
    finally:
        authorized.active = False
        _AUTHORIZED_TOOL_DISPATCH.reset(token)


def _required_policy_configuration() -> tuple[bool, ToolPolicyBlock | None]:
    """Return whether a tool policy is required, failing closed on bad config."""
    try:
        from hermes_cli.plugins import _get_required_policies_for_module

        configured = _get_required_policies_for_module()
    except Exception:
        return False, ToolPolicyBlock(
            policy="tool_dispatch",
            policy_code=RequiredPolicyFailureCode.CONFIG_INVALID,
            message="Required policy configuration is invalid.",
        )
    if type(configured) is not dict:
        return False, ToolPolicyBlock(
            policy="tool_dispatch",
            policy_code=RequiredPolicyFailureCode.CONFIG_INVALID,
            message="Required policy configuration is invalid.",
        )
    return bool(configured), None


def _binding_block(policy_code: str, message: str) -> ToolPolicyBlock:
    return ToolPolicyBlock(
        policy="tool_dispatch",
        policy_code=policy_code,
        message=message,
    )


def registry_dispatch_policy_block(
    *,
    tool_name: str,
    args: dict,
    task_id: str,
    session_id: str,
    turn_id: str,
    tool_call_id: str,
    prepared_runtime: PreparedToolRuntime,
) -> ToolPolicyBlock | None:
    """Consume and verify the final authorization for registry dispatch."""
    required, config_block = _required_policy_configuration()
    if config_block is not None:
        return config_block
    if not required:
        return None

    authorized = _active_authorized_tool_dispatch()
    if authorized is None or authorized.registry_consumed:
        return _binding_block(
            PolicyDecisionCode.BINDING_MISSING,
            "Required policy authorization is missing.",
        )

    # The first registry attempt consumes the one-use authorization even if it
    # is malformed. A mismatched probe cannot be followed by a corrected retry.
    authorized.registry_consumed = True
    try:
        observed = create_tool_dispatch_policy_input(
            tool_name=tool_name,
            original_args=authorized.policy_input.original_args,
            effective_args=args,
            task_id=task_id,
            session_id=session_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            prepared_runtime=prepared_runtime,
        )
    except Exception:
        return _binding_block(
            PolicyDecisionCode.BINDING_MISMATCH,
            "Required policy authorization does not match this dispatch.",
        )
    if not hmac.compare_digest(
        authorized.policy_input.policy_binding,
        observed.policy_binding,
    ):
        return _binding_block(
            PolicyDecisionCode.BINDING_MISMATCH,
            "Required policy authorization does not match this dispatch.",
        )
    return None


def is_required_policy_block_result(result: object) -> bool:
    """Return True only for the structured required-policy block envelope."""
    if type(result) is not str:
        return False
    try:
        parsed = json.loads(result)
    except (TypeError, ValueError):
        return False
    return (
        type(parsed) is dict
        and parsed.get("status") == "blocked"
        and parsed.get("error_type") == "required_policy_block"
        and type(parsed.get("policy")) is str
        and type(parsed.get("policy_code")) is str
    )


def _emit_required_policy_block(
    *,
    tool_name: str,
    effective_args: dict,
    result: str,
    block: ToolPolicyBlock,
    task_id: str,
    session_id: str,
    turn_id: str,
    tool_call_id: str,
    api_request_id: str,
    duration_ms: int,
    middleware_trace: list[dict[str, Any]],
) -> None:
    record_required_policy_block(tool_call_id, block)
    try:
        from model_tools import _emit_post_tool_call_hook

        _emit_post_tool_call_hook(
            function_name=tool_name,
            function_args=effective_args,
            result=result,
            task_id=task_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            turn_id=turn_id,
            api_request_id=api_request_id,
            duration_ms=duration_ms,
            status="blocked",
            error_type=block.error_type,
            error_message=block.message,
            middleware_trace=list(middleware_trace),
        )
    except Exception as exc:
        logger.debug("required policy post_tool_call hook error: %s", exc)


def authorize_and_dispatch_tool(
    tool_name: str,
    effective_args: Dict[str, Any],
    next_call: Callable[[Dict[str, Any]], Any],
    *,
    original_args: Dict[str, Any],
    task_id: str,
    session_id: str,
    turn_id: str,
    tool_call_id: str,
    api_request_id: str = "",
    middleware_trace: Optional[List[Dict[str, Any]]] = None,
) -> Any:
    """Authorize the exact terminal payload, then invoke its handler once."""
    started = time.monotonic()
    trace = list(middleware_trace or [])
    try:
        from agent.tool_runtime_context import (
            bind_prepared_tool_runtime,
            prepare_tool_runtime,
        )
        from hermes_cli.plugins import authorize_required_tool_policies

        prepared_runtime = prepare_tool_runtime(
            tool_name,
            effective_args,
            task_id,
            session_id,
        )
        policy_input = create_tool_dispatch_policy_input(
            tool_name=tool_name,
            original_args=original_args,
            effective_args=effective_args,
            task_id=task_id,
            session_id=session_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            prepared_runtime=prepared_runtime,
        )
        block = authorize_required_tool_policies(policy_input)
        if block is not None and not isinstance(block, ToolPolicyBlock):
            block = ToolPolicyBlock(
                policy="tool_dispatch",
                policy_code=RequiredPolicyFailureCode.CALLBACK_ERROR,
                message="Required policy callback failed.",
            )
    except Exception:
        block = ToolPolicyBlock(
            policy="tool_dispatch",
            policy_code=RequiredPolicyFailureCode.CALLBACK_ERROR,
            message="Required policy dispatch preparation failed.",
        )

    if block is not None:
        result = json.dumps(block.to_result(), ensure_ascii=False)
        _emit_required_policy_block(
            tool_name=tool_name,
            effective_args=effective_args,
            result=result,
            block=block,
            task_id=task_id,
            session_id=session_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            api_request_id=api_request_id,
            duration_ms=int((time.monotonic() - started) * 1000),
            middleware_trace=trace,
        )
        return result

    with (
        _bind_authorized_tool_dispatch(policy_input, prepared_runtime),
        bind_prepared_tool_runtime(prepared_runtime),
    ):
        return next_call(effective_args)


def observer_payload(**kwargs: Any) -> Dict[str, Any]:
    kwargs.setdefault("telemetry_schema_version", OBSERVER_SCHEMA_VERSION)
    return kwargs


def middleware_payload(**kwargs: Any) -> Dict[str, Any]:
    kwargs.setdefault("telemetry_schema_version", OBSERVER_SCHEMA_VERSION)
    kwargs.setdefault("middleware_schema_version", MIDDLEWARE_SCHEMA_VERSION)
    return kwargs


def _safe_copy(payload: Any) -> Any:
    """Deep-copy a request payload, tolerating non-deepcopyable members.

    Request payloads are normally plain JSON-shaped dicts, but an LLM request
    can occasionally carry non-deepcopyable objects (clients, callbacks, file
    handles). A hard ``deepcopy`` failure there would otherwise abort the whole
    request-middleware pass. Fall back to a shallow ``dict`` copy so middleware
    still runs and the original nested objects are shared by reference rather
    than corrupting the live payload.
    """
    try:
        return deepcopy(payload)
    except Exception as exc:  # pragma: no cover - exercised via fallback test
        logger.debug("deepcopy failed for request payload (%s); using shallow copy", exc)
        if isinstance(payload, dict):
            return dict(payload)
        return payload


def apply_llm_request_middleware(
    request: Dict[str, Any],
    **context: Any,
) -> RequestMiddlewareResult:
    """Apply registered LLM request middleware.

    Middleware may return ``{"request": {...}}`` to replace the effective
    provider kwargs before Hermes sends them.
    """
    if not _has_middleware(LLM_REQUEST_MIDDLEWARE):
        return RequestMiddlewareResult(
            payload=request,
            original_payload=request,
            changed=False,
            trace=[],
        )

    original_request = _safe_copy(request)
    current_request = _safe_copy(original_request)
    trace: List[Dict[str, Any]] = []

    for result in _invoke_middleware(
        LLM_REQUEST_MIDDLEWARE,
        request=current_request,
        original_request=original_request,
        **context,
    ):
        if not isinstance(result, dict):
            continue
        next_request = result.get("request")
        if not isinstance(next_request, dict):
            continue
        current_request = _safe_copy(next_request)
        trace.append(_trace_entry(result))

    return RequestMiddlewareResult(
        payload=current_request,
        original_payload=original_request,
        changed=bool(trace),
        trace=trace,
    )


def apply_tool_request_middleware(
    tool_name: str,
    args: Dict[str, Any],
    **context: Any,
) -> RequestMiddlewareResult:
    """Apply registered tool request middleware.

    Middleware may return ``{"args": {...}}`` to replace the effective tool
    arguments before hooks, guardrails, approvals, and execution see them.
    """
    if not _has_middleware(TOOL_REQUEST_MIDDLEWARE):
        return RequestMiddlewareResult(
            payload=args,
            original_payload=args,
            changed=False,
            trace=[],
        )

    original_args = _safe_copy(args)
    current_args = _safe_copy(original_args)
    trace: List[Dict[str, Any]] = []

    for result in _invoke_middleware(
        TOOL_REQUEST_MIDDLEWARE,
        tool_name=tool_name,
        args=current_args,
        original_args=original_args,
        **context,
    ):
        if not isinstance(result, dict):
            continue
        next_args = result.get("args")
        if not isinstance(next_args, dict):
            continue
        current_args = _safe_copy(next_args)
        trace.append(_trace_entry(result))

    return RequestMiddlewareResult(
        payload=current_args,
        original_payload=original_args,
        changed=bool(trace),
        trace=trace,
    )


def apply_api_request_middleware(
    request: Dict[str, Any],
    **context: Any,
) -> RequestMiddlewareResult:
    """Compatibility wrapper for older ``api_request`` naming."""
    return apply_llm_request_middleware(request, **context)


def run_llm_execution_middleware(
    request: Dict[str, Any],
    next_call: Callable[[Dict[str, Any]], Any],
    **context: Any,
) -> Any:
    """Run provider execution through registered LLM execution middleware."""
    callbacks = _get_middleware_callbacks(LLM_EXECUTION_MIDDLEWARE)
    if not callbacks:
        return next_call(request)
    return _run_execution_chain(
        LLM_EXECUTION_MIDDLEWARE,
        callbacks,
        next_call,
        request=request,
        original_request=context.pop("original_request", request),
        **context,
    )


def run_tool_execution_middleware(
    tool_name: str,
    args: Dict[str, Any],
    next_call: Callable[[Dict[str, Any]], Any],
    **context: Any,
) -> Any:
    """Run tool execution through registered tool execution middleware."""
    original_args = context.pop("original_args", args)
    final_dispatch = context.pop("final_dispatch", True)
    middleware_trace = context.get("middleware_trace", [])

    def terminal_call(effective_args: Any) -> Any:
        final_args = effective_args if isinstance(effective_args, dict) else args
        if not final_dispatch:
            return next_call(final_args)
        return authorize_and_dispatch_tool(
            tool_name,
            final_args,
            next_call,
            original_args=(
                original_args if isinstance(original_args, dict) else args
            ),
            task_id=str(context.get("task_id") or ""),
            session_id=str(context.get("session_id") or ""),
            turn_id=str(context.get("turn_id") or ""),
            tool_call_id=str(context.get("tool_call_id") or ""),
            api_request_id=str(context.get("api_request_id") or ""),
            middleware_trace=(
                middleware_trace if isinstance(middleware_trace, list) else []
            ),
        )

    callbacks = _get_middleware_callbacks(TOOL_EXECUTION_MIDDLEWARE)
    if not callbacks:
        return terminal_call(args)
    return _run_execution_chain(
        TOOL_EXECUTION_MIDDLEWARE,
        callbacks,
        terminal_call,
        tool_name=tool_name,
        args=args,
        original_args=original_args,
        **context,
    )


def run_api_execution_middleware(
    request: Dict[str, Any],
    next_call: Callable[[Dict[str, Any]], Any],
    **context: Any,
) -> Any:
    """Compatibility wrapper for older ``api_execution`` naming."""
    return run_llm_execution_middleware(request, next_call, **context)


def _invoke_middleware(kind: str, **kwargs: Any) -> List[Any]:
    from hermes_cli.plugins import invoke_middleware

    return invoke_middleware(kind, **middleware_payload(**kwargs))


def _has_middleware(kind: str) -> bool:
    from hermes_cli.plugins import has_middleware

    return has_middleware(kind)


def _get_middleware_callbacks(kind: str) -> List[Callable]:
    from hermes_cli.plugins import get_plugin_manager

    return list(get_plugin_manager()._middleware.get(kind, []))


def _run_execution_chain(
    kind: str,
    callbacks: List[Callable],
    terminal_call: Callable[[Any], Any],
    **kwargs: Any,
) -> Any:
    payload_key = "request" if "request" in kwargs else "args"

    class _DownstreamExecutionError(Exception):
        def __init__(self, original: BaseException) -> None:
            super().__init__(str(original))
            self.original = original

    def call_at(index: int, payload: Any) -> Any:
        if index >= len(callbacks):
            return terminal_call(payload)

        callback = callbacks[index]
        next_called = False
        next_succeeded = False
        next_result: Any = None

        def next_call(next_payload: Any = None) -> Any:
            nonlocal next_called, next_succeeded, next_result
            # ``next_call`` is single-use per middleware frame. Calling it more
            # than once would re-run the downstream provider/tool, so a second
            # invocation is a contract violation rather than a retry. Surface it
            # instead of silently executing the terminal call twice.
            if next_called:
                raise RuntimeError(
                    f"Middleware '{kind}' callback "
                    f"{getattr(callback, '__name__', repr(callback))} called "
                    "next_call() more than once; downstream execution is single-use"
                )
            next_called = True
            try:
                next_result = call_at(index + 1, payload if next_payload is None else next_payload)
                next_succeeded = True
                return next_result
            except Exception as exc:
                raise _DownstreamExecutionError(exc) from exc

        call_kwargs = middleware_payload(**kwargs)
        call_kwargs[payload_key] = payload
        call_kwargs["next_call"] = next_call
        try:
            return callback(**call_kwargs)
        except _DownstreamExecutionError as exc:
            raise exc.original
        except Exception as exc:
            logger.warning(
                "Middleware '%s' callback %s raised: %s",
                kind,
                getattr(callback, "__name__", repr(callback)),
                exc,
            )
            if next_succeeded:
                return next_result
            if next_called:
                raise
            return call_at(index + 1, payload)

    return call_at(0, kwargs[payload_key])


def _trace_entry(result: Dict[str, Any]) -> Dict[str, Any]:
    entry: Dict[str, Any] = {}
    for key in ("source", "reason", "name"):
        value = result.get(key)
        if isinstance(value, str) and value:
            entry[key] = value
    if not entry:
        entry["source"] = "plugin"
    return entry
