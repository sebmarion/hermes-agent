"""Regression tests for the current xurl bookmarks CLI contract."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "optional-skills/research/darwinian-evolver/labs/scripts"
sys.path.insert(0, str(SCRIPTS))

import harvest_x_bookmarks as hx  # noqa: E402


def test_fetch_bookmarks_uses_current_xurl_cli_without_obsolete_json_flag(monkeypatch) -> None:
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps({"bookmarks": [{"id": "1", "full_text": "Hermes", "url": "x"}]}),
            stderr="",
        )

    monkeypatch.setattr(hx.subprocess, "run", fake_run)

    assert hx.fetch_bookmarks(7) == [{"id": "1", "full_text": "Hermes", "url": "x"}]
    assert calls[0][0] == ["xurl", "bookmarks", "-n", "7"]
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["text"] is True


def test_fetch_bookmarks_falls_back_to_read_only_browser_session(monkeypatch) -> None:
    calls = []
    browser_records = [
        {"id": "22", "full_text": "Hermes browser fallback", "url": "https://x.com/u/status/22"},
    ]

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[0] == "xurl":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unauthorized")
        assert argv[0] == "node"
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(browser_records), stderr="")

    monkeypatch.setenv("X_BOOKMARKS_CDP_URL", "http://127.0.0.1:9444")
    monkeypatch.setenv("X_PUBLISHER_ROOT", "/tmp/hermes-x-publisher")
    monkeypatch.setattr(hx.subprocess, "run", fake_run)

    assert hx.fetch_bookmarks(7) == browser_records
    assert calls[0][0] == ["xurl", "bookmarks", "-n", "7"]
    assert calls[1][0][:2] == ["node", "-e"]
    assert calls[1][1]["env"]["X_BOOKMARKS_CDP_URL"] == "http://127.0.0.1:9444"
    assert calls[1][1]["env"]["X_PUBLISHER_ROOT"] == "/tmp/hermes-x-publisher"


def test_browser_fallback_process_exits_after_read(tmp_path: Path, monkeypatch) -> None:
    module = tmp_path / "node_modules/playwright-core"
    module.mkdir(parents=True)
    module.joinpath("index.js").write_text(
        """
const keepAlive = setInterval(() => {}, 1000);
const page = {
  goto: async () => {},
  locator: () => ({
    first: () => ({ waitFor: async () => {} }),
    evaluateAll: async () => [{
      id: "22",
      full_text: "Hermes browser fallback",
      url: "https://x.com/u/status/22",
    }],
  }),
  evaluate: async () => {},
  waitForTimeout: async () => {},
  close: async () => {},
};
exports.chromium = {
  connectOverCDP: async () => ({
    contexts: () => [{ newPage: async () => page }],
  }),
};
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("X_PUBLISHER_ROOT", str(tmp_path))
    monkeypatch.setattr(hx, "_BROWSER_TIMEOUT_SECONDS", 0.25)

    assert hx._fetch_browser_bookmarks(1) == [
        {
            "id": "22",
            "full_text": "Hermes browser fallback",
            "url": "https://x.com/u/status/22",
        }
    ]


def test_browser_fallback_rejects_non_loopback_cdp(monkeypatch) -> None:
    calls = []
    monkeypatch.setenv("X_BOOKMARKS_CDP_URL", "http://example.com:9333")
    monkeypatch.setattr(hx.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    with pytest.raises(RuntimeError, match="loopback"):
        hx._fetch_browser_bookmarks(1)
    assert calls == []


def test_browser_fallback_rejects_non_strict_records(monkeypatch) -> None:
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "xurl":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unauthorized")
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps([{
                "id": "22",
                "full_text": "Hermes browser fallback",
                "url": "https://x.com/u/status/22",
                "unexpected": "must not escape parser",
            }]),
            stderr="",
        )

    monkeypatch.setattr(hx.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="browser session fallback failed") as failure:
        hx.fetch_bookmarks()
    assert "unexpected" not in str(failure.value)
    assert calls[0] == ["xurl", "bookmarks", "-n", "50"]
    assert calls[1][:2] == ["node", "-e"]


@pytest.mark.parametrize(
    "stdout",
    [
        "not-json",
        json.dumps({"bookmarks": {}}),
        json.dumps("wrong shape"),
        json.dumps(["not-a-bookmark"]),
    ],
)
def test_fetch_bookmarks_rejects_invalid_json_or_response_shape(monkeypatch, stdout: str) -> None:
    monkeypatch.setattr(
        hx.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=""),
    )

    with pytest.raises(RuntimeError):
        hx.fetch_bookmarks()