"""Dedicated subprocess entrypoint for one isolated BestPlan slice."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import os
import socket
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any


_RESULT_MARKER = "HERMES_BESTPLAN_RESULT="
_MAX_FRAME_BYTES = 4 * 1024 * 1024
_MAX_STDIN_BYTES = 2 * 1024 * 1024
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_SEARCH_BYTES = 64 * 1024 * 1024
_MAX_SEARCH_ENTRIES = 25_000
_MAX_SEARCH_RESULT_BYTES = 2 * 1024 * 1024
_MAX_SEARCH_SECONDS = 5.0
_MAX_RESULT_SUMMARY_CHARS = 16_000
_MAX_TOOL_ARGUMENT_BYTES = 256 * 1024
_MAX_INNER_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_CANDIDATE_TOOL_OPERATIONS = 128
_MAX_CANDIDATE_READ_BYTES = 128 * 1024 * 1024
_MAX_CANDIDATE_WRITE_BYTES = 64 * 1024 * 1024
_MAX_CANDIDATE_SEARCH_BYTES = 128 * 1024 * 1024
_EXACT_LEASE_STAGE_PREFIX = ".hermes-bestplan-stage-"
_BROKER_RUNTIME_KEYS = frozenset({
    "bestplan_toolsets",
    "max_output_tokens",
    "model",
    "request_overrides",
})
_FORBIDDEN_REQUEST_KEYS = frozenset({
    "api_key",
    "api_mode",
    "base_url",
    "command",
    "endpoint",
    "extra_headers",
    "headers",
    "provider",
})
_ALLOWED_REQUEST_OVERRIDE_KEYS = frozenset({
    "frequency_penalty",
    "presence_penalty",
    "reasoning_effort",
    "seed",
    "stop",
    "temperature",
    "top_p",
})
_ALLOWED_MODEL_REQUEST_KEYS = frozenset({
    "frequency_penalty",
    "max_completion_tokens",
    "max_tokens",
    "messages",
    "model",
    "n",
    "parallel_tool_calls",
    "presence_penalty",
    "reasoning_effort",
    "response_format",
    "seed",
    "stop",
    "stream",
    "temperature",
    "tool_choice",
    "tools",
    "top_p",
})
_CANDIDATE_TOOL_NAMES = frozenset({
    "patch",
    "read_file",
    "search_files",
    "write_file",
})
CANDIDATE_TOOL_SCHEMAS = {
    "read_file": {
        "name": "read_file",
        "description": "Read bounded UTF-8 text inside the candidate source tree.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 1, "maximum": 10_000_000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "write_file": {
        "name": "write_file",
        "description": "Write bounded UTF-8 text inside an approved candidate lease.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    "patch": {
        "name": "patch",
        "description": "Replace exact text inside one approved candidate file.",
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["replace"]},
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        },
    },
    "search_files": {
        "name": "search_files",
        "description": "Search bounded candidate paths or literal UTF-8 text.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "target": {"type": "string", "enum": ["content", "files"]},
                "path": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "offset": {"type": "integer", "minimum": 0, "maximum": 1_000_000},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
}


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _exact_lease_stage_path(path: Path) -> Path:
    digest = hashlib.sha256(os.fsencode(str(path))).hexdigest()[:24]
    return path.parent / f"{_EXACT_LEASE_STAGE_PREFIX}{digest}"


class _BrokerChannel:
    """One bounded canonical request/response stream over an inherited socket."""

    def __init__(self, channel: socket.socket | int):
        if isinstance(channel, bool):
            raise ValueError("broker channel is invalid")
        if isinstance(channel, int):
            channel = socket.socket(fileno=channel)
        if not isinstance(channel, socket.socket):
            raise ValueError("broker channel must be a socket")
        if channel.family != socket.AF_UNIX or (
            channel.type & socket.SOCK_STREAM
        ) != socket.SOCK_STREAM:
            raise ValueError("broker channel must be an AF_UNIX stream")
        self._channel = channel
        self._lock = threading.Lock()
        self._closed = False

    def _receive_exact(self, size: int) -> bytes:
        output = bytearray()
        while len(output) < size:
            chunk = self._channel.recv(size - len(output))
            if not chunk:
                raise EOFError("broker channel closed")
            output.extend(chunk)
        return bytes(output)

    def _receive(self) -> dict[str, object]:
        size = struct.unpack("!I", self._receive_exact(4))[0]
        if size <= 0 or size > _MAX_FRAME_BYTES:
            raise ValueError("broker response frame is outside the bounded limit")
        raw = self._receive_exact(size)
        try:
            decoded = json.loads(
                raw.decode("utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON number: {value}")
                ),
            )
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("broker response frame is invalid") from exc
        if not isinstance(decoded, dict) or _canonical_json(decoded).encode() != raw:
            raise ValueError("broker response frame is not canonical")
        return decoded

    def request(self, value: dict[str, object]) -> dict[str, object]:
        encoded = _canonical_json(value).encode("utf-8")
        if not encoded or len(encoded) > _MAX_FRAME_BYTES:
            raise ValueError("broker request frame is outside the bounded limit")
        with self._lock:
            if self._closed:
                raise EOFError("broker channel is closed")
            self._channel.sendall(struct.pack("!I", len(encoded)) + encoded)
            return self._receive()

    def wait_for_host_close(self) -> None:
        """Keep the admitted worker alive until the host seals broker admission."""

        with self._lock:
            if self._closed:
                return
            while True:
                chunk = self._channel.recv(1)
                if not chunk:
                    return
                raise ValueError("broker channel received data after final result")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._channel.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._channel.close()


class _BrokerOpenAIClient:
    """Small OpenAI-shaped facade whose only transport is ``_BrokerChannel``."""

    def __init__(
        self,
        channel: _BrokerChannel,
        *,
        expected_model: str | None = None,
        max_output_tokens: int = 32_768,
    ):
        self._channel = channel
        self._expected_model = expected_model
        self._max_output_tokens = max_output_tokens
        self._request_number = 0
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create),
        )

    def is_closed(self) -> bool:
        return False

    def close(self) -> None:
        # AIAgent treats primary and request-local clients as separately owned.
        # They are the same broker facade here, so only the worker owns closure.
        return None

    def _create(self, **kwargs):
        local_timeout = kwargs.pop("timeout", None)
        if local_timeout is not None and (
            isinstance(local_timeout, bool)
            or not isinstance(local_timeout, (int, float))
            or not math.isfinite(float(local_timeout))
            or not 0 < float(local_timeout) <= 86_400
        ):
            raise ValueError("brokered model request timeout is invalid")
        if kwargs.get("stream") not in (None, False):
            raise ValueError("brokered model requests do not allow stream")
        if set(kwargs) - _ALLOWED_MODEL_REQUEST_KEYS or any(
            key in kwargs for key in _FORBIDDEN_REQUEST_KEYS
        ):
            raise ValueError("brokered model request contains routing data")
        model = kwargs.get("model")
        if not isinstance(model, str) or not model:
            raise ValueError("brokered model request requires a model")
        if self._expected_model is not None and model != self._expected_model:
            raise ValueError("brokered model request model mismatch")
        if not isinstance(kwargs.get("messages"), list):
            raise ValueError("brokered model request requires messages")
        requested_tokens = kwargs.get(
            "max_completion_tokens", kwargs.get("max_tokens", self._max_output_tokens),
        )
        if (
            isinstance(requested_tokens, bool)
            or not isinstance(requested_tokens, int)
            or requested_tokens < 1
            or requested_tokens > self._max_output_tokens
        ):
            raise ValueError("brokered model request exceeds its token cap")
        advertised_tools = _validate_candidate_tools(kwargs.get("tools"))
        _validate_tool_choice(kwargs.get("tool_choice"), advertised_tools)
        request_body = dict(kwargs)
        request_body["stream"] = False
        request_json = _canonical_json(request_body)
        if len(request_json.encode("utf-8")) > _MAX_FRAME_BYTES:
            raise ValueError("brokered model request exceeds the frame limit")
        self._request_number += 1
        request_id = f"turn-{self._request_number:08d}"
        response = self._channel.request({
            "max_output_tokens": requested_tokens,
            "request": request_body,
            "request_id": request_id,
        })
        if set(response) != {"ok", "request_id", "response_json"}:
            raise ValueError("broker response envelope is invalid")
        if response.get("ok") is not True or response.get("request_id") != request_id:
            raise ValueError("broker response request identity differs")
        response_json = response.get("response_json")
        if (
            not isinstance(response_json, str)
            or len(response_json.encode("utf-8")) > _MAX_INNER_RESPONSE_BYTES
        ):
            raise ValueError("broker response JSON is invalid")
        try:
            response_body = json.loads(response_json)
        except json.JSONDecodeError as exc:
            raise ValueError("broker response JSON is invalid") from exc
        if not isinstance(response_body, dict) or _canonical_json(response_body) != response_json:
            raise ValueError("broker response JSON is not canonical")
        _validate_candidate_response(
            response_body,
            expected_model=model,
            advertised_tools=advertised_tools,
        )
        from openai.types.chat import ChatCompletion

        try:
            validator = getattr(ChatCompletion, "model_validate", None)
            if callable(validator):
                return validator(response_body)
            return ChatCompletion.parse_obj(response_body)
        except BaseException:
            raise ValueError("broker response validation failed") from None


def _validate_candidate_tools(value: object) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or len(value) > len(_CANDIDATE_TOOL_NAMES):
        raise ValueError("brokered model request tool schema is invalid")
    names: set[str] = set()
    for schema in value:
        if not isinstance(schema, dict) or schema.get("type") != "function":
            raise ValueError("brokered model request tool schema is invalid")
        function = schema.get("function")
        if not isinstance(function, dict):
            raise ValueError("brokered model request tool schema is invalid")
        name = function.get("name")
        parameters = function.get("parameters")
        if (
            not isinstance(name, str)
            or name not in _CANDIDATE_TOOL_NAMES
            or name in names
            or schema != {
                "type": "function",
                "function": CANDIDATE_TOOL_SCHEMAS.get(name),
            }
        ):
            raise ValueError("brokered model request tool schema is invalid")
        if len(_canonical_json(schema).encode("utf-8")) > 128 * 1024:
            raise ValueError("brokered model request tool schema is oversized")
        names.add(name)
    return frozenset(names)


def _validate_tool_choice(value: object, advertised: frozenset[str]) -> None:
    if value is None or value in ("auto", "none", "required"):
        return
    if not isinstance(value, dict) or value.get("type") != "function":
        raise ValueError("brokered model request tool choice is invalid")
    function = value.get("function")
    if (
        not isinstance(function, dict)
        or set(function) != {"name"}
        or function.get("name") not in advertised
    ):
        raise ValueError("brokered model request tool choice is invalid")


def _validate_candidate_response(
    value: dict[str, object],
    *,
    expected_model: str,
    advertised_tools: frozenset[str],
) -> None:
    if value.get("model") != expected_model:
        raise ValueError("broker response model mismatch")
    choices = value.get("choices")
    if not isinstance(choices, list) or not 1 <= len(choices) <= 16:
        raise ValueError("broker response choices are invalid")
    for choice in choices:
        if not isinstance(choice, dict):
            raise ValueError("broker response choice is invalid")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ValueError("broker response message is invalid")
        tool_calls = message.get("tool_calls", [])
        if tool_calls is None:
            tool_calls = []
        if not isinstance(tool_calls, list) or len(tool_calls) > 64:
            raise ValueError("broker response tool calls are invalid")
        for call in tool_calls:
            if not isinstance(call, dict) or call.get("type") != "function":
                raise ValueError("broker response tool call is invalid")
            function = call.get("function")
            if not isinstance(function, dict):
                raise ValueError("broker response tool call is invalid")
            name = function.get("name")
            arguments = function.get("arguments")
            if name not in advertised_tools:
                raise ValueError("broker response tool is not advertised")
            if (
                not isinstance(arguments, str)
                or len(arguments.encode("utf-8")) > _MAX_TOOL_ARGUMENT_BYTES
            ):
                raise ValueError("broker response tool arguments are invalid")
            try:
                parsed_arguments = json.loads(arguments)
            except (json.JSONDecodeError, RecursionError):
                raise ValueError("broker response tool arguments are invalid") from None
            if not isinstance(parsed_arguments, dict):
                raise ValueError("broker response tool arguments are invalid")


def _validate_brokered_runtime(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _BROKER_RUNTIME_KEYS:
        raise ValueError("brokered runtime has unexpected fields")
    model = value.get("model")
    if not isinstance(model, str) or not model or "\x00" in model:
        raise ValueError("brokered runtime model is invalid")
    toolsets = value.get("bestplan_toolsets")
    if toolsets not in (["file"], ["read_only_files"]):
        raise ValueError("brokered runtime toolset is not process-free")
    max_output_tokens = value.get("max_output_tokens")
    if (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or not 1 <= max_output_tokens <= 32_768
    ):
        raise ValueError("brokered runtime max_output_tokens is invalid")
    overrides = value.get("request_overrides")
    if not isinstance(overrides, dict):
        raise ValueError("brokered runtime request_overrides must be an object")
    if "stream" in overrides:
        raise ValueError("brokered runtime cannot configure stream")
    if set(overrides) - _ALLOWED_REQUEST_OVERRIDE_KEYS or any(
        key in overrides for key in _FORBIDDEN_REQUEST_KEYS
    ):
        raise ValueError("brokered runtime contains routing data")
    encoded = _canonical_json(overrides).encode("utf-8")
    if len(encoded) > 64 * 1024:
        raise ValueError("brokered runtime request_overrides are oversized")
    return {
        "model": model,
        "bestplan_toolsets": list(toolsets),
        "max_output_tokens": max_output_tokens,
        "request_overrides": dict(overrides),
    }


class _CandidateFileTools:
    """Process-free source reads and lease-bound text edits."""

    def __init__(
        self,
        *,
        workspace: str | Path,
        allowed_paths: tuple[str, ...] | list[str],
        read_only: bool,
    ):
        self.workspace = Path(os.path.abspath(os.fspath(workspace)))
        if not self.workspace.is_dir():
            raise ValueError("candidate workspace is not a directory")
        self.read_only = bool(read_only)
        leases: list[Path] = []
        for raw in allowed_paths:
            if not isinstance(raw, str) or not raw or "\x00" in raw:
                raise ValueError("candidate write lease is invalid")
            relative = Path(raw)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("candidate write lease is invalid")
            lease = Path(os.path.abspath(self.workspace / relative))
            if lease != self.workspace and self.workspace not in lease.parents:
                raise ValueError("candidate write lease escapes the workspace")
            leases.append(lease)
        self.leases = tuple(sorted(set(leases), key=str))
        self.exact_file_leases = frozenset(
            lease for lease in self.leases if not lease.is_dir()
        )
        if self.read_only and self.leases:
            raise ValueError("read-only candidate cannot have write leases")
        self._budget_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._tool_operations = 0
        self._read_bytes = 0
        self._write_bytes = 0
        self._search_bytes = 0

    def _reserve_operation(self) -> None:
        with self._budget_lock:
            if self._tool_operations + 1 > _MAX_CANDIDATE_TOOL_OPERATIONS:
                raise ValueError("candidate tool operation budget exhausted")
            self._tool_operations += 1

    def _reserve_bytes(self, kind: str, amount: int) -> None:
        limits = {
            "read": ("_read_bytes", _MAX_CANDIDATE_READ_BYTES),
            "write": ("_write_bytes", _MAX_CANDIDATE_WRITE_BYTES),
            "search": ("_search_bytes", _MAX_CANDIDATE_SEARCH_BYTES),
        }
        field, maximum = limits[kind]
        with self._budget_lock:
            current = int(getattr(self, field))
            if amount < 0 or current + amount > maximum:
                raise ValueError(f"candidate {kind} byte budget exhausted")
            setattr(self, field, current + amount)

    def _path(self, raw: object, *, writing: bool) -> Path:
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise ValueError("candidate file path is invalid")
        supplied = Path(raw).expanduser()
        lexical = supplied if supplied.is_absolute() else self.workspace / supplied
        lexical = Path(os.path.abspath(lexical))
        if lexical != self.workspace and self.workspace not in lexical.parents:
            raise ValueError("candidate file path escapes the workspace")
        current = self.workspace
        relative_parts = lexical.relative_to(self.workspace).parts
        for part in relative_parts[:-1] if writing else relative_parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                if writing:
                    continue
                raise
            if stat_is_symlink(info.st_mode):
                raise ValueError("candidate file path contains a symlink")
        if writing:
            if self.read_only:
                raise ValueError("candidate write lease is read-only")
            if not any(
                lexical == lease or lease in lexical.parents for lease in self.leases
            ):
                raise ValueError("candidate file path is outside the write lease")
            try:
                if stat_is_symlink(lexical.lstat().st_mode):
                    raise ValueError("candidate file path is a symlink")
            except FileNotFoundError:
                pass
        return lexical

    @staticmethod
    def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("candidate file bound must be an integer")
        return min(maximum, max(minimum, value))

    def read(self, args: dict[str, object], **_kwargs) -> str:
        self._reserve_operation()
        path = self._path(args.get("path"), writing=False)
        text = self._bounded_text(path, budget="read")
        offset = self._bounded_int(args.get("offset"), 1, 1, 10_000_000)
        limit = self._bounded_int(args.get("limit"), 2000, 1, 2000)
        lines = text.splitlines()
        selected = lines[offset - 1:offset - 1 + limit]
        return "\n".join(
            f"{index}|{line}"
            for index, line in enumerate(selected, start=offset)
        )

    def _atomic_write(
        self, path: Path, content: str, *, write_budget_reserved: bool = False,
    ) -> dict[str, object]:
        if not isinstance(content, str):
            raise ValueError("candidate file content must be text")
        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_FILE_BYTES:
            raise ValueError("candidate file content exceeds the bounded limit")
        if not write_budget_reserved:
            self._reserve_bytes("write", len(encoded))
        if path.is_dir():
            raise ValueError("candidate file path is a directory")
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        with self._write_lock:
            if path in self.exact_file_leases:
                temporary = _exact_lease_stage_path(path)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(temporary, flags, 0o600)
            else:
                descriptor, raw_temporary = tempfile.mkstemp(
                    prefix=".bestplan-write-", dir=path.parent,
                )
                temporary = Path(raw_temporary)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, 0o644)
                os.replace(temporary, path)
            except BaseException:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                raise
        return {
            "path": str(path.relative_to(self.workspace)),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "verified": path.read_bytes() == encoded,
        }

    def write(self, args: dict[str, object], **_kwargs) -> str:
        self._reserve_operation()
        path = self._path(args.get("path"), writing=True)
        result = self._atomic_write(path, args.get("content"))
        return _canonical_json(result)

    def patch(self, args: dict[str, object], **_kwargs) -> str:
        self._reserve_operation()
        if args.get("mode", "replace") != "replace":
            raise ValueError("candidate patch supports process-free replace mode only")
        path = self._path(args.get("path"), writing=True)
        old = args.get("old_string")
        new = args.get("new_string")
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise ValueError("candidate patch requires old_string and new_string")
        content = self._bounded_text(path, budget="read")
        count = content.count(old)
        replace_all = args.get("replace_all") is True
        if count == 0 or (count != 1 and not replace_all):
            raise ValueError("candidate patch match is absent or ambiguous")
        replacements = count if replace_all else 1
        projected = (
            len(content.encode("utf-8"))
            - replacements * len(old.encode("utf-8"))
            + replacements * len(new.encode("utf-8"))
        )
        if projected > _MAX_FILE_BYTES:
            raise ValueError("candidate patch output exceeds the bounded limit")
        self._reserve_bytes("write", projected)
        updated = content.replace(old, new) if replace_all else content.replace(old, new, 1)
        result = self._atomic_write(path, updated, write_budget_reserved=True)
        result["replacements"] = count if replace_all else 1
        return _canonical_json(result)

    def search(self, args: dict[str, object], **_kwargs) -> str:
        self._reserve_operation()
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern or len(pattern) > 4096:
            raise ValueError("candidate search pattern is invalid")
        root = self._path(args.get("path", "."), writing=False)
        target = args.get("target", "content")
        limit = self._bounded_int(args.get("limit"), 50, 1, 200)
        offset = self._bounded_int(args.get("offset"), 0, 0, 1_000_000)
        results: list[str] = []
        entries = 0
        total_bytes = 0
        result_bytes = 0
        deadline = time.monotonic() + _MAX_SEARCH_SECONDS
        paths = (root,) if root.is_file() else self._iter_paths(root)
        for path in paths:
            if time.monotonic() >= deadline:
                raise ValueError("candidate search deadline expired")
            entries += 1
            if entries > _MAX_SEARCH_ENTRIES:
                raise ValueError("candidate search exceeds the entry limit")
            if path.is_symlink() or not path.is_file():
                continue
            relative = str(path.relative_to(self.workspace))
            if target == "files":
                if fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(relative, pattern):
                    results.append(relative)
            elif target == "content":
                info = path.stat(follow_symlinks=False)
                total_bytes += info.st_size
                if total_bytes > _MAX_SEARCH_BYTES:
                    raise ValueError("candidate search exceeds the content limit")
                try:
                    text = self._bounded_text(path, budget="search")
                except UnicodeDecodeError:
                    continue
                for number, line in enumerate(text.splitlines(), start=1):
                    if time.monotonic() >= deadline:
                        raise ValueError("candidate search deadline expired")
                    if pattern in line:
                        result = f"{relative}:{number}:{line[:2000]}"
                        result_bytes += len(result.encode("utf-8"))
                        if result_bytes > _MAX_SEARCH_RESULT_BYTES:
                            raise ValueError("candidate search result exceeds the bounded limit")
                        results.append(result)
                        if len(results) >= offset + limit:
                            break
            else:
                raise ValueError("candidate search target is invalid")
            if len(results) >= offset + limit:
                break
        return "\n".join(results[offset:offset + limit])

    @staticmethod
    def _iter_paths(root: Path):
        stack = [root]
        while stack:
            directory = stack.pop()
            children: list[Path] = []
            with os.scandir(directory) as iterator:
                for child in iterator:
                    children.append(Path(child.path))
                    if len(children) > _MAX_SEARCH_ENTRIES:
                        raise ValueError("candidate search exceeds the entry limit")
            children.sort(key=lambda item: os.fsencode(item.name))
            for child in children:
                yield child
            for child in reversed(children):
                try:
                    if child.is_dir() and not child.is_symlink():
                        stack.append(child)
                except OSError:
                    continue

    def _bounded_text(self, path: Path, *, budget: str) -> str:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            import stat

            if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_FILE_BYTES:
                raise ValueError("candidate file is not a bounded regular file")
            self._reserve_bytes(budget, before.st_size)
            data = bytearray()
            while len(data) <= _MAX_FILE_BYTES:
                chunk = os.read(descriptor, min(1024 * 1024, _MAX_FILE_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if len(data) > _MAX_FILE_BYTES or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("candidate file did not remain bounded and stable")
        return bytes(data).decode("utf-8")


def stat_is_symlink(mode: int) -> bool:
    import stat

    return stat.S_ISLNK(mode)


def _install_candidate_file_tools(file_tools: _CandidateFileTools) -> None:
    from tools.registry import ToolEntry, invalidate_check_fn_cache, registry

    handlers = {
        "read_file": file_tools.read,
        "write_file": file_tools.write,
        "patch": file_tools.patch,
        "search_files": file_tools.search,
    }
    with registry._lock:
        for table in (registry._tools, registry._builtin_tools):
            for name, handler in handlers.items():
                old = table.get(name)
                if old is None:
                    raise ValueError("candidate file tool registration is unavailable")
                table[name] = ToolEntry(
                    name=old.name,
                    toolset=old.toolset,
                    schema=CANDIDATE_TOOL_SCHEMAS[name],
                    handler=handler,
                    check_fn=lambda: True,
                    requires_env=[],
                    is_async=False,
                    description=old.description,
                    emoji=old.emoji,
                    max_result_size_chars=old.max_result_size_chars,
                    dynamic_schema_overrides=None,
                )
        registry._generation += 1
    invalidate_check_fn_cache()


def _brokered_agent_class(broker_client: _BrokerOpenAIClient):
    from run_agent import AIAgent

    class _BrokeredAIAgent(AIAgent):
        def _create_openai_client(self, _client_kwargs, *, reason, shared):
            del reason, shared
            return broker_client

        def _create_request_openai_client(self, *, reason, api_kwargs=None):
            del reason, api_kwargs
            return broker_client

        def _ensure_primary_openai_client(self, *, reason):
            del reason
            return broker_client

        def _close_request_openai_client(self, client, *, reason):
            del client, reason

        def _close_openai_client(self, client, *, reason, shared):
            del client, reason, shared

    return _BrokeredAIAgent


def _install_bestplan_import_guard() -> None:
    """Prevent controller/user secret-source loading before importing AIAgent."""

    from hermes_cli import env_loader

    def _no_secret_sources(*_args, **_kwargs):
        return None

    env_loader.load_hermes_dotenv = _no_secret_sources


def _assert_exact_worker_environment(expected: dict[str, str]) -> None:
    if dict(os.environ) != expected:
        raise ValueError("worker environment changed outside the launch contract")


def _disable_auxiliary_model_paths(agent: object) -> None:
    """Keep every model-bearing path on the inherited broker facade."""

    setattr(agent, "compression_enabled", False)
    compressor = getattr(agent, "context_compressor", None)
    if compressor is not None and hasattr(compressor, "_micro_compact_enabled"):
        compressor._micro_compact_enabled = False


def _bounded_payload() -> dict[str, object]:
    raw = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
    if len(raw) > _MAX_STDIN_BYTES:
        raise ValueError("worker input exceeds the bounded limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("worker input is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("worker input must be an object")
    return value


def _validate_brokered_payload(payload: object) -> dict[str, object]:
    expected = {
        "allowed_paths",
        "goal",
        "max_iterations",
        "read_only",
        "runtime",
        "runtime_home",
        "system_prompt",
        "task_id",
        "workspace",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError("worker payload fields are invalid")
    workspace_raw = str(payload["workspace"])
    runtime_home_raw = str(payload["runtime_home"])
    workspace = Path(os.path.abspath(workspace_raw))
    runtime_home = Path(os.path.abspath(runtime_home_raw))
    if (
        not Path(workspace_raw).is_absolute()
        or not Path(runtime_home_raw).is_absolute()
        or str(workspace) != workspace_raw
        or str(runtime_home) != runtime_home_raw
        or workspace != Path(os.getcwd())
        or not runtime_home.is_dir()
    ):
        raise ValueError("worker payload roots differ from the launch boundary")
    allowed_paths = payload["allowed_paths"]
    if not isinstance(allowed_paths, list) or any(
        not isinstance(item, str) for item in allowed_paths
    ):
        raise ValueError("worker payload write leases are invalid")
    read_only = payload["read_only"]
    if not isinstance(read_only, bool):
        raise ValueError("worker payload read_only is invalid")
    max_iterations = payload["max_iterations"]
    if (
        isinstance(max_iterations, bool)
        or not isinstance(max_iterations, int)
        or not 1 <= max_iterations <= 500
    ):
        raise ValueError("worker payload max_iterations is invalid")
    for field in ("goal", "system_prompt", "task_id"):
        if not isinstance(payload[field], str) or len(payload[field]) > 256_000:
            raise ValueError("worker payload text field is invalid")
    return {
        **payload,
        "workspace": workspace,
        "runtime_home": runtime_home,
        "runtime": _validate_brokered_runtime(payload["runtime"]),
        "allowed_paths": list(allowed_paths),
    }


def _emit_result(value: dict[str, object]) -> None:
    encoded = _canonical_json(value)
    sys.stdout.write(_RESULT_MARKER + encoded + "\n")
    sys.stdout.flush()


def _brokered_main() -> int:
    channel: _BrokerChannel | None = None
    agent = None
    started = time.monotonic()
    try:
        launch_environment = dict(os.environ)
        payload = _validate_brokered_payload(_bounded_payload())
        runtime = payload["runtime"]
        assert isinstance(runtime, dict)
        workspace = payload["workspace"]
        runtime_home = payload["runtime_home"]
        assert isinstance(workspace, Path)
        assert isinstance(runtime_home, Path)
        descriptor = int(os.environ["HERMES_BESTPLAN_BROKER_FD"])
        channel = _BrokerChannel(descriptor)
        broker_client = _BrokerOpenAIClient(
            channel,
            expected_model=str(runtime["model"]),
            max_output_tokens=int(runtime["max_output_tokens"]),
        )
        os.environ["HERMES_HOME"] = str(runtime_home)
        os.environ["TERMINAL_CWD"] = str(workspace)

        from agent.delegation_context import bestplan_child_context
        from hermes_constants import set_hermes_home_override

        set_hermes_home_override(runtime_home)
        tools = _CandidateFileTools(
            workspace=workspace,
            allowed_paths=payload["allowed_paths"],
            read_only=bool(payload["read_only"]),
        )
        with bestplan_child_context(str(payload["task_id"])):
            # Importing run_agent registers the checked-in schemas. Replace only
            # their handlers after that import and before the tool snapshot.
            _install_bestplan_import_guard()
            AgentClass = _brokered_agent_class(broker_client)
            _assert_exact_worker_environment(launch_environment)
            _install_candidate_file_tools(tools)
            agent = AgentClass(
                base_url="http://bestplan-broker.invalid/v1",
                api_key="bestplan-broker-no-provider-credential",
                provider="openai",
                api_mode="chat_completions",
                model=str(runtime["model"]),
                max_iterations=int(payload["max_iterations"]),
                max_tokens=int(runtime["max_output_tokens"]),
                request_overrides=dict(runtime["request_overrides"]),
                enabled_toolsets=list(runtime["bestplan_toolsets"]),
                quiet_mode=True,
                save_trajectories=False,
                platform="bestplan-worker",
                skip_context_files=True,
                skip_memory=True,
                checkpoints_enabled=False,
            )
            _disable_auxiliary_model_paths(agent)
            _assert_exact_worker_environment(launch_environment)
            agent._disable_streaming = True
            agent.terminal_cwd = str(workspace)
            result = agent.run_conversation(
                user_message=str(payload["goal"]),
                system_message=str(payload["system_prompt"]),
                conversation_history=[],
                task_id=str(payload["task_id"]),
            )
        summary = str(result.get("final_response") or "")[:_MAX_RESULT_SUMMARY_CHARS]
        output = {
            "api_calls": int(result.get("api_calls") or 0),
            "duration_seconds": round(time.monotonic() - started, 2),
            "error": None,
            "model": str(runtime["model"]),
            "status": "completed" if result.get("completed", True) else "error",
            "summary": summary,
        }
        _emit_result(output)
        if output["status"] == "completed":
            channel.wait_for_host_close()
        return 0 if output["status"] == "completed" else 1
    except BaseException:
        _emit_result({
            "api_calls": 0,
            "error": "candidate_worker_failed",
            "status": "error",
            "summary": "",
        })
        return 1
    finally:
        if agent is not None:
            try:
                agent.close()
            except BaseException:
                pass
        if channel is not None:
            try:
                channel.close()
            except BaseException:
                pass


def _legacy_main() -> int:
    """Compatibility entrypoint retained until the Task 5 caller migrates."""

    try:
        payload = json.loads(sys.stdin.read())
        runtime = dict(payload["runtime"])
        workspace = Path(payload["workspace"]).resolve()
        runtime_home = Path(payload["runtime_home"]).resolve()
        os.environ["HERMES_HOME"] = str(runtime_home)
        os.environ["TERMINAL_CWD"] = str(workspace)
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

        from hermes_constants import set_hermes_home_override
        from run_agent import AIAgent

        set_hermes_home_override(runtime_home)
        toolsets = list(runtime["bestplan_toolsets"])
        started = time.monotonic()
        agent = AIAgent(
            base_url=runtime.get("base_url"),
            api_key=runtime.get("api_key"),
            provider=runtime.get("provider"),
            api_mode=runtime.get("api_mode"),
            command=runtime.get("command"),
            args=runtime.get("args"),
            acp_command=runtime.get("acp_command"),
            acp_args=runtime.get("acp_args"),
            model=str(runtime.get("model") or ""),
            max_iterations=int(payload.get("max_iterations") or 50),
            max_tokens=runtime.get("max_output_tokens"),
            request_overrides=runtime.get("request_overrides"),
            enabled_toolsets=toolsets,
            quiet_mode=True,
            save_trajectories=False,
            platform="bestplan-worker",
            skip_context_files=True,
            skip_memory=True,
            checkpoints_enabled=False,
        )
        agent.terminal_cwd = str(workspace)
        try:
            result = agent.run_conversation(
                user_message=str(payload.get("goal") or ""),
                system_message=str(payload.get("system_prompt") or ""),
                conversation_history=[],
                task_id=str(payload.get("task_id") or "bestplan"),
            )
            output = {
                "status": "completed" if result.get("completed", True) else "error",
                "summary": str(result.get("final_response") or ""),
                "error": result.get("error"),
                "api_calls": int(result.get("api_calls") or 0),
                "duration_seconds": round(time.monotonic() - started, 2),
                "model": str(runtime.get("model") or ""),
            }
        finally:
            agent.close()
        sys.stdout.write(_RESULT_MARKER + json.dumps(output, ensure_ascii=False))
        sys.stdout.flush()
        return 0
    except BaseException as exc:
        sys.stdout.write(_RESULT_MARKER + json.dumps({
            "status": "error",
            "summary": "",
            "error": f"{type(exc).__name__}: {exc}",
            "api_calls": 0,
        }))
        sys.stdout.flush()
        return 1


def _candidate_broker_descriptor_is_valid(value: object) -> bool:
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        return False
    try:
        descriptor = int(value, 10)
    except ValueError:
        return False
    if descriptor < 3 or str(descriptor) != value:
        return False
    duplicate = None
    try:
        os.fstat(descriptor)
        duplicate = socket.fromfd(descriptor, socket.AF_UNIX, socket.SOCK_STREAM)
        if (
            duplicate.family != socket.AF_UNIX
            or duplicate.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
            != socket.SOCK_STREAM
        ):
            return False
        duplicate.getsockname()
        duplicate.getpeername()
        return True
    except (OSError, ValueError):
        return False
    finally:
        if duplicate is not None:
            duplicate.close()


def _candidate_broker_unavailable() -> int:
    _emit_result({
        "api_calls": 0,
        "error": "candidate_broker_unavailable",
        "status": "error",
        "summary": "",
    })
    return 1


def _main() -> int:
    marker = os.environ.get("HERMES_BESTPLAN_CHILD")
    descriptor = os.environ.get("HERMES_BESTPLAN_BROKER_FD")
    if marker is not None or descriptor is not None:
        if marker != "1" or not _candidate_broker_descriptor_is_valid(descriptor):
            return _candidate_broker_unavailable()
        return _brokered_main()
    return _legacy_main()


if __name__ == "__main__":
    raise SystemExit(_main())
