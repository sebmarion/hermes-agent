import json
import os
import signal
import threading
import time
from pathlib import Path

import pytest

from agent.claude_code_plan import (
    ClaudeCodePlanChild,
    ClaudeCodePlanUnavailable,
    probe_claude_plan_auth,
    resolve_claude_code_plan_runtime,
)


_FORBIDDEN_AUTH_ENV = (
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
)


def _write_fake_claude(tmp_path: Path, source: str) -> Path:
    executable = tmp_path / "claude"
    executable.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
    executable.chmod(0o755)
    return executable


def _write_auth_claude_with_heartbeat_descendant(
    tmp_path: Path,
    heartbeat: Path,
    *,
    output: str | None,
) -> Path:
    descendant_source = (
        "import pathlib, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"path = pathlib.Path({str(heartbeat)!r})\n"
        "deadline = time.monotonic() + 5\n"
        "while time.monotonic() < deadline:\n"
        "    path.write_text(str(time.monotonic()))\n"
        "    time.sleep(0.05)\n"
    )
    finish = (
        f"print({output!r}, flush=True)\n"
        if output is not None
        else "time.sleep(5)\n"
    )
    return _write_fake_claude(
        tmp_path,
        "import pathlib, subprocess, sys, time\n"
        f"heartbeat = pathlib.Path({str(heartbeat)!r})\n"
        f"subprocess.Popen([sys.executable, '-c', {descendant_source!r}])\n"
        "deadline = time.monotonic() + 2\n"
        "while not heartbeat.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "if not heartbeat.exists():\n"
        "    raise SystemExit(70)\n"
        + finish,
    )


def _assert_heartbeat_stopped(heartbeat: Path) -> None:
    assert heartbeat.exists()
    before = heartbeat.stat().st_mtime_ns
    time.sleep(0.4)
    assert heartbeat.stat().st_mtime_ns == before


def test_plan_auth_probe_uses_saved_first_party_login_without_api_overrides(
    tmp_path, monkeypatch
):
    executable = _write_fake_claude(
        tmp_path,
        """
import json, os, sys
forbidden = %r
if sys.argv[1:] != ["auth", "status", "--json"]:
    raise SystemExit(64)
overrides = [name for name in forbidden if os.environ.get(name)]
print(json.dumps({
    "loggedIn": not overrides,
    "authMethod": "claude.ai" if not overrides else "apiKey",
    "apiProvider": "firstParty" if not overrides else "console",
    "email": "company@example.com",
    "orgName": "Company",
    "subscriptionType": "team",
}))
"""
        % (_FORBIDDEN_AUTH_ENV,),
    )
    for name in _FORBIDDEN_AUTH_ENV:
        monkeypatch.setenv(name, "must-not-reach-claude")

    status = probe_claude_plan_auth(executable=str(executable))

    assert status["loggedIn"] is True
    assert status["authMethod"] == "claude.ai"
    assert status["apiProvider"] == "firstParty"


@pytest.mark.parametrize(
    "status",
    [
        {"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"},
        {"loggedIn": True, "authMethod": "apiKey", "apiProvider": "firstParty"},
        {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "console"},
    ],
)
def test_plan_auth_probe_rejects_non_subscription_auth(tmp_path, status):
    executable = _write_fake_claude(
        tmp_path,
        "import json\nprint(json.dumps(%r))\n" % (status,),
    )

    with pytest.raises(ClaudeCodePlanUnavailable, match="Claude plan login"):
        probe_claude_plan_auth(executable=str(executable))


def test_plan_runtime_is_keyless_and_keeps_exact_model(tmp_path):
    executable = _write_fake_claude(
        tmp_path,
        """
import json
print(json.dumps({
    "loggedIn": True,
    "authMethod": "claude.ai",
    "apiProvider": "firstParty",
    "subscriptionType": "team",
}))
""",
    )

    runtime = resolve_claude_code_plan_runtime(
        model="claude-fable-5",
        executable=str(executable),
    )

    assert runtime == {
        "provider": "anthropic",
        "requested_provider": "anthropic",
        "model": "claude-fable-5",
        "api_mode": "claude_code",
        "base_url": None,
        "api_key": None,
        "executable": str(executable),
    }


