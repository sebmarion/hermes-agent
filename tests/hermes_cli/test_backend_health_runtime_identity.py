import asyncio

from hermes_cli import web_server


def test_health_reports_the_boot_runtime_identity(monkeypatch):
    identity = {
        "code_fingerprint": "git:main:" + "a" * 40,
        "code_sha": "a" * 40,
    }
    monkeypatch.setattr(web_server.app.state, "runtime_identity", identity, raising=False)

    payload = asyncio.run(web_server.get_health())

    assert payload["runtime_identity"] == identity


def test_runtime_identity_includes_a_boot_source_digest(monkeypatch):
    code_sha = "a" * 40
    source_digest = "b" * 64
    monkeypatch.setattr(
        web_server,
        "get_boot_fingerprint",
        lambda: "git:main:" + code_sha,
    )
    monkeypatch.setattr(web_server, "get_boot_source_digest", lambda: source_digest)

    assert web_server._runtime_identity_from_boot_fingerprint() == {
        "code_fingerprint": "git:main:" + code_sha,
        "code_sha": code_sha,
        "source_digest": source_digest,
    }


def test_runtime_identity_uses_cached_boot_digest_not_live_disk_digest(monkeypatch):
    code_sha = "a" * 40
    boot_digest = "b" * 64
    monkeypatch.setattr(web_server, "get_boot_fingerprint", lambda: "git:main:" + code_sha)
    monkeypatch.setattr(web_server, "get_boot_source_digest", lambda: boot_digest)
    monkeypatch.setattr(
        web_server,
        "runtime_source_digest",
        lambda _root: (_ for _ in ()).throw(AssertionError("health must not rehash mutable disk")),
    )

    assert web_server._runtime_identity_from_boot_fingerprint()["source_digest"] == boot_digest
