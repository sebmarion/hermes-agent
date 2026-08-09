"""Plan-backed Claude Code transport for host-owned BestPlan lanes.

This adapter deliberately invokes Anthropic's official Claude Code CLI with
the user's saved ``claude.ai`` subscription login.  It never accepts an API
key, bearer override, alternate base URL, or cloud-provider routing flag.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping


class ClaudeCodePlanUnavailable(RuntimeError):
    """Raised when the official Claude plan transport cannot be used safely."""


_FORBIDDEN_AUTH_ENV = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_AWS_API_KEY",
        "ANTHROPIC_AWS_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
        "ANTHROPIC_FOUNDRY_API_KEY",
        "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
        "ANTHROPIC_FOUNDRY_BASE_URL",
        "ANTHROPIC_FOUNDRY_RESOURCE",
        "ANTHROPIC_GOOGLE_CLOUD_BASE_URL",
        "ANTHROPIC_IDENTITY_TOKEN",
        "ANTHROPIC_IDENTITY_TOKEN_FILE",
        "ANTHROPIC_UNIX_SOCKET",
        "ANTHROPIC_VERTEX_BASE_URL",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLAUDE_CODE_API_BASE_URL",
        "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
        "CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL",
        "CLAUDE_CODE_CLIENT_KEY",
        "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE",
        "CLAUDE_CODE_CUSTOM_OAUTH_URL",
        "CLAUDE_CODE_HFI_BEARER_TOKEN",
        "CLAUDE_CODE_HOST_AUTH_ENV_VAR",
        "CLAUDE_CODE_OAUTH_CLIENT_ID",
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
        "CLAUDE_CODE_OAUTH_SCOPES",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        "CLAUDE_CODE_SESSION_ACCESS_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_WEBSOCKET_AUTH_FILE_DESCRIPTOR",
    }
)
_FORBIDDEN_AUTH_ENV_PREFIXES = ("ANTHROPIC_",)
_SUPPORTED_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_READ_ONLY_TOOLS = "Read,Glob,Grep,WebFetch,WebSearch"
_AUTH_PROBE_POLL_SECONDS = 0.05
_PROCESS_TERM_TIMEOUT_SECONDS = 1.0
_PROCESS_KILL_TIMEOUT_SECONDS = 1.0


class _ClaudePlanAuthCancelled(RuntimeError):
    pass


def claude_plan_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an environment that can only use Claude Code's saved plan login."""
    env = dict(os.environ if source is None else source)
    for name in tuple(env):
        if name in _FORBIDDEN_AUTH_ENV or name.startswith(_FORBIDDEN_AUTH_ENV_PREFIXES):
            env.pop(name, None)
    return env


def find_claude_executable(executable: str | None = None) -> str:
    """Resolve the Claude Code executable without invoking a shell."""
    candidates: list[str] = []
    if executable:
        candidates.append(executable)
    configured = os.environ.get("HERMES_CLAUDE_CLI_PATH")
    if configured:
        candidates.append(configured)
    discovered = shutil.which("claude")
    if discovered:
        candidates.append(discovered)
    candidates.extend(
        [
            str(Path.home() / ".local" / "bin" / "claude"),
            "/opt/homebrew/bin/claude",
            "/usr/local/bin/claude",
        ]
    )
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise ClaudeCodePlanUnavailable("Claude Code executable is unavailable")