def test_plan_child_uses_print_mode_read_only_tools_and_stdin_prompt(
    tmp_path, monkeypatch
):
    executable = _write_fake_claude(
        tmp_path,
        """
import json, os, sys
prompt = sys.stdin.read()
print(json.dumps({
    "argv": sys.argv[1:],
    "prompt": prompt,
    "forbidden_env": {
        name: os.environ[name]
        for name in %r
        if os.environ.get(name)
    },
}))
"""
        % (_FORBIDDEN_AUTH_ENV,),
    )
    for name in _FORBIDDEN_AUTH_ENV:
        monkeypatch.setenv(name, "must-not-reach-claude")
    prompt = "HERMES_BESTPLAN_CANDIDATE_V1 inspect --not-an-option"
    child = ClaudeCodePlanChild(
        executable=str(executable),
        model="claude-opus-5",
        reasoning_effort="xhigh",
        workspace=tmp_path,
    )

    result = child.run_conversation(prompt)
    payload = json.loads(result["final_response"])
    argv = payload["argv"]

    assert payload["prompt"] == prompt
    assert prompt not in argv
    assert payload["forbidden_env"] == {}
    assert "--print" in argv
    assert "--safe-mode" in argv
    assert "--no-chrome" in argv
    assert "--no-session-persistence" in argv
    assert "--disable-slash-commands" in argv
    assert "--strict-mcp-config" in argv
    assert json.loads(argv[argv.index("--mcp-config") + 1]) == {
        "mcpServers": {},
    }
    assert argv[argv.index("--model") + 1] == "claude-opus-5"
    assert argv[argv.index("--effort") + 1] == "xhigh"
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep,WebFetch,WebSearch"
    assert argv[argv.index("--output-format") + 1] == "text"


def test_plan_repair_child_disables_every_tool(tmp_path):
    executable = _write_fake_claude(
        tmp_path,
        """
import json, sys
sys.stdin.read()
print(json.dumps({"argv": sys.argv[1:]}))
""",
    )
    child = ClaudeCodePlanChild(
        executable=str(executable),
        model="claude-opus-5",
        reasoning_effort="xhigh",
        workspace=tmp_path,
        tools_enabled=False,
    )

    payload = json.loads(child.run_conversation("repair")["final_response"])

    assert payload["argv"][payload["argv"].index("--tools") + 1] == ""


def test_stop_before_popen_assignment_terminates_new_process(tmp_path, monkeypatch):
    from agent import claude_code_plan

    executable = _write_fake_claude(tmp_path, "raise SystemExit(99)\n")
    popen_entered = threading.Event()
    release_popen = threading.Event()
    communicate_entered = threading.Event()
    terminated = threading.Event()

    class FakeProcess:
        pid = 424242
        returncode = None

        def poll(self):
            return self.returncode

        def communicate(self, _prompt):
            communicate_entered.set()
            terminated.wait(timeout=5)
            self.returncode = -15
            return "", ""

        def wait(self, timeout=None):
            if not terminated.wait(timeout=timeout):
                raise claude_code_plan.subprocess.TimeoutExpired("claude", timeout)
            self.returncode = -15
            return self.returncode

    process = FakeProcess()

    def fake_popen(*_args, **_kwargs):
        popen_entered.set()
        release_popen.wait(timeout=5)
        return process

    monkeypatch.setattr(claude_code_plan.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        claude_code_plan,
        "_signal_process_group",
        lambda _process, _signal: terminated.set(),
    )
    child = ClaudeCodePlanChild(
        executable=str(executable),
        model="claude-opus-5",
        reasoning_effort="xhigh",
        workspace=tmp_path,
    )
    errors = []

    def run():
        try:
            child.run_conversation("wait")
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert popen_entered.wait(timeout=2)

    child.hard_interrupt("stop during spawn")
    release_popen.set()
    assert communicate_entered.wait(timeout=2)
    worker.join(timeout=1)
    try:
        assert not worker.is_alive()
        assert terminated.is_set()
        assert errors and isinstance(errors[0], ClaudeCodePlanUnavailable)
    finally:
        terminated.set()
        worker.join(timeout=2)


