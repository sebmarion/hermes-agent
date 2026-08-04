"""Transport layer types and registry for provider response normalization.

Usage:
    from agent.transports import get_transport
    transport = get_transport("anthropic_messages")
    result = transport.normalize_response(raw_response)
"""

import importlib

from agent.transports.types import (
    NormalizedResponse,
    ToolCall,
    Usage,
    build_tool_call,
    map_finish_reason,
)  # noqa: F401

_REGISTRY: dict = {}
_discovered: bool = False
_IMPORT_ERRORS: dict[str, ImportError] = {}
_BUILTIN_TRANSPORT_MODULES = {
    "anthropic_messages": "agent.transports.anthropic",
    "codex_responses": "agent.transports.codex",
    "chat_completions": "agent.transports.chat_completions",
    "bedrock_converse": "agent.transports.bedrock",
}


def register_transport(api_mode: str, transport_cls: type) -> None:
    """Register a transport class for an api_mode string."""
    _REGISTRY[api_mode] = transport_cls


def get_transport(api_mode: str):
    """Get a transport instance for the given api_mode.

    Returns None if api_mode is unknown. Import failures for a built-in
    transport are re-raised so callers see the real load failure instead of
    treating a broken built-in as an unregistered extension.
    """
    global _discovered
    if not _discovered:
        _discover_transports()
    cls = _REGISTRY.get(api_mode)
    if cls is None:
        # The registry can be partially populated when a specific transport
        # module was imported directly (for example chat_completions before
        # codex).  Discover on misses, not only when the registry is empty, so
        # test/order-dependent imports do not make valid api_modes unavailable.
        _discover_transports()
        cls = _REGISTRY.get(api_mode)
    if cls is None:
        import_error = _IMPORT_ERRORS.get(api_mode)
        if import_error is not None:
            raise import_error
        return None
    return cls()


def _discover_transports() -> None:
    """Import all transport modules to trigger auto-registration."""
    global _discovered
    _discovered = True
    for api_mode, module_name in _BUILTIN_TRANSPORT_MODULES.items():
        try:
            importlib.import_module(module_name)
        except ImportError as exc:
            _IMPORT_ERRORS[api_mode] = exc
        else:
            _IMPORT_ERRORS.pop(api_mode, None)
