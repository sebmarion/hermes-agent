"""Mandatory OpenCodeReview gate for BestPlan implementation turns.

This stays at the plugin edge.  It uses OCR delegation mode only: OCR selects
reviewable files and resolves rules; the existing Hermes review workflow owns
LLM review.  The gate is invoked by Hermes' pre_verify lifecycle hook after a
turn has landed file mutations.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any, Iterable


_RECEIPT_SCHEMA = "hermes.bestplan.ocr-receipt.v1"
_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
_MAX_REVIEWABLE_FILES = 2_000
_MAX_TRACKED_FILE_BYTES = 64 * 1024 * 1024
_MAX_PASS_CACHE_ENTRIES = 256
_PASS_CACHE_TTL_SECONDS = 6 * 60 * 60
_MAX_ACTIVE_SESSIONS = 256
_MAX_RECEIPT_BYTES = 8 * 1024 * 1024
_COMMAND_TIMEOUT_SECONDS = 90
_SESSION_TTL_SECONDS = 6 * 60 * 60
_BESTPLAN_MARKER_RE = re.compile(
    r"(?:bestplan|hermes_bestplan|hermes-bestplan|plan-then-implement)",
    re.IGNORECASE,
)

_CACHE_LOCK = RLock()
_RECEIPT_LOCK = RLock()
_ACTIVE_SESSIONS: dict[str, float] = {}
_PASS_CACHE: dict[tuple[str, str, str, str], dict[str, Any]] = {}


class OcrGateError(RuntimeError):
    """The mandatory OCR gate could not produce valid evidence."""


def mark_bestplan_session(session_id: str) -> None:
    """Remember that this session entered the BestPlan tool path."""
    value = str(session_id or "").strip()
    if not value:
        return
    with _CACHE_LOCK:
        now = time.time()
        _ACTIVE_SESSIONS[value] = now + _SESSION_TTL_SECONDS
        expired = [key for key, deadline in _ACTIVE_SESSIONS.items() if deadline < now]
        for key in expired:
            _ACTIVE_SESSIONS.pop(key, None)
        while len(_ACTIVE_SESSIONS) > _MAX_ACTIVE_SESSIONS:
            oldest = min(_ACTIVE_SESSIONS, key=_ACTIVE_SESSIONS.get)
            _ACTIVE_SESSIONS.pop(oldest, None)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _resolve_ocr_executable() -> tuple[Path, str]:
    executable_raw = shutil.which("ocr")
    if not executable_raw:
        raise OcrGateError("ocr is not on PATH")
    try:
        executable = Path(executable_raw).resolve(strict=True)
        if not stat.S_ISREG(executable.stat().st_mode):
            raise OcrGateError("ocr executable is not a regular file")
        return executable, _sha256(executable.read_bytes())
    except OSError as exc:
        raise OcrGateError("ocr executable is unavailable") from exc


def _safe_env(executable: Path) -> dict[str, str]:
    keep = {
        "HOME",
        "USERPROFILE",
        "PATH",
        "LANG",
        "LC_ALL",
        "SYSTEMROOT",
        "SystemRoot",
        "WINDIR",
        "TEMP",
        "TMP",
        "PATHEXT",
        "COMSPEC",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "NODE_PATH",
    }
    env = {key: value for key, value in os.environ.items() if key in keep}
    current_path = env.get("PATH", "")
    path_parts = [str(executable.parent)]
    if current_path:
        path_parts.append(current_path)
    if os.name != "nt":
        path_parts.extend(["/usr/bin", "/bin"])
    env["PATH"] = os.pathsep.join(dict.fromkeys(path_parts))
    if os.name != "nt":
        env.setdefault("HOME", str(Path.home()))
        env["LANG"] = "C"
        env["LC_ALL"] = "C"
    return env


def _run(
    argv: list[str],
    *,
    cwd: Path,
    executable: Path | None = None,
) -> tuple[int, bytes, bytes]:
    env = _safe_env(executable) if executable is not None else None
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise OcrGateError(f"command unavailable: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise OcrGateError(f"command timed out: {argv[0]}") from exc
    stdout = bytes(completed.stdout or b"")
    stderr = bytes(completed.stderr or b"")
    if len(stdout) + len(stderr) > _MAX_OUTPUT_BYTES:
        raise OcrGateError(f"command output exceeded {_MAX_OUTPUT_BYTES} bytes: {argv[0]}")
    return completed.returncode, stdout, stderr


def _path_candidates(raw_path: str) -> tuple[Path, ...]:
    candidate = Path(str(raw_path)).expanduser()
    if candidate.is_absolute():
        return (candidate,)
    roots = [Path.cwd()]
    hermes_home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()
    roots.extend((hermes_home / "skills", Path.home() / ".hermes" / "skills"))
    seen: set[str] = set()
    candidates = []
    for root in roots:
        value = root / candidate
        key = str(value)
        if key not in seen:
            seen.add(key)
            candidates.append(value)
    return tuple(candidates)


def _repo_for_path(raw_path: str) -> Path | None:
    for raw_candidate in _path_candidates(raw_path):
        try:
            resolved_candidate = raw_candidate.resolve(strict=False)
        except OSError:
            continue
        candidate = resolved_candidate if resolved_candidate.is_dir() else resolved_candidate.parent
        root = _git_root_for_directory(candidate)
        if root is not None:
            return root
    return None


def _git_root_for_directory(candidate: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.decode("utf-8", "replace").strip()
    if not value:
        return None
    try:
        root = Path(value).resolve(strict=True)
        return root if (root / ".git").exists() else root
    except (OSError, RuntimeError):
        return None


def _repo_roots(changed_paths: Iterable[str]) -> tuple[Path, ...]:
    roots: dict[str, Path] = {}
    for raw_path in changed_paths:
        root = _repo_for_path(str(raw_path))
        if root is not None:
            roots[str(root)] = root
    return tuple(roots[key] for key in sorted(roots))


def _git_identity(root: Path, changed_paths: list[str]) -> tuple[str, str]:
    code, head, stderr = _run(["git", "rev-parse", "HEAD"], cwd=root)
    if code != 0:
        raise OcrGateError(
            f"cannot resolve repository HEAD: {stderr.decode('utf-8', 'replace').strip()[:240]}"
        )
    code, diff, stderr = _run(
        [
            "git",
            "diff",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            "HEAD",
            "--",
        ],
        cwd=root,
    )
    if code != 0:
        raise OcrGateError(
            f"cannot capture repository diff: {stderr.decode('utf-8', 'replace').strip()[:240]}"
        )
    code, status, stderr = _run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "--"],
        cwd=root,
    )
    if code != 0:
        raise OcrGateError(
            f"cannot capture repository status: {stderr.decode('utf-8', 'replace').strip()[:240]}"
        )
    file_digests: list[dict[str, Any]] = []
    repository_root = root.resolve(strict=True)
    for raw_path in changed_paths:
        candidate = Path(str(raw_path)).expanduser()
        if not candidate.is_absolute():
            candidate = repository_root / candidate
        try:
            resolved = candidate.resolve(strict=False)
            relative = resolved.relative_to(repository_root).as_posix()
        except (OSError, RuntimeError, ValueError) as exc:
            raise OcrGateError("changed path is outside the selected repository") from exc
        if resolved.is_file():
            size = resolved.stat().st_size
            if size > _MAX_TRACKED_FILE_BYTES:
                raise OcrGateError("changed file exceeds the OCR identity size limit")
            file_digests.append(
                {"path": relative, "size": size, "sha256": _sha256(resolved.read_bytes())}
            )
        else:
            file_digests.append({"path": relative, "missing": True})
    payload = {
        "head": head.decode("ascii", "replace").strip(),
        "diff_sha256": _sha256(diff),
        "status": status.decode("utf-8", "replace"),
        "changed_files": file_digests,
    }
    return payload["head"], _sha256(_canonical_json(payload).encode("utf-8"))


def _json_output(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OcrGateError(f"OCR {label} output is not valid JSON") from exc
    if not isinstance(value, dict) or value.get("schema_version") != "1":
        raise OcrGateError(f"OCR {label} output has an unsupported schema")
    return value


def _reviewable_paths(preview: dict[str, Any]) -> tuple[str, ...]:
    values = preview.get("reviewable_files")
    if not isinstance(values, list) or len(values) > _MAX_REVIEWABLE_FILES:
        raise OcrGateError("OCR preview has an invalid reviewable_files list")
    paths: list[str] = []
    for item in values:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise OcrGateError("OCR preview contains an invalid file entry")
        raw_path = item["path"]
        path = raw_path.replace("\\", "/")
        pure = PurePosixPath(path)
        is_drive_absolute = bool(re.match(r"^[A-Za-z]:/", path))
        if path.startswith("/") or is_drive_absolute or any(
            part == ".." for part in pure.parts
        ):
            raise OcrGateError("OCR preview returned an unsafe path")
        if not path or path in paths:
            raise OcrGateError("OCR preview returned duplicate or empty paths")
        paths.append(path)
    return tuple(paths)


def _receipt_path() -> Path:
    home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    directory = home / "bestplan"
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_info = directory.lstat()
        if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode):
            raise OcrGateError("BestPlan receipt directory is not a safe directory")
        directory.chmod(0o700)
    except OcrGateError:
        raise
    except OSError as exc:
        raise OcrGateError("BestPlan receipt directory is unavailable") from exc
    path = directory / "ocr-receipts.jsonl"
    try:
        path_info = path.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise OcrGateError("BestPlan receipt file is unavailable") from exc
    else:
        if stat.S_ISLNK(path_info.st_mode) or not stat.S_ISREG(path_info.st_mode):
            raise OcrGateError("BestPlan receipt file is not safe")
        try:
            path.chmod(0o600)
        except OSError as exc:
            raise OcrGateError("BestPlan receipt file permissions are unavailable") from exc
    return path


def _append_receipt(receipt: dict[str, Any]) -> None:
    with _RECEIPT_LOCK:
        path = _receipt_path()
        line = (_canonical_json(receipt) + "\n").encode("utf-8")
        try:
            current_size = path.stat().st_size
            receipt_exists = True
        except FileNotFoundError:
            current_size = 0
            receipt_exists = False
        if receipt_exists and current_size + len(line) > _MAX_RECEIPT_BYTES:
            truncate_flags = os.O_WRONLY | os.O_TRUNC
            truncate_flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, truncate_flags)
            os.close(descriptor)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "ab", closefd=True) as handle:
                descriptor = -1
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        try:
            path.chmod(0o600)
        except OSError as exc:
            raise OcrGateError("BestPlan receipt file permissions are unavailable") from exc


def _failure_receipt(
    *,
    session_id: str,
    repo: str,
    changed_paths: list[str],
    reason: str,
) -> dict[str, Any]:
    return {
        "schema": _RECEIPT_SCHEMA,
        "status": "failed",
        "session_id": session_id,
        "repository": repo,
        "changed_paths": sorted(changed_paths),
        "reason": reason[:512],
        "created_at": time.time(),
    }


def _persist_receipt_or_failure(
    receipt: dict[str, Any],
    *,
    session_id: str,
    repo: str,
    changed_paths: list[str],
) -> dict[str, Any]:
    try:
        _append_receipt(receipt)
        return receipt
    except (OSError, OcrGateError) as exc:
        failure = _failure_receipt(
            session_id=session_id,
            repo=repo,
            changed_paths=changed_paths,
            reason=f"OCR receipt persistence failed: {type(exc).__name__}",
        )
        try:
            _append_receipt(failure)
        except (OSError, OcrGateError):
            pass
        return failure


def _verify_repo(
    *,
    session_id: str,
    root: Path,
    changed_paths: list[str],
    executable: Path,
    executable_sha256: str,
) -> dict[str, Any]:
    version_code, version_out, version_err = _run(
        [str(executable), "--version"], cwd=root, executable=executable
    )
    if version_code != 0 or not version_out.strip():
        detail = version_err.decode("utf-8", "replace").strip()[:240]
        raise OcrGateError(f"ocr --version failed{': ' + detail if detail else ''}")
    head, workspace_digest = _git_identity(root, changed_paths)
    preview_code, preview_raw, preview_err = _run(
        [
            str(executable),
            "delegate",
            "preview",
            "--format",
            "json",
            "--repo",
            str(root),
        ],
        cwd=root,
        executable=executable,
    )
    if preview_code != 0:
        detail = preview_err.decode("utf-8", "replace").strip()[:240]
        raise OcrGateError(f"ocr delegate preview failed{': ' + detail if detail else ''}")
    preview = _json_output(preview_raw, "preview")
    reviewable = _reviewable_paths(preview)
    if reviewable:
        rule_code, rules_raw, rules_err = _run(
            [
                str(executable),
                "delegate",
                "rule",
                "--format",
                "json",
                "--repo",
                str(root),
                *reviewable,
            ],
            cwd=root,
            executable=executable,
        )
        if rule_code != 0:
            detail = rules_err.decode("utf-8", "replace").strip()[:240]
            raise OcrGateError(f"ocr delegate rule failed{': ' + detail if detail else ''}")
        _json_output(rules_raw, "rule")
    else:
        rules_raw = _canonical_json({"schema_version": "1", "not_applicable": True}).encode(
            "utf-8"
        )
    preview_digest = _sha256(preview_raw)
    rules_digest = _sha256(rules_raw)
    executable_digest = executable_sha256
    receipt = {
        "schema": _RECEIPT_SCHEMA,
        "status": "passed",
        "session_id": session_id,
        "repository": str(root),
        "head": head,
        "workspace_digest": workspace_digest,
        "changed_paths": sorted(changed_paths),
        "reviewable_paths": list(reviewable),
        "reviewable_count": len(reviewable),
        "excluded_count": int(preview.get("excluded_count") or 0),
        "ocr": {
            "executable": str(executable),
            "executable_sha256": executable_digest,
            "version": version_out.decode("utf-8", "replace").strip()[:256],
            "preview_sha256": preview_digest,
            "rules_sha256": rules_digest,
        },
        "created_at": time.time(),
    }
    receipt["receipt_sha256"] = _sha256(_canonical_json(receipt).encode("utf-8"))
    return receipt


def verify_ocr_for_turn(
    *,
    session_id: str,
    changed_paths: Iterable[str],
) -> dict[str, Any]:
    """Run OCR once per current workspace contents and persist the receipt."""
    session_id = str(session_id or "").strip()
    paths = sorted({str(item) for item in changed_paths if str(item).strip()})
    resolved_pairs = [(path, _repo_for_path(path)) for path in paths]
    unresolved = [path for path, root in resolved_pairs if root is None]
    if unresolved:
        receipt = _failure_receipt(
            session_id=session_id,
            repo="",
            changed_paths=unresolved,
            reason="changed path is not inside a resolvable Git repository",
        )
        return _persist_receipt_or_failure(
            receipt,
            session_id=session_id,
            repo="",
            changed_paths=unresolved,
        )
    roots = tuple(sorted({root for _path, root in resolved_pairs if root is not None}, key=str))
    results: list[dict[str, Any]] = []
    for root in roots:
        repo_paths = [
            path for path, path_root in resolved_pairs if path_root == root
        ]
        try:
            executable, executable_sha256 = _resolve_ocr_executable()
            _head, workspace_digest = _git_identity(root, repo_paths)
            cache_key = (
                session_id,
                str(root),
                workspace_digest,
                executable_sha256,
            )
            with _CACHE_LOCK:
                cached = _PASS_CACHE.get(cache_key)
                if cached is not None:
                    try:
                        cached_at = float(cached.get("created_at", 0.0))
                    except (TypeError, ValueError):
                        cached_at = 0.0
                    if time.time() - cached_at <= _PASS_CACHE_TTL_SECONDS:
                        results.append(cached)
                        continue
                    _PASS_CACHE.pop(cache_key, None)
            receipt = _verify_repo(
                session_id=session_id,
                root=root,
                changed_paths=repo_paths,
                executable=executable,
                executable_sha256=executable_sha256,
            )
            receipt = _persist_receipt_or_failure(
                receipt,
                session_id=session_id,
                repo=str(root),
                changed_paths=repo_paths,
            )
            if receipt.get("status") == "passed":
                with _CACHE_LOCK:
                    while len(_PASS_CACHE) >= _MAX_PASS_CACHE_ENTRIES:
                        oldest = min(
                            _PASS_CACHE,
                            key=lambda key: float(_PASS_CACHE[key].get("created_at", 0.0) or 0.0),
                        )
                        _PASS_CACHE.pop(oldest, None)
                    _PASS_CACHE[cache_key] = receipt
            results.append(receipt)
        except (OSError, OcrGateError, ValueError) as exc:
            failure = _failure_receipt(
                session_id=session_id,
                repo=str(root),
                changed_paths=repo_paths,
                reason=str(exc),
            )
            results.append(
                _persist_receipt_or_failure(
                    failure,
                    session_id=session_id,
                    repo=str(root),
                    changed_paths=repo_paths,
                )
            )
    failed = [item for item in results if item.get("status") != "passed"]
    return {
        "schema": _RECEIPT_SCHEMA,
        "status": "failed" if failed else "passed",
        "session_id": session_id,
        "repositories": results,
        "reason": failed[0].get("reason") if failed else None,
    }


def _is_bestplan_turn(session_id: str, final_response: str) -> bool:
    if _BESTPLAN_MARKER_RE.search(str(final_response or "")):
        return True
    now = time.time()
    with _CACHE_LOCK:
        deadline = _ACTIVE_SESSIONS.get(str(session_id or ""), 0.0)
        if deadline and deadline >= now:
            return True
        _ACTIVE_SESSIONS.pop(str(session_id or ""), None)
    return False


def pre_verify(**kwargs: Any) -> dict[str, str] | None:
    """Require fresh OCR evidence before a BestPlan coding turn can finish."""
    if not kwargs.get("coding"):
        return None
    session_id = str(kwargs.get("session_id") or "")
    changed_paths = kwargs.get("changed_paths") or []
    if not isinstance(changed_paths, (list, tuple, set)) or not changed_paths:
        return None
    if not _is_bestplan_turn(session_id, str(kwargs.get("final_response") or "")):
        return None
    try:
        result = verify_ocr_for_turn(session_id=session_id, changed_paths=changed_paths)
    except Exception as exc:
        return {
            "action": "continue",
            "message": (
                "BestPlan OCR verification gate failed internally. Do not claim the "
                "implementation complete. Fix or report the exact blocker, then "
                f"rerun the OCR-backed review. Reason: {type(exc).__name__}"
            ),
        }
    if result.get("status") == "passed":
        return None
    reason = str(result.get("reason") or "OCR did not produce a valid receipt")
    return {
        "action": "continue",
        "message": (
            "BestPlan OCR verification gate failed. Do not claim the implementation "
            "complete. Fix the exact blocker, then rerun the OCR-backed review. "
            f"Reason: {reason[:400]}"
        ),
    }


__all__ = ["OcrGateError", "mark_bestplan_session", "pre_verify", "verify_ocr_for_turn"]