def test_hard_interrupt_escalates_and_reaps_process_group(tmp_path, monkeypatch):
    from agent import claude_code_plan

    executable = _write_fake_claude(tmp_path, "raise SystemExit(99)\n")
    signals = []

    class FakeProcess:
        pid = 424242
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if not signals or signals[-1] != claude_code_plan.signal.SIGKILL:
                raise claude_code_plan.subprocess.TimeoutExpired("claude", timeout)
            self.returncode = -claude_code_plan.signal.SIGKILL
            return self.returncode

    process = FakeProcess()
    monkeypatch.setattr(
        claude_code_plan,
        "_signal_process_group",
        lambda observed, sig: signals.append(sig) if observed is process else None,
    )
    child = ClaudeCodePlanChild(
        executable=str(executable),
        model="claude-opus-5",
        reasoning_effort="xhigh",
        workspace=tmp_path,
    )
    child._process = process

    child.hard_interrupt("deadline")

    assert signals == [
        claude_code_plan.signal.SIGTERM,
        claude_code_plan.signal.SIGKILL,
    ]
    assert process.returncode == -claude_code_plan.signal.SIGKILL


def test_plan_child_hard_interrupt_terminates_inflight_cli(tmp_path):
    ready = tmp_path / "ready"
    executable = _write_fake_claude(
        tmp_path,
        """
import pathlib, sys, time
pathlib.Path(%r).write_text("ready")
sys.stdin.read()
time.sleep(30)
""" % (str(ready),),
    )
    child = ClaudeCodePlanChild(
        executable=str(executable),
        model="claude-opus-5",
        reasoning_effort="xhigh",
        workspace=tmp_path,
    )
    errors = []

    def run():
        try:
            child.run_conversation("wait")
        except Exception as exc:  # the interrupted process must fail closed
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 3
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists()

    child.hard_interrupt("test stop")
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert errors and isinstance(errors[0], ClaudeCodePlanUnavailable)
    child.close()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
