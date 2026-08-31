#!/usr/bin/env python3
"""Run a subprocess while bounding captured stdout before it reaches memory."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Mapping, Sequence


class OutputLimitExceeded(RuntimeError):
    """Raised after terminating a child whose stdout crossed the configured cap."""


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        if process.poll() is not None:
            return
        try:
            process.kill()
        except OSError:
            pass


def run_text_bounded(
    argv: Sequence[str],
    *,
    timeout: float,
    max_stdout_bytes: int,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Capture UTF-8 stdout up to a hard byte limit, discarding stderr.

    The reader stores at most ``max_stdout_bytes``. Crossing the limit or timeout
    kills the child's process group so descendants cannot keep the pipe alive.
    """
    if max_stdout_bytes < 1:
        raise ValueError("max_stdout_bytes must be positive")

    process = subprocess.Popen(
        list(argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=dict(env) if env is not None else None,
        start_new_session=True,
    )
    if process.stdout is None:  # pragma: no cover - guaranteed by PIPE
        _kill_process_group(process)
        raise RuntimeError("bounded subprocess stdout pipe unavailable")

    output = bytearray()
    exceeded = threading.Event()
    read_errors: list[BaseException] = []

    def _drain() -> None:
        try:
            while True:
                chunk = process.stdout.read(65_536)
                if not chunk:
                    return
                remaining = max_stdout_bytes - len(output)
                if len(chunk) > remaining:
                    if remaining > 0:
                        output.extend(chunk[:remaining])
                    exceeded.set()
                    _kill_process_group(process)
                    return
                output.extend(chunk)
        except BaseException as exc:  # noqa: BLE001 - surfaced after process cleanup
            read_errors.append(exc)
            _kill_process_group(process)

    reader = threading.Thread(target=_drain, name="bounded-stdout-reader", daemon=True)
    reader.start()
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        process.wait()
        reader.join(timeout=1)
        raise
    finally:
        if process.poll() is not None:
            reader.join(timeout=1)

    if reader.is_alive():
        _kill_process_group(process)
        reader.join(timeout=1)
        if reader.is_alive():
            raise RuntimeError("bounded subprocess stdout did not close")
    if read_errors:
        raise RuntimeError("bounded subprocess stdout read failed") from read_errors[0]
    if exceeded.is_set():
        raise OutputLimitExceeded(
            f"subprocess stdout exceeded {max_stdout_bytes} bytes"
        )

    return subprocess.CompletedProcess(
        list(argv),
        returncode,
        stdout=bytes(output).decode("utf-8", errors="replace"),
        stderr="",
    )
