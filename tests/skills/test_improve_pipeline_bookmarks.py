"""Regression tests for the current xurl bookmarks CLI contract."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "optional-skills/research/darwinian-evolver/labs/scripts"
sys.path.insert(0, str(SCRIPTS))

import harvest_x_bookmarks as hx  # noqa: E402
from bounded_subprocess import OutputLimitExceeded, run_text_bounded  # noqa: E402


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

    monkeypatch.setattr(hx, "run_text_bounded", fake_run)

    assert hx.fetch_bookmarks(7) == [{"id": "1", "full_text": "Hermes", "url": "x"}]
    assert calls[0][0] == ["xurl", "bookmarks", "-n", "7"]
    assert calls[0][1]["max_stdout_bytes"] == hx._MAX_BOOKMARK_STDOUT_BYTES


def test_xurl_never_returns_more_bookmarks_than_requested(monkeypatch) -> None:
    records = [
        {"id": str(index), "full_text": f"bookmark {index}", "url": f"https://x.com/u/status/{index}"}
        for index in range(3)
    ]
    monkeypatch.setattr(
        hx,
        "run_text_bounded",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"bookmarks": records}), stderr=""
        ),
    )

    assert hx.fetch_bookmarks(1) == records[:1]


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
    monkeypatch.setattr(hx, "run_text_bounded", fake_run)

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
    monkeypatch.setattr(hx, "run_text_bounded", lambda *args, **kwargs: calls.append((args, kwargs)))

    with pytest.raises(RuntimeError, match="loopback"):
        hx._fetch_browser_bookmarks(1)
    assert calls == []


def test_browser_fallback_rejects_malformed_loopback_port(monkeypatch) -> None:
    calls = []
    monkeypatch.setenv("X_BOOKMARKS_CDP_URL", "http://127.0.0.1:99999")
    monkeypatch.setattr(hx, "run_text_bounded", lambda *args, **kwargs: calls.append((args, kwargs)))

    with pytest.raises(RuntimeError, match="loopback"):
        hx._fetch_browser_bookmarks(1)
    assert calls == []


def test_bounded_subprocess_stops_oversized_stdout() -> None:
    with pytest.raises(OutputLimitExceeded, match="stdout exceeded"):
        run_text_bounded(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 4097)"],
            timeout=5,
            max_stdout_bytes=4096,
        )


@pytest.mark.live_system_guard_bypass
def test_bounded_subprocess_kills_descendant_holding_stdout_open(tmp_path: Path) -> None:
    if not Path("/proc").is_dir():
        pytest.skip("requires procfs process verification")
    pid_file = tmp_path / "descendant.pid"
    descendant = (
        "import os,time; "
        "open(os.environ['PID_FILE'],'w').write(str(os.getpid())); "
        "time.sleep(30)"
    )
    parent = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable, '-c', {descendant!r}], stdout=sys.stdout)"
    )
    env = dict(os.environ, PID_FILE=str(pid_file))
    descendant_pid = None
    try:
        completed = run_text_bounded(
            [sys.executable, "-c", parent],
            timeout=5,
            max_stdout_bytes=4096,
            env=env,
        )
        assert completed.returncode == 0
        descendant_pid = int(pid_file.read_text())
        deadline = time.monotonic() + 2
        while Path(f"/proc/{descendant_pid}").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not Path(f"/proc/{descendant_pid}").exists()
    finally:
        if descendant_pid is None and pid_file.is_file():
            descendant_pid = int(pid_file.read_text())
        if descendant_pid is not None and Path(f"/proc/{descendant_pid}").exists():
            os.kill(descendant_pid, 9)


def test_browser_fallback_rejects_oversized_record(monkeypatch) -> None:
    oversized = [{
        "id": "22",
        "full_text": "x" * (hx._MAX_BOOKMARK_TEXT_CHARS + 1),
        "url": "https://x.com/u/status/22",
    }]
    monkeypatch.setattr(
        hx,
        "run_text_bounded",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=json.dumps(oversized), stderr=""
        ),
    )

    with pytest.raises(RuntimeError, match="invalid bookmark record"):
        hx._fetch_browser_bookmarks(1)


def test_browser_script_bounds_dom_records_before_serialization(tmp_path: Path, monkeypatch) -> None:
    module = tmp_path / "node_modules/playwright-core"
    module.mkdir(parents=True)
    module.joinpath("index.js").write_text(
        """
const ownerDocument = {
  defaultView: { NodeFilter: { SHOW_TEXT: 4 } },
  createTreeWalker: (root) => {
    let index = 0;
    return { nextNode: () => root._textNodes[index++] || null };
  },
};
function article(index) {
  const statusId = `${index}`.repeat(200);
  const tweet = { ownerDocument, _textNodes: [{ nodeValue: "x".repeat(10000) }] };
  return {
    ownerDocument,
    _textNodes: tweet._textNodes,
    querySelectorAll: () => [{ getAttribute: () => `/user/status/${statusId}` }],
    querySelector: () => tweet,
  };
}
const page = {
  goto: async () => {},
  locator: () => ({
    first: () => ({ waitFor: async () => {} }),
    evaluateAll: async (callback, limits) => {
      const isolatedCallback = require("node:vm").runInNewContext(
        `(${callback.toString()})`, { URL }
      );
      return isolatedCallback(
        [article(1), article(2), article(3), article(4), article(5)], limits
      );
    },
  }),
  evaluate: async () => {},
  waitForTimeout: async () => {},
  close: async () => {},
};
exports.chromium = {
  connectOverCDP: async () => ({ contexts: () => [{ newPage: async () => page }] }),
};
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("X_PUBLISHER_ROOT", str(tmp_path))

    rows = hx._fetch_browser_bookmarks(2)

    assert len(rows) == 2
    assert all(len(row["id"]) <= hx._MAX_BOOKMARK_ID_CHARS for row in rows)
    assert all(len(row["full_text"]) <= hx._MAX_BOOKMARK_TEXT_CHARS for row in rows)
    assert "innerText" not in hx._BROWSER_SCRIPT


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

    monkeypatch.setattr(hx, "run_text_bounded", fake_run)

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
        hx,
        "run_text_bounded",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=""),
    )

    with pytest.raises(RuntimeError):
        hx.fetch_bookmarks()