def probe_claude_plan_auth(
    *,
    executable: str | None = None,
    timeout: float = 15.0,
    cancel_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Require a logged-in first-party ``claude.ai`` subscription session."""
    binary = find_claude_executable(executable)
    process: subprocess.Popen[str] | None = None
    try:
        if cancel_requested is not None and cancel_requested():
            raise _ClaudePlanAuthCancelled
        process = subprocess.Popen(
            [binary, "auth", "status", "--json"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=claude_plan_environment(),
            start_new_session=(os.name == "posix"),
        )
        stdout, _stderr = _communicate_auth_probe(
            process,
            timeout=timeout,
            cancel_requested=cancel_requested,
        )
        returncode = process.returncode
    except _ClaudePlanAuthCancelled as exc:
        raise ClaudeCodePlanUnavailable("Claude plan login verification was cancelled") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise ClaudeCodePlanUnavailable("Claude plan login could not be verified") from exc
    except Exception as exc:
        raise ClaudeCodePlanUnavailable("Claude plan login could not be verified") from exc
    finally:
        if process is not None:
            try:
                _terminate_process_group(process)
            finally:
                _close_process_pipes(process)
    try:
        status = json.loads(stdout) if returncode == 0 else None
    except (TypeError, json.JSONDecodeError):
        status = None
    if not isinstance(status, dict) or not (
        status.get("loggedIn") is True
        and status.get("authMethod") == "claude.ai"
        and status.get("apiProvider") == "firstParty"
    ):
        raise ClaudeCodePlanUnavailable(
            "Claude plan login must be an authenticated first-party claude.ai session"
        )
    return status


def resolve_claude_code_plan_runtime(
    *,
    model: str,
    executable: str | None = None,
    auth_timeout: float = 15.0,
    cancel_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Resolve a keyless BestPlan runtime backed by official Claude Code."""
    target_model = str(model or "").strip()
    if not target_model:
        raise ClaudeCodePlanUnavailable("Claude plan model is missing")
    binary = find_claude_executable(executable)
    probe_claude_plan_auth(
        executable=binary,
        timeout=auth_timeout,
        cancel_requested=cancel_requested,
    )
    return {
        "provider": "anthropic",
        "requested_provider": "anthropic",
        "model": target_model,
        "api_mode": "claude_code",
        "base_url": None,
        "api_key": None,
        "executable": binary,
    }


def _signal_process_group(process: subprocess.Popen[str], sig: int) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, sig)
        elif process.poll() is not None:
            return
        elif sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        pass


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    """Bound process-group shutdown and reap the Claude CLI process."""
    _signal_process_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=_PROCESS_TERM_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    else:
        if os.name != "posix":
            return
    _signal_process_group(process, signal.SIGKILL)
    try:
        process.wait(timeout=_PROCESS_KILL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def _close_process_pipes(process: subprocess.Popen[str]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _communicate_auth_probe(
    process: subprocess.Popen[str],
    *,
    timeout: float,
    cancel_requested: Callable[[], bool] | None,
) -> tuple[str, str]:
    timeout_seconds = float(timeout)
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        if cancel_requested is not None and cancel_requested():
            raise _ClaudePlanAuthCancelled
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout_seconds)
        try:
            return process.communicate(
                timeout=min(_AUTH_PROBE_POLL_SECONDS, remaining)
            )
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                continue
            # The CLI leader has exited, but one of its descendants still owns
            # an inherited pipe. Kill the isolated group, then drain the status.
            _terminate_process_group(process)
            return process.communicate(timeout=_PROCESS_KILL_TIMEOUT_SECONDS)


class ClaudeCodePlanChild:
    """Minimal ``AIAgent``-compatible child around ``claude --print``."""

    def __init__(
        self,
        *,
        executable: str,
        model: str,
        reasoning_effort: str,
        workspace: str | Path,
        tools_enabled: bool = True,
    ) -> None:
        effort = str(reasoning_effort or "").strip().lower()
        if effort not in _SUPPORTED_EFFORTS:
            raise ClaudeCodePlanUnavailable(
                "Claude Code plan transport requires low, medium, high, xhigh, or max effort"
            )
        self.executable = find_claude_executable(executable)
        self.model = str(model or "").strip()
        self.reasoning_effort = effort
        self.tools_enabled = bool(tools_enabled)
        self.workspace = Path(workspace).expanduser().resolve()
        if not self.workspace.is_dir():
            raise ClaudeCodePlanUnavailable("Claude Code plan workspace is unavailable")
        self._process: subprocess.Popen[str] | None = None
        self._process_lock = threading.Lock()
        self._stop_requested = threading.Event()

    def _command(self) -> list[str]:
        return [
            self.executable,
            "--print",
            "--safe-mode",
            "--no-chrome",
            "--no-session-persistence",
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--mcp-config",
            "{}",
            "--permission-mode",
            "plan",
            "--tools",
            _READ_ONLY_TOOLS if self.tools_enabled else "",
            "--output-format",
            "text",
            "--model",
            self.model,
            "--effort",
            self.reasoning_effort,
        ]

    def run_conversation(self, prompt: str) -> dict[str, str]:
        if self._stop_requested.is_set():
            raise ClaudeCodePlanUnavailable("Claude Code plan turn was cancelled")
        try:
            process = subprocess.Popen(
                self._command(),
                cwd=str(self.workspace),
                env=claude_plan_environment(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            raise ClaudeCodePlanUnavailable("Claude Code plan turn could not start") from exc
        with self._process_lock:
            self._process = process
            stop_requested = self._stop_requested.is_set()
        if stop_requested:
            _terminate_process_group(process)
        try:
            stdout, _stderr = process.communicate(str(prompt))
        finally:
            try:
                _terminate_process_group(process)
            finally:
                with self._process_lock:
                    if self._process is process:
                        self._process = None
        if process.returncode != 0:
            raise ClaudeCodePlanUnavailable("Claude Code plan turn failed")
        response = str(stdout or "").strip()
        if not response:
            raise ClaudeCodePlanUnavailable("Claude Code plan turn returned no output")
        return {"final_response": response}

    def hard_interrupt(self, _message: str | None = None) -> None:
        self._stop_requested.set()
        with self._process_lock:
            process = self._process
        if process is not None:
            _terminate_process_group(process)

    def interrupt(self, message: str | None = None) -> None:
        self.hard_interrupt(message)

    def clear_interrupt(self, **_kwargs: Any) -> bool:
        with self._process_lock:
            if self._process is not None:
                return False
            was_requested = self._stop_requested.is_set()
            self._stop_requested.clear()
        return was_requested

    def close(self) -> None:
        self._stop_requested.set()
        with self._process_lock:
            process = self._process
        if process is not None:
            _terminate_process_group(process)
