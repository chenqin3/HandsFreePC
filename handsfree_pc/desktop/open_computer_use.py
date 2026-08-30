from __future__ import annotations

import base64
import json
import re
import threading
from collections.abc import Callable, Sequence
from typing import Any

from .mcp_client import McpClientError, PersistentMcpClient
from .protocol import ActionReceipt, DesktopAction, DesktopActionType, DesktopObservation


class DesktopDriverError(RuntimeError):
    pass


class StaleObservationError(DesktopDriverError):
    pass


class DriverActionOutcomeUnknown(DesktopDriverError):
    pass


_REQUIRED_TOOLS = {
    "list_apps",
    "get_app_state",
    "click",
    "perform_secondary_action",
    "scroll",
    "drag",
    "type_text",
    "press_key",
    "set_value",
}
_WINDOW_LINE = re.compile(r'^Window:\s+"(?P<title>.*)",\s+App:\s+.+\.$')


def _tool_content(result: dict[str, Any]) -> tuple[str, bytes | None]:
    if result.get("isError") is True:
        content = result.get("content")
        message = "Open Computer Use returned an error"
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    message = str(item.get("text") or message)
                    break
        raise DesktopDriverError(message[:500])
    content = result.get("content")
    if not isinstance(content, list):
        raise DesktopDriverError("Open Computer Use returned no content list")
    text_blocks: list[str] = []
    image: bytes | None = None
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            text_blocks.append(item["text"])
        elif item.get("type") == "image" and item.get("mimeType") == "image/png":
            data = item.get("data")
            if not isinstance(data, str):
                raise DesktopDriverError("Open Computer Use returned invalid image data")
            try:
                image = base64.b64decode(data, validate=True)
            except ValueError as exc:
                raise DesktopDriverError("Open Computer Use returned invalid base64 image") from exc
    text = "\n".join(text_blocks).strip()
    if not text:
        raise DesktopDriverError("Open Computer Use returned no accessibility text")
    if "\ufffd" in text:
        raise DesktopDriverError(
            "Open Computer Use returned damaged Unicode text; its Windows UTF-8 bridge is not ready"
        )
    return text, image


