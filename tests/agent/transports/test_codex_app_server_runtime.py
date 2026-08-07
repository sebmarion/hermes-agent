"""Tests for the optional codex app-server runtime gate.

These are unit tests for the api_mode rewriter and the wire-level transport
module. They do NOT require the `codex` CLI to be installed — that's
covered by a separate live test gated on `codex --version`.
"""

from __future__ import annotations

import pytest

from hermes_cli.runtime_provider import (
    _VALID_API_MODES,
    _maybe_apply_codex_app_server_runtime,
)


class TestApiModeRegistration:
    """The new api_mode must be registered or downstream parsing rejects it."""

    def test_codex_app_server_is_a_valid_api_mode(self) -> None:
        assert "codex_app_server" in _VALID_API_MODES

    def test_existing_api_modes_still_present(self) -> None:
        # Regression guard: don't accidentally delete other api_modes when
        # touching this set.
        for mode in (
            "chat_completions",
            "codex_responses",
            "anthropic_messages",
            "bedrock_converse",
        ):
            assert mode in _VALID_API_MODES


class TestMaybeApplyCodexAppServerRuntime:
    """The opt-in helper that rewrites api_mode → codex_app_server."""

    @pytest.mark.parametrize(
        "model_cfg",
        [
            None,
            {},
            {"openai_runtime": ""},
            {"openai_runtime": "auto"},
            {"openai_runtime": "AUTO"},
            {"other_key": "codex_app_server"},  # wrong key
        ],
    )
    def test_default_off_for_openai(self, model_cfg) -> None:
        """Default behavior is preserved when the flag is unset/auto."""
        got = _maybe_apply_codex_app_server_runtime(
            provider="openai", api_mode="chat_completions", model_cfg=model_cfg
        )
        assert got == "chat_completions"

    def test_opt_in_rewrites_openai(self) -> None:
        got = _maybe_apply_codex_app_server_runtime(
            provider="openai",
            api_mode="chat_completions",
            model_cfg={"openai_runtime": "codex_app_server"},
        )
        assert got == "codex_app_server"



    @pytest.mark.parametrize(
        "provider",
        [
            "anthropic",
            "openrouter",
            "xai",
            "qwen-oauth",
            "opencode-zen",
            "bedrock",
            "",
        ],
    )
    def test_other_providers_never_rerouted(self, provider) -> None:
        """Non-OpenAI providers MUST NOT be rerouted even with the flag set —
        codex's app-server can only run OpenAI/Codex auth flows."""
        got = _maybe_apply_codex_app_server_runtime(
            provider=provider,
            api_mode="anthropic_messages",
            model_cfg={"openai_runtime": "codex_app_server"},
        )
        assert got == "anthropic_messages", (
            f"provider={provider!r} should not be rerouted to codex_app_server"
        )