@pytest.mark.parametrize("returncode", [0, 7])
def test_plan_child_tears_down_stdio_detached_descendant_after_cli_return(
    tmp_path, returncode
):
    heartbeat = tmp_path / f"turn-descendant-heartbeat-{returncode}"
    process_group = tmp_path / f"turn-process-group-{returncode}"
    descendant_source = (
        "import pathlib, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"path = pathlib.Path({str(heartbeat)!r})\n"
        "deadline = time.monotonic() + 5\n"
        "while time.monotonic() < deadline:\n"
        "    path.write_text(str(time.monotonic()))\n"
        "    time.sleep(0.05)\n"
    )
    executable = _write_fake_claude(
        tmp_path,
        "import os, pathlib, subprocess, sys, time\n"
        f"pathlib.Path({str(process_group)!r}).write_text(str(os.getpid()))\n"
        f"heartbeat = pathlib.Path({str(heartbeat)!r})\n"
        "subprocess.Popen(\n"
        f"    [sys.executable, '-c', {descendant_source!r}],\n"
        "    stdin=subprocess.DEVNULL,\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        "deadline = time.monotonic() + 2\n"
        "while not heartbeat.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "if not heartbeat.exists():\n"
        "    raise SystemExit(70)\n"
        "print('finished', flush=True)\n"
        f"raise SystemExit({returncode})\n",
    )
    child = ClaudeCodePlanChild(
        executable=str(executable),
        model="claude-opus-5",
        reasoning_effort="xhigh",
        workspace=tmp_path,
    )

    try:
        if returncode:
            with pytest.raises(ClaudeCodePlanUnavailable, match="turn failed"):
                child.run_conversation("wait")
        else:
            assert child.run_conversation("wait") == {"final_response": "finished"}
        _assert_heartbeat_stopped(heartbeat)
    finally:
        if process_group.exists():
            try:
                os.killpg(int(process_group.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_hard_interrupt_kills_descendant_after_cli_leader_exits(tmp_path):
    ready = tmp_path / "descendant-ready"
    descendant_source = (
        "import os, pathlib, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    executable = _write_fake_claude(
        tmp_path,
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, '-c', {descendant_source!r}])\n"
        "raise SystemExit(0)\n",
    )
    child = ClaudeCodePlanChild(
        executable=str(executable),
        model="claude-opus-5",
        reasoning_effort="xhigh",
        workspace=tmp_path,
    )
    errors = []

    def run():
        try:
            child.run_conversation("wait")
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    process = None
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with child._process_lock:
                process = child._process
            if process is not None and process.poll() is not None:
                break
            time.sleep(0.01)
        assert process is not None
        assert process.poll() == 0
        assert worker.is_alive()

        child.hard_interrupt("leader already exited")
        worker.join(timeout=3)
        assert not worker.is_alive()
        assert errors and isinstance(errors[0], ClaudeCodePlanUnavailable)
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        worker.join(timeout=3)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_auth_probe_timeout_kills_cli_descendant_process_group(tmp_path):
    heartbeat = tmp_path / "auth-descendant-heartbeat"
    descendant_source = (
        "import os, pathlib, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"path = pathlib.Path({str(heartbeat)!r})\n"
        "deadline = time.monotonic() + 5\n"
        "while time.monotonic() < deadline:\n"
        "    path.write_text(str(time.monotonic()))\n"
        "    time.sleep(0.05)\n"
    )
    executable = _write_fake_claude(
        tmp_path,
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {descendant_source!r}])\n"
        "time.sleep(5)\n",
    )
    started = time.monotonic()
    with pytest.raises(ClaudeCodePlanUnavailable, match="verified"):
        probe_claude_plan_auth(executable=str(executable), timeout=1.0)
    assert time.monotonic() - started < 3
    assert heartbeat.exists()
    before = heartbeat.stat().st_mtime_ns
    time.sleep(0.4)
    assert heartbeat.stat().st_mtime_ns == before


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_auth_probe_success_kills_cli_descendant_process_group(tmp_path):
    heartbeat = tmp_path / "auth-success-descendant-heartbeat"
    status = json.dumps(
        {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "subscriptionType": "team",
        }
    )
    executable = _write_auth_claude_with_heartbeat_descendant(
        tmp_path,
        heartbeat,
        output=status,
    )

    started = time.monotonic()
    observed = probe_claude_plan_auth(executable=str(executable), timeout=3.0)

    assert observed["subscriptionType"] == "team"
    assert time.monotonic() - started < 2
    _assert_heartbeat_stopped(heartbeat)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_auth_probe_invalid_status_kills_cli_descendant_process_group(tmp_path):
    heartbeat = tmp_path / "auth-invalid-descendant-heartbeat"
    executable = _write_auth_claude_with_heartbeat_descendant(
        tmp_path,
        heartbeat,
        output="not-json",
    )

    started = time.monotonic()
    with pytest.raises(ClaudeCodePlanUnavailable, match="Claude plan login"):
        probe_claude_plan_auth(executable=str(executable), timeout=3.0)

    assert time.monotonic() - started < 2
    _assert_heartbeat_stopped(heartbeat)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_auth_probe_cancel_kills_cli_descendant_process_group(tmp_path):
    heartbeat = tmp_path / "auth-cancel-descendant-heartbeat"
    executable = _write_auth_claude_with_heartbeat_descendant(
        tmp_path,
        heartbeat,
        output=None,
    )

    started = time.monotonic()
    with pytest.raises(ClaudeCodePlanUnavailable, match="cancelled"):
        probe_claude_plan_auth(
            executable=str(executable),
            timeout=3.0,
            cancel_requested=heartbeat.exists,
        )

    assert time.monotonic() - started < 2
    _assert_heartbeat_stopped(heartbeat)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_auth_probe_callback_exception_kills_cli_descendant_process_group(tmp_path):
    heartbeat = tmp_path / "auth-exception-descendant-heartbeat"
    executable = _write_auth_claude_with_heartbeat_descendant(
        tmp_path,
        heartbeat,
        output=None,
    )

    def broken_cancel_callback() -> bool:
        if heartbeat.exists():
            raise RuntimeError("callback failed")
        return False

    started = time.monotonic()
    with pytest.raises(ClaudeCodePlanUnavailable, match="verified"):
        probe_claude_plan_auth(
            executable=str(executable),
            timeout=3.0,
            cancel_requested=broken_cancel_callback,
        )

    assert time.monotonic() - started < 2
    _assert_heartbeat_stopped(heartbeat)


def test_plan_runtime_forwards_auth_cancel_callback(tmp_path):
    executable = _write_fake_claude(tmp_path, "raise SystemExit(99)\n")

    with pytest.raises(ClaudeCodePlanUnavailable, match="cancelled"):
        resolve_claude_code_plan_runtime(
            model="claude-opus-5",
            executable=str(executable),
            cancel_requested=lambda: True,
        )