class PersistentOpenComputerUseDriver:
    """Experimental long-lived adapter for @qwen-code/open-computer-use 0.2.3."""

    def __init__(
        self,
        *,
        executable: str = "open-computer-use",
        args: Sequence[str] = ("mcp",),
        timeout_seconds: float = 35.0,
        allow_coordinate_actions: bool = False,
        allowed_apps: Sequence[str] = ("codex", "claude"),
        client_factory: Callable[[], PersistentMcpClient] | None = None,
    ) -> None:
        self.executable = executable
        self.args = tuple(args)
        self.timeout_seconds = float(timeout_seconds)
        self.allow_coordinate_actions = allow_coordinate_actions
        self.allowed_apps = frozenset(
            item.strip().casefold() for item in allowed_apps if item.strip()
        )
        if not self.allowed_apps:
            raise ValueError("Open Computer Use requires at least one locally configured app")
        self._client_factory = client_factory
        self._client: PersistentMcpClient | None = None
        self._generation = 0
        self._latest: dict[str, DesktopObservation] = {}
        self._action_pending_observation: set[str] = set()
        self._lock = threading.RLock()

    def _build_client(self) -> PersistentMcpClient:
        if self._client_factory is not None:
            return self._client_factory()
        return PersistentMcpClient(
            self.executable,
            self.args,
            timeout_seconds=self.timeout_seconds,
        )

    def _require_client(self) -> PersistentMcpClient:
        if self._client is None:
            self.start()
        assert self._client is not None
        return self._client

    def start(self) -> None:
        with self._lock:
            if self._client is not None and self._client.running:
                return
            client = self._build_client()
            client.start()
            tools = client.list_tools()
            names = {str(item.get("name")) for item in tools}
            missing = _REQUIRED_TOOLS - names
            if missing:
                client.close()
                raise DesktopDriverError(
                    f"Open Computer Use is missing required tools: {sorted(missing)}"
                )
            self._client = client
            self._latest.clear()
            self._action_pending_observation.clear()

    @staticmethod
    def _normalized_app(app: str) -> str:
        value = app.strip().casefold()
        if not value:
            raise ValueError("app must be non-empty")
        return value

    def list_apps(self, *, cancel_event: threading.Event | None = None) -> str:
        result = self._require_client().call_tool("list_apps", {}, cancel_event=cancel_event)
        text, _image = _tool_content(result)
        # Never forward the MCP server's free-form list text to a cloud planner.
        # Only exact, locally configured identifiers survive this boundary.
        visible = []
        seen: set[str] = set()
        for line in text.splitlines():
            candidate = line.strip().casefold()
            if candidate in self.allowed_apps and candidate not in seen:
                visible.append({"app": candidate, "visible_window_count": 1})
                seen.add(candidate)
        return json.dumps(visible, ensure_ascii=False, sort_keys=True)

    def observe(
        self, app: str, *, cancel_event: threading.Event | None = None
    ) -> DesktopObservation:
        normalized = self._normalized_app(app)
        if normalized not in self.allowed_apps:
            raise DesktopDriverError(
                "application is outside the local Open Computer Use allow-list"
            )
        result = self._require_client().call_tool(
            "get_app_state", {"app": app}, cancel_event=cancel_event
        )
        text, image = _tool_content(result)
        title: str | None = None
        for line in text.splitlines()[:4]:
            if match := _WINDOW_LINE.match(line.strip()):
                title = match.group("title")
                break
        with self._lock:
            self._generation += 1
            observation = DesktopObservation(
                app=app,
                generation=self._generation,
                accessibility_text=text,
                screenshot_png=image,
                window_title=title,
            )
            self._latest[normalized] = observation
            self._action_pending_observation.discard(normalized)
            return observation

    def execute(
        self,
        action: DesktopAction,
        before: DesktopObservation,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ActionReceipt:
        normalized = self._normalized_app(action.app)
        with self._lock:
            latest = self._latest.get(normalized)
            if latest is None or latest.generation != before.generation:
                raise StaleObservationError("desktop action used a stale observation")
            if (
                action.generation != before.generation
                or action.app.casefold() != before.app.casefold()
            ):
                raise StaleObservationError("desktop action does not match its observation")
            if normalized in self._action_pending_observation:
                raise StaleObservationError("a fresh observation is required after every action")
            if (
                action.type in {DesktopActionType.CLICK, DesktopActionType.DRAG}
                and action.element_index is None
                and not self.allow_coordinate_actions
            ):
                raise DesktopDriverError("coordinate actions are disabled in the 0.3 driver")
            if action.type in {DesktopActionType.TYPE_TEXT, DesktopActionType.SET_VALUE}:
                payload = (
                    action.text if action.type == DesktopActionType.TYPE_TEXT else action.value
                )
                if payload is not None and payload != payload.strip():
                    raise DesktopDriverError(
                        "Open Computer Use 0.2.3 trims edge whitespace; refusing lossy text input"
                    )
            self._action_pending_observation.add(normalized)
        try:
            result = self._require_client().call_tool(
                action.type.value,
                action.tool_arguments(),
                cancel_event=cancel_event,
            )
            _tool_content(result)
        except McpClientError as exc:
            # A timeout or broken pipe after a mutating call has an unknown UI outcome.
            self._latest.pop(normalized, None)
            raise DriverActionOutcomeUnknown(str(exc)) from exc
        except Exception:
            self._latest.pop(normalized, None)
            raise
        return ActionReceipt(
            action=action,
            accepted=True,
            before_generation=before.generation,
            driver_message="Open Computer Use accepted one atomic action",
        )

    def cancel(self) -> bool:
        client = self._client
        return client.cancel() if client is not None else False

    def close(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
            self._latest.clear()
            self._action_pending_observation.clear()
        if client is not None:
            client.close()
