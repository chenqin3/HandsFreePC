from __future__ import annotations

import os
import sys
import threading

import pytest

from handsfree_pc.desktop.mcp_client import (
    McpClientError,
    PersistentMcpClient,
    _sanitized_environment,
)

_MCP_SERVER = r"""
import json
import os
import sys

initialize_count = 0
initialized_notification = False
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")
    if method == "notifications/initialized":
        initialized_notification = True
        continue
    if method == "initialize":
        initialize_count += 1
        result = {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "serverInfo": {"name": "test-server", "version": "1"},
        }
    elif method == "probe":
        result = {
            "pid": os.getpid(),
            "initialize_count": initialize_count,
            "initialized_notification": initialized_notification,
            "environment_names": sorted(os.environ),
        }
    elif method == "never":
        continue
    elif method == "tools/list":
        result = {"tools": []}
    else:
        result = {"method": method}
    response = {"jsonrpc": "2.0", "id": request_id, "result": result}
    print(json.dumps(response), flush=True)
"""


def _client(*, timeout_seconds: float = 2.0) -> PersistentMcpClient:
    source_environment = {
        "PATH": os.environ.get("PATH", ""),
        "PATHEXT": os.environ.get("PATHEXT", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "WINDIR": os.environ.get("WINDIR", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        "COMSPEC": os.environ.get("COMSPEC", ""),
        "LANG": "zh_CN.UTF-8",
        "HANDSFREEPC_TEST_SAFE": "visible",
        "HANDSFREEPC_TEST_API_KEY": "not-a-real-secret",
        "HANDSFREEPC_TEST_TOKEN": "not-a-real-token",
        "HANDSFREEPC_TEST_PASSWORD": "not-a-real-password",
        "NODE_OPTIONS": "--require=fake-hook.js",
        "PYTHONPATH": "fake-python-path",
        "GIT_ASKPASS": "fake-askpass.exe",
        "SSH_AUTH_SOCK": "fake-agent.sock",
        "GITHUB_PAT": "not-a-real-pat",
        "AWS_ACCESS_KEY_ID": "not-a-real-key-id",
    }
    return PersistentMcpClient(
        sys.executable,
        ("-u", "-c", _MCP_SERVER),
        timeout_seconds=timeout_seconds,
        environment=source_environment,
    )


def test_environment_filter_uses_minimal_windows_allowlist_by_name_only():
    filtered = _sanitized_environment(
        {
            "PATH": "safe-path",
            "PATHEXT": ".EXE;.CMD",
            "SystemRoot": "safe-system-root",
            "WINDIR": "safe-windows-directory",
            "TEMP": "safe-temp",
            "TMP": "safe-tmp",
            "COMSPEC": "safe-command-processor",
            "USERPROFILE": "safe-profile",
            "APPDATA": "safe-app-data",
            "LOCALAPPDATA": "safe-local-app-data",
            "LANG": "zh_CN.UTF-8",
            "LC_ALL": "C.UTF-8",
            "SAFE": "not-allowlisted",
            "CODEX_HOME": "fake-codex-home",
            "NODE_OPTIONS": "--require=fake-hook.js",
            "PYTHONPATH": "fake-python-path",
            "GIT_ASKPASS": "fake-askpass.exe",
            "SSH_AUTH_SOCK": "fake-agent.sock",
            "GITHUB_PAT": "not-a-real-pat",
            "AWS_SECRET_ACCESS_KEY": "not-a-real-secret",
        }
    )

    assert set(filtered) == {
        "PATH",
        "PATHEXT",
        "SystemRoot",
        "WINDIR",
        "TEMP",
        "TMP",
        "COMSPEC",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "LANG",
        "LC_ALL",
    }


def test_client_initializes_once_and_reuses_one_long_lived_process():
    client = _client()
    try:
        client.start()
        first = client.request("probe")
        second = client.request("probe")

        assert client.running
        assert first["pid"] == second["pid"]
        assert first["initialize_count"] == second["initialize_count"] == 1
        assert first["initialized_notification"] is True
        environment_names = set(first["environment_names"])
        assert {
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "COMSPEC",
            "LANG",
        } <= environment_names
        assert {
            "HANDSFREEPC_TEST_SAFE",
            "HANDSFREEPC_TEST_API_KEY",
            "HANDSFREEPC_TEST_TOKEN",
            "HANDSFREEPC_TEST_PASSWORD",
            "NODE_OPTIONS",
            "PYTHONPATH",
            "GIT_ASKPASS",
            "SSH_AUTH_SOCK",
            "GITHUB_PAT",
            "AWS_ACCESS_KEY_ID",
        }.isdisjoint(environment_names)
    finally:
        client.close()


def test_request_timeout_stops_the_persistent_server():
    client = _client(timeout_seconds=0.4)
    try:
        client.start()

        with pytest.raises(McpClientError, match="timed out: never"):
            client.request("never")

        assert not client.running
    finally:
        client.close()


def test_cancel_event_stops_an_in_flight_request_and_process():
    client = _client()
    cancel_event = threading.Event()
    cancel_event.set()
    try:
        client.start()

        with pytest.raises(McpClientError, match="cancelled"):
            client.request("never", cancel_event=cancel_event)

        assert not client.running
        assert client.cancel() is False
    finally:
        client.close()
