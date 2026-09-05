"""Real gateway status must report a stable boot-generation source digest."""
import json
from gateway import status


def test_runtime_status_publishes_actual_boot_source_digest(tmp_path, monkeypatch):
    from gateway.code_skew import runtime_source_digest
    from pathlib import Path
    destination = tmp_path / "gateway_state.json"
    status._gateway_boot_source_digest.cache_clear()
    monkeypatch.setattr(status, "_get_runtime_status_path", lambda: destination)
    status.write_runtime_status(gateway_state="starting", active_agents=0)
    first = json.loads(destination.read_text())
    expected = runtime_source_digest(Path(status.__file__).resolve().parents[1])
    assert first["source_digest"] == expected
    assert len(first["code_sha"]) == 40
    status.write_runtime_status(gateway_state="running", active_agents=1)
    second = json.loads(destination.read_text())
    assert second["source_digest"] == first["source_digest"]
    assert second["code_sha"] == first["code_sha"]
    status._gateway_boot_source_digest.cache_clear()


def test_status_digest_stays_bound_to_boot_when_source_files_change(monkeypatch):
    import gateway.code_skew as skew
    status._gateway_boot_source_digest.cache_clear()
    monkeypatch.setattr(skew, "runtime_source_digest", lambda *_a: "a" * 64)
    first = status._get_code_identity_fields()
    monkeypatch.setattr(skew, "runtime_source_digest", lambda *_a: "b" * 64)
    assert status._get_code_identity_fields()["source_digest"] == first["source_digest"] == "a" * 64
    status._gateway_boot_source_digest.cache_clear()
