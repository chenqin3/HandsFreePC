from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import IO, Any

import psutil


class McpClientError(RuntimeError):
    pass


class McpClientUnavailable(McpClientError):
    pass


_ENV_ALLOWLIST = {
    "APPDATA",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
}


def _sanitized_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return only the bounded Windows runtime and locale environment."""

    values = os.environ if source is None else source
    return {key: value for key, value in values.items() if key.upper() in _ENV_ALLOWLIST}


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _stop_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        root = psutil.Process(process.pid)
        descendants = root.children(recursive=True)
        targets = [root, *reversed(descendants)]
        for target in targets:
            with suppress(psutil.Error):
                target.terminate()
        _gone, alive = psutil.wait_procs(targets, timeout=2.0)
        for target in alive:
            with suppress(psutil.Error):
                target.kill()
        if alive:
            psutil.wait_procs(alive, timeout=2.0)
    except (psutil.Error, AttributeError):
        pass
    if process.poll() is None:
        with suppress(OSError):
            process.terminate()
        with suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=2.0)
    if process.poll() is None:
        with suppress(OSError):
            process.kill()
        with suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=2.0)


class PersistentMcpClient:
    """Small sequential JSON-RPC client for a long-lived MCP stdio server."""

    def __init__(
        self,
        command: str,
        args: Sequence[str] = (),
        *,
        timeout_seconds: float = 30.0,
        environment: Mapping[str, str] | None = None,
        working_directory: str | Path | None = None,
        popen_factory: Any | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("MCP command must be a non-empty string")
        self.command = command
        self.args = tuple(str(item) for item in args)
        self.timeout_seconds = float(timeout_seconds)
        self.environment = environment
        self.working_directory = (
            Path(working_directory).expanduser().resolve() if working_directory else None
        )
        self._popen_factory = popen_factory or subprocess.Popen
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._responses: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self._deferred: dict[int, dict[str, Any]] = {}
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._request_id = 0
        self._call_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._cancel_event = threading.Event()
        self._started = False
        self._closed = False

    @property
    def running(self) -> bool:
        process = self._process
        return bool(self._started and process is not None and process.poll() is None)

    def _resolve_command(self) -> str:
        resolved = shutil.which(self.command)
        if resolved is None:
            raise McpClientUnavailable(f"MCP executable was not found: {self.command}")
        return resolved

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                raise McpClientError("MCP client is closed")
            if self.running:
                return
            executable = self._resolve_command()
            try:
                process = self._popen_factory(
                    [executable, *self.args],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    cwd=str(self.working_directory) if self.working_directory else None,
                    env=_sanitized_environment(self.environment),
                    creationflags=_creation_flags(),
                )
            except OSError as exc:
                raise McpClientError(f"MCP process could not start: {type(exc).__name__}") from exc
            if process.stdin is None or process.stdout is None or process.stderr is None:
                _stop_process_tree(process)
                raise McpClientError("MCP process did not expose stdio pipes")
            self._process = process
            self._cancel_event.clear()
            self._reader = threading.Thread(
                target=self._read_stdout,
                args=(process.stdout,),
                name="handsfreepc-mcp-stdout",
                daemon=True,
            )
            self._stderr_reader = threading.Thread(
                target=self._read_stderr,
                args=(process.stderr,),
                name="handsfreepc-mcp-stderr",
                daemon=True,
            )
            self._reader.start()
            self._stderr_reader.start()
            self._started = True
        try:
            result = self.request(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "HandsFreePC", "version": "0.4.0"},
                },
            )
            if not isinstance(result.get("capabilities"), dict):
                raise McpClientError("MCP initialize response had no capabilities")
            self.notify("notifications/initialized", {})
        except Exception:
            self.close()
            raise

    def _read_stdout(self, stream: IO[str]) -> None:
        try:
            for line in stream:
                value = line.strip()
                if not value:
                    continue
                try:
                    payload = json.loads(value)
                except json.JSONDecodeError as exc:
                    self._responses.put(McpClientError("MCP server emitted invalid JSON"))
                    self._responses.put(exc)
                    return
                if isinstance(payload, dict):
                    self._responses.put(payload)
        except BaseException as exc:  # pragma: no cover - OS pipe failures are host-specific
            self._responses.put(exc)
        finally:
            self._responses.put(McpClientError("MCP stdout closed"))

    def _read_stderr(self, stream: IO[str]) -> None:
        with suppress(OSError):
            for line in stream:
                value = line.strip()
                if value:
                    self._stderr_tail.append(value[:500])

    def _write(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise McpClientError("MCP process is not running")
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            process.stdin.write("\n")
            process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise McpClientError("Could not write to MCP process") from exc

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        if not self.running:
            if method == "initialize" and self._started:
                pass
            else:
                self.start()
        with self._call_lock:
            if self._closed:
                raise McpClientError("MCP client is closed")
            self._request_id += 1
            request_id = self._request_id
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params or {},
                }
            )
            deadline = time.monotonic() + self.timeout_seconds
            while True:
                if self._cancel_event.is_set() or (
                    cancel_event is not None and cancel_event.is_set()
                ):
                    self.cancel()
                    raise McpClientError("MCP request was cancelled")
                if request_id in self._deferred:
                    response = self._deferred.pop(request_id)
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self.cancel()
                        raise McpClientError(f"MCP request timed out: {method}")
                    try:
                        item = self._responses.get(timeout=min(0.1, remaining))
                    except queue.Empty:
                        continue
                    if isinstance(item, BaseException):
                        raise McpClientError(str(item) or type(item).__name__) from item
                    response_id = item.get("id")
                    if response_id != request_id:
                        if isinstance(response_id, int):
                            self._deferred[response_id] = item
                        continue
                    response = item
                if "error" in response:
                    error = response.get("error")
                    if isinstance(error, dict):
                        message = str(error.get("message") or "MCP JSON-RPC error")
                    else:
                        message = "MCP JSON-RPC error"
                    raise McpClientError(message)
                result = response.get("result")
                if not isinstance(result, dict):
                    raise McpClientError("MCP response result was not an object")
                return result

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if not self.running:
            raise McpClientError("MCP process is not running")
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def list_tools(self) -> list[dict[str, Any]]:
        result = self.request("tools/list", {})
        tools = result.get("tools")
        if not isinstance(tools, list) or any(not isinstance(item, dict) for item in tools):
            raise McpClientError("MCP tools/list returned an invalid tool list")
        return tools

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        return self.request(
            "tools/call",
            {"name": name, "arguments": arguments},
            cancel_event=cancel_event,
        )

    def cancel(self) -> bool:
        self._cancel_event.set()
        process = self._process
        if process is None or process.poll() is not None:
            return False
        _stop_process_tree(process)
        return True

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            process = self._process
            self._process = None
        if process is not None:
            if process.stdin is not None:
                with suppress(OSError, ValueError):
                    process.stdin.close()
            _stop_process_tree(process)
        for thread in (self._reader, self._stderr_reader):
            if thread is not None and thread.is_alive():
                thread.join(timeout=1.0)