class TestCodexAppServerModule:
    """Module-surface tests for the JSON-RPC speaker. Don't require codex CLI."""

    @staticmethod
    def _capture_spawn_cmd(monkeypatch, **client_kwargs):
        import subprocess
        from agent.transports import codex_app_server as cas

        captured = {}

        class FakePopen:
            def __init__(self, cmd, *args, **kwargs):
                captured["cmd"] = list(cmd)
                self.stdin = None
                self.stdout = None
                self.stderr = None
                self.pid = 1
                self.returncode = None

            def poll(self):
                return None

            def wait(self, timeout=None):
                return 0

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        client = cas.CodexAppServerClient(**client_kwargs)
        client._closed = True
        return captured["cmd"]

    def test_default_binary_resolves_user_local_install_for_minimal_daemon_path(
        self, monkeypatch, tmp_path
    ) -> None:
        codex = tmp_path / "spawn-home" / ".local" / "bin" / "codex"
        codex.parent.mkdir(parents=True)
        codex.write_text("#!/bin/sh\n")
        codex.chmod(0o755)

        cmd = self._capture_spawn_cmd(
            monkeypatch,
            env={
                "HERMES_REAL_HOME": str(tmp_path / "spawn-home"),
                "HOME": str(tmp_path / "spawn-home"),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            },
        )

        assert cmd[:2] == [str(codex), "app-server"]

    def test_explicit_binary_and_path_resolved_default_are_unchanged(
        self, monkeypatch, tmp_path
    ) -> None:
        local_codex = tmp_path / "home" / ".local" / "bin" / "codex"
        local_codex.parent.mkdir(parents=True)
        local_codex.write_text("#!/bin/sh\n")
        local_codex.chmod(0o755)
        path_codex = tmp_path / "path" / "codex"
        path_codex.parent.mkdir()
        path_codex.write_text("#!/bin/sh\n")
        path_codex.chmod(0o755)
        env = {"HOME": str(tmp_path / "home"), "PATH": str(path_codex.parent)}

        assert self._capture_spawn_cmd(
            monkeypatch, codex_bin="/custom/codex", env=env
        )[:2] == ["/custom/codex", "app-server"]
        assert self._capture_spawn_cmd(monkeypatch, env=env)[:2] == [
            "codex",
            "app-server",
        ]

    @pytest.mark.parametrize("executable", [False, None])
    def test_default_binary_falls_back_when_user_local_candidate_is_unusable(
        self, monkeypatch, tmp_path, executable
    ) -> None:
        home = tmp_path / "home"
        candidate = home / ".local" / "bin" / "codex"
        if executable is not None:
            candidate.parent.mkdir(parents=True)
            candidate.write_text("#!/bin/sh\n")
            candidate.chmod(0o644)

        cmd = self._capture_spawn_cmd(
            monkeypatch,
            env={
                "HERMES_REAL_HOME": str(home),
                "HOME": str(home),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            },
        )

        assert cmd[:2] == ["codex", "app-server"]

    @staticmethod
    def _capture_binary_check(monkeypatch, *, codex_bin="codex"):
        import subprocess
        from agent.transports import codex_app_server as cas

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            captured["env"] = kwargs.get("env", {}).copy()
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout="codex-cli 0.125.0\n", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = cas.check_codex_binary(codex_bin=codex_bin)
        return captured, result

    def test_check_binary_resolves_user_local_install_with_sanitized_env(
        self, monkeypatch, tmp_path
    ) -> None:
        codex = tmp_path / ".local" / "bin" / "codex"
        codex.parent.mkdir(parents=True)
        codex.write_text("#!/bin/sh\n")
        codex.chmod(0o755)
        daemon_path = str(tmp_path / "daemon-bin")
        (tmp_path / "daemon-bin").mkdir()
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("PATH", daemon_path)
        monkeypatch.setenv("GH_TOKEN", "must-not-reach-preflight")
        monkeypatch.setenv("OPENAI_API_KEY", "not-needed-for-version")

        captured, result = self._capture_binary_check(monkeypatch)

        assert result == (True, "0.125.0")
        assert captured["cmd"] == [str(codex), "--version"]
        assert captured["env"]["HOME"] == str(tmp_path)
        assert captured["env"]["PATH"] == daemon_path
        assert "GH_TOKEN" not in captured["env"]
        assert "OPENAI_API_KEY" not in captured["env"]

    def test_check_binary_preserves_explicit_binary_and_path_hit(
        self, monkeypatch, tmp_path
    ) -> None:
        local_codex = tmp_path / "home" / ".local" / "bin" / "codex"
        local_codex.parent.mkdir(parents=True)
        local_codex.write_text("#!/bin/sh\n")
        local_codex.chmod(0o755)
        path_codex = tmp_path / "path" / "codex"
        path_codex.parent.mkdir()
        path_codex.write_text("#!/bin/sh\n")
        path_codex.chmod(0o755)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("PATH", str(path_codex.parent))

        explicit, _ = self._capture_binary_check(
            monkeypatch, codex_bin="/custom/codex"
        )
        path_hit, _ = self._capture_binary_check(monkeypatch)

        assert explicit["cmd"] == ["/custom/codex", "--version"]
        assert path_hit["cmd"] == ["codex", "--version"]

    @pytest.mark.parametrize("create_candidate", [True, False])
    def test_check_binary_falls_back_for_unusable_user_local_candidate(
        self, monkeypatch, tmp_path, create_candidate
    ) -> None:
        candidate = tmp_path / ".local" / "bin" / "codex"
        if create_candidate:
            candidate.parent.mkdir(parents=True)
            candidate.write_text("#!/bin/sh\n")
            candidate.chmod(0o644)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("PATH", str(tmp_path / "daemon-bin"))

        captured, result = self._capture_binary_check(monkeypatch)

        assert result == (True, "0.125.0")
        assert captured["cmd"] == ["codex", "--version"]

    def test_resolver_uses_only_spawn_env_home_trust_order(self, tmp_path) -> None:
        from agent.transports.codex_app_server import _resolve_codex_bin

        homes = {
            "real": tmp_path / "real-home",
            "home": tmp_path / "home",
            "profile": tmp_path / "user-profile",
            "drive": tmp_path / "drive-home",
        }
        for home in homes.values():
            codex = home / ".local" / "bin" / "codex"
            codex.parent.mkdir(parents=True)
            codex.write_text("#!/bin/sh\n")
            codex.chmod(0o755)

        base_env = {"PATH": str(tmp_path / "empty-path")}
        cases = [
            (
                {
                    "HERMES_REAL_HOME": str(homes["real"]),
                    "HOME": str(homes["home"]),
                    "USERPROFILE": str(homes["profile"]),
                    "HOMEDRIVE": str(tmp_path),
                    "HOMEPATH": "/drive-home",
                },
                homes["real"],
            ),
            (
                {
                    "HOME": str(homes["home"]),
                    "USERPROFILE": str(homes["profile"]),
                    "HOMEDRIVE": str(tmp_path),
                    "HOMEPATH": "/drive-home",
                },
                homes["home"],
            ),
            (
                {
                    "USERPROFILE": str(homes["profile"]),
                    "HOMEDRIVE": str(tmp_path),
                    "HOMEPATH": "/drive-home",
                },
                homes["profile"],
            ),
            (
                {"HOMEDRIVE": str(tmp_path), "HOMEPATH": "/drive-home"},
                homes["drive"],
            ),
        ]

        for home_env, expected_home in cases:
            resolved = _resolve_codex_bin("codex", env={**base_env, **home_env})
            assert resolved == str(expected_home / ".local" / "bin" / "codex")

    def test_resolver_does_not_consult_ambient_home(
        self, monkeypatch, tmp_path
    ) -> None:
        from agent.transports.codex_app_server import _resolve_codex_bin

        ambient_codex = tmp_path / ".local" / "bin" / "codex"
        ambient_codex.parent.mkdir(parents=True)
        ambient_codex.write_text("#!/bin/sh\n")
        ambient_codex.chmod(0o755)
        monkeypatch.setenv("HOME", str(tmp_path))

        assert _resolve_codex_bin(
            "codex", env={"PATH": str(tmp_path / "empty-path")}
        ) == "codex"

    def test_resolver_supports_userprofile_and_pathext_real_file(
        self, tmp_path
    ) -> None:
        from agent.transports.codex_app_server import _resolve_codex_bin

        profile = tmp_path / "windows-profile"
        launcher = profile / ".local" / "bin" / "codex.CMD"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("@echo off\n")
        launcher.chmod(0o755)

        resolved = _resolve_codex_bin(
            "codex",
            env={
                "PATH": str(tmp_path / "empty-path"),
                "USERPROFILE": str(profile),
                "PATHEXT": ".EXE;.CMD;.BAT",
            },
        )

        assert resolved == str(launcher)

    def test_resolver_allows_executable_symlink_in_user_local_bin(
        self, tmp_path
    ) -> None:
        from agent.transports.codex_app_server import _resolve_codex_bin

        target = tmp_path / "installed" / "codex"
        target.parent.mkdir()
        target.write_text("#!/bin/sh\n")
        target.chmod(0o755)
        link = tmp_path / "home" / ".local" / "bin" / "codex"
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(target)
        except OSError as exc:
            pytest.skip(f"symlinks unavailable: {exc}")

        resolved = _resolve_codex_bin(
            "codex",
            env={
                "HOME": str(tmp_path / "home"),
                "PATH": str(tmp_path / "empty-path"),
            },
        )

        assert resolved == str(link)

    def test_preflight_and_client_spawn_same_user_local_shim(
        self, monkeypatch, tmp_path
    ) -> None:
        import os
        import time
        from agent.transports import codex_app_server as cas

        real_home = tmp_path / "real-home"
        shim_name = "codex.CMD" if os.name == "nt" else "codex"
        shim = real_home / ".local" / "bin" / shim_name
        marker = tmp_path / "app-server-invoked"
        empty_path = tmp_path / "empty-path"
        shim.parent.mkdir(parents=True)
        empty_path.mkdir()
        if os.name == "nt":
            shim.write_text(
                "@echo off\r\n"
                'if "%~1"=="--version" (\r\n'
                "  echo codex-cli 0.125.0\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                'if "%~1"=="app-server" (\r\n'
                '  >"%CODEX_SHIM_MARKER%" echo %~f0\r\n'
                "  set /p _line=\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "exit /b 2\r\n"
            )
        else:
            shim.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"--version\" ]; then\n"
                "  printf '%s\\n' 'codex-cli 0.125.0'\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"$1\" = \"app-server\" ]; then\n"
                "  printf '%s\\n' \"$0\" > \"$CODEX_SHIM_MARKER\"\n"
                "  while IFS= read -r _line; do :; done\n"
                "  exit 0\n"
                "fi\n"
                "exit 2\n"
            )
        shim.chmod(0o755)
        monkeypatch.setenv("HERMES_REAL_HOME", str(real_home))
        monkeypatch.setenv("HOME", str(tmp_path / "decoy-home"))
        monkeypatch.setenv("PATH", str(empty_path))
        monkeypatch.setenv("PATHEXT", ".EXE;.CMD;.BAT")

        assert cas.check_codex_binary() == (True, "0.125.0")

        client = cas.CodexAppServerClient(
            env={
                "HERMES_REAL_HOME": str(real_home),
                "HOME": str(tmp_path / "decoy-home"),
                "PATH": str(empty_path),
                "PATHEXT": ".EXE;.CMD;.BAT",
                "CODEX_SHIM_MARKER": str(marker),
            }
        )
        try:
            deadline = time.monotonic() + 2.0
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            invoked = os.path.normcase(os.path.normpath(marker.read_text().strip()))
            expected = os.path.normcase(os.path.normpath(str(shim)))
            assert invoked == expected
        finally:
            client.close(timeout=2.0)

        assert client._proc.poll() is not None




    def test_check_binary_handles_missing_executable(self) -> None:
        from agent.transports.codex_app_server import check_codex_binary

        ok, msg = check_codex_binary(codex_bin="/nonexistent/codex/binary/path")
        assert ok is False
        assert "not found" in msg.lower() or "no such" in msg.lower()

    def test_codex_error_class_is_runtimeerror(self) -> None:
        from agent.transports.codex_app_server import CodexAppServerError

        err = CodexAppServerError(code=-32600, message="boom")
        assert isinstance(err, RuntimeError)
        assert "boom" in str(err)
        assert "-32600" in str(err)


