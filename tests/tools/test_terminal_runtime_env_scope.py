"""Terminal configuration must honor the task-local runtime env boundary."""

from agent import runtime_env
from tools import terminal_tool


def test_terminal_config_uses_authoritative_runtime_scope(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "process.invalid")
    monkeypatch.setenv("TERMINAL_SSH_USER", "process-user")
    monkeypatch.setattr(terminal_tool, "_ensure_terminal_env_bridged", lambda: None)

    token = runtime_env.set_runtime_env(
        {
            "TERMINAL_ENV": "ssh",
            "TERMINAL_SSH_HOST": "profile.invalid",
            "TERMINAL_SSH_USER": "profile-user",
            "TERMINAL_CWD": "~",
        },
        authoritative=True,
    )
    try:
        config = terminal_tool._get_env_config()
    finally:
        runtime_env.reset_runtime_env(token)

    assert config["env_type"] == "ssh"
    assert config["ssh_host"] == "profile.invalid"
    assert config["ssh_user"] == "profile-user"
    assert config["cwd"] == "~"
    assert __import__("os").environ["TERMINAL_SSH_HOST"] == "process.invalid"