class TestSpawnEnvIsolation:
    """The codex spawn must NOT rewrite HOME — codex's shell tool spawns
    subprocesses (gh, git, npm, aws, gcloud, ...) that need to find their
    config in the real user $HOME. CODEX_HOME isolates codex's own state,
    HOME stays unchanged.

    OpenClaw hit this footgun (openclaw/openclaw#81562) — they were
    rewriting HOME to a synthetic per-agent dir alongside CODEX_HOME,
    and then `gh auth status` / git config / etc. all broke inside codex
    shell calls. We avoid the same bug by only overlaying CODEX_HOME and
    RUST_LOG on top of os.environ.copy().
    """

    def test_spawn_env_preserves_HOME(self, monkeypatch):
        """The spawn env must contain the parent process's HOME unchanged.
        Verifies via a subprocess-monkey-patch."""
        import subprocess
        from agent.transports import codex_app_server as cas

        captured = {}

        class FakePopen:
            def __init__(self, cmd, *args, **kwargs):
                captured["env"] = kwargs.get("env", {}).copy()
                # Provide minimal Popen surface so __init__ doesn't crash
                # on attribute access during construction.
                self.stdin = None
                self.stdout = None
                self.stderr = None
                self.pid = 1
                self.returncode = None

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        monkeypatch.setenv("HOME", "/users/alice")

        client = cas.CodexAppServerClient(codex_bin="codex")
        client._closed = True  # so close() is a no-op

        # The spawn env must have HOME=/users/alice unchanged
        assert captured["env"].get("HOME") == "/users/alice", (
            f"HOME got rewritten in codex spawn env: "
            f"{captured['env'].get('HOME')!r}. Codex's shell tool's "
            "subprocesses (gh, git, aws, npm) need the user's real HOME."
        )

    def test_spawn_env_sets_CODEX_HOME_when_provided(self, monkeypatch):
        """CODEX_HOME isolation must still work — that's the whole point
        of the codex_home arg."""
        import subprocess
        from agent.transports import codex_app_server as cas

        captured = {}

        class FakePopen:
            def __init__(self, cmd, *args, **kwargs):
                captured["env"] = kwargs.get("env", {}).copy()
                self.stdin = None
                self.stdout = None
                self.stderr = None
                self.pid = 1
                self.returncode = None

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        monkeypatch.setenv("HOME", "/users/alice")

        client = cas.CodexAppServerClient(
            codex_bin="codex", codex_home="/tmp/profile/codex"
        )
        client._closed = True

        assert captured["env"].get("CODEX_HOME") == "/tmp/profile/codex"
        # And HOME still passes through unchanged
        assert captured["env"].get("HOME") == "/users/alice"

    def test_kanban_worker_adds_only_kanban_writable_root(self, monkeypatch):
        """Codex-runtime Kanban workers need to write board state outside
        their scratch/worktree workspace, but should not fall back to
        danger-full-access. Hermes passes a narrow app-server config override
        for the Kanban root only.
        """
        import subprocess
        from agent.transports import codex_app_server as cas

        captured = {}

        class FakePopen:
            def __init__(self, cmd, *args, **kwargs):
                captured["cmd"] = list(cmd)
                captured["env"] = kwargs.get("env", {}).copy()
                self.stdin = None
                self.stdout = None
                self.stderr = None
                self.pid = 1
                self.returncode = None

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        monkeypatch.setenv("HOME", "/users/alice")
        monkeypatch.setenv("HERMES_HOME", "/users/alice/.hermes/profiles/backend-worker")
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_smoke")
        monkeypatch.setenv(
            "HERMES_KANBAN_DB",
            "/users/alice/.hermes/kanban/boards/smoke/kanban.db",
        )

        client = cas.CodexAppServerClient(codex_bin="codex")
        client._closed = True

        cmd = captured["cmd"]
        assert cmd[:2] == ["codex", "app-server"]
        assert 'sandbox_mode="workspace-write"' in cmd
        assert (
            'sandbox_workspace_write.writable_roots=["/users/alice/.hermes/kanban/boards/smoke"]'
            in cmd
        )
        assert "sandbox_workspace_write.network_access=false" in cmd
        assert all("danger" not in part for part in cmd)


class TestSpawnEnvSecretStripping:
    """codex app-server routes its spawn env through hermes_subprocess_env(
    inherit_credentials=True) instead of a raw os.environ.copy().

    codex is a model-driving CLI executor: it legitimately needs LLM provider
    credentials to authenticate, but it must NOT inherit Tier-1 Hermes secrets
    (gateway bot tokens, GitHub/infra auth, dashboard session token) or the
    dynamic-internal secrets (AUXILIARY_*_API_KEY / _BASE_URL side-LLM keys,
    GATEWAY_RELAY_* relay-auth) — a coding subprocess has no use for those and
    a model-controlled action could exfiltrate them. This closes the #29157
    sibling spawn-site gap (copilot_acp_client already routes through the
    helper; codex app-server predated it).
    """

    @staticmethod
    def _capture_spawn_env(monkeypatch):
        import subprocess
        from agent.transports import codex_app_server as cas

        captured = {}

        class FakePopen:
            def __init__(self, cmd, *args, **kwargs):
                captured["env"] = kwargs.get("env", {}).copy()
                self.stdin = None
                self.stdout = None
                self.stderr = None
                self.pid = 1
                self.returncode = None

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        client = cas.CodexAppServerClient(codex_bin="codex")
        client._closed = True
        return captured["env"]

    def test_tier1_and_internal_secrets_stripped_from_spawn_env(self, monkeypatch):
        for var, val in {
            "GH_TOKEN": "ghp-secret",
            "TELEGRAM_BOT_TOKEN": "bot-secret",
            "MODAL_TOKEN_SECRET": "modal-secret",
            "HERMES_DASHBOARD_SESSION_TOKEN": "dash-secret",
            "AUXILIARY_VISION_API_KEY": "aux-secret",
            "GATEWAY_RELAY_SECRET": "relay-secret",
            "GATEWAY_RELAY_ID": "relay-id",
            "GATEWAY_RELAY_DELIVERY_KEY": "relay-delivery",
        }.items():
            monkeypatch.setenv(var, val)

        env = self._capture_spawn_env(monkeypatch)
        for var in (
            "GH_TOKEN", "TELEGRAM_BOT_TOKEN", "MODAL_TOKEN_SECRET",
            "HERMES_DASHBOARD_SESSION_TOKEN", "AUXILIARY_VISION_API_KEY",
            "GATEWAY_RELAY_SECRET", "GATEWAY_RELAY_ID", "GATEWAY_RELAY_DELIVERY_KEY",
        ):
            assert var not in env, f"{var} leaked into codex app-server spawn env"

    def test_provider_credentials_still_reach_codex(self, monkeypatch):
        """codex authenticates against the model endpoint — provider keys must
        still flow through (inherit_credentials=True)."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-codex-needs-this")
        env = self._capture_spawn_env(monkeypatch)
        assert env.get("OPENAI_API_KEY") == "sk-codex-needs-this"
