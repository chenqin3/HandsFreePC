from __future__ import annotations

from typing import Any

import pytest

from handsfree_pc.desktop.mcp_client import McpClientError
from handsfree_pc.desktop.open_computer_use import (
    DesktopDriverError,
    DriverActionOutcomeUnknown,
    PersistentOpenComputerUseDriver,
    StaleObservationError,
)
from handsfree_pc.desktop.protocol import DesktopAction, DesktopActionType

_TOOLS = {
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


def _content(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


class FakeMcpClient:
    def __init__(self) -> None:
        self.running = False
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, dict[str, Any] | Exception] = {
            "list_apps": _content("Claude\nCodex"),
            "get_app_state": _content(
                'Window: "Claude", App: Claude.\n0 name="Chat" control_type="TabItem"'
            ),
            "click": _content("Click accepted"),
            "perform_secondary_action": _content("Secondary action accepted"),
            "scroll": _content("Scroll accepted"),
            "drag": _content("Drag accepted"),
            "type_text": _content("Text accepted"),
            "press_key": _content("Key accepted"),
            "set_value": _content("Value accepted"),
        }

    def start(self) -> None:
        self.running = True

    def list_tools(self) -> list[dict[str, str]]:
        return [{"name": name} for name in sorted(_TOOLS)]

    def call_tool(self, name, arguments, *, cancel_event=None):
        self.calls.append((name, arguments))
        value = self.responses[name]
        if isinstance(value, Exception):
            raise value
        return value

    def cancel(self) -> bool:
        was_running = self.running
        self.running = False
        return was_running

    def close(self) -> None:
        self.closed = True
        self.running = False


def _driver(fake: FakeMcpClient) -> PersistentOpenComputerUseDriver:
    return PersistentOpenComputerUseDriver(client_factory=lambda: fake)


def _click(*, generation: int) -> DesktopAction:
    return DesktopAction(
        type=DesktopActionType.CLICK,
        app="Claude",
        generation=generation,
        element_index="0",
    )


def test_observation_generations_force_reobserve_after_every_action():
    client = FakeMcpClient()
    driver = _driver(client)

    first = driver.observe("Claude")
    receipt = driver.execute(_click(generation=first.generation), first)

    assert first.generation == 1
    assert first.window_title == "Claude"
    assert receipt.accepted is True
    assert client.calls[-1] == (
        "click",
        {"app": "Claude", "element_index": "0"},
    )

    with pytest.raises(StaleObservationError, match="fresh observation"):
        driver.execute(_click(generation=first.generation), first)

    second = driver.observe("Claude")
    assert second.generation == 2
    with pytest.raises(StaleObservationError, match="stale observation"):
        driver.execute(_click(generation=first.generation), first)

    driver.execute(_click(generation=second.generation), second)


def test_damaged_unicode_is_rejected_before_it_can_become_planner_context():
    client = FakeMcpClient()
    client.responses["get_app_state"] = _content('Window: "Claude", App: Claude.\nbad�text')
    driver = _driver(client)

    with pytest.raises(DesktopDriverError, match="damaged Unicode"):
        driver.observe("Claude")


def test_transport_failure_after_mutation_is_reported_as_unknown_outcome():
    client = FakeMcpClient()
    driver = _driver(client)
    observation = driver.observe("Claude")
    client.responses["click"] = McpClientError("pipe closed after write")

    with pytest.raises(DriverActionOutcomeUnknown, match="pipe closed"):
        driver.execute(_click(generation=observation.generation), observation)

    with pytest.raises(StaleObservationError, match="stale observation"):
        driver.execute(_click(generation=observation.generation), observation)


def test_lossy_edge_whitespace_and_coordinate_clicks_fail_closed():
    client = FakeMcpClient()
    driver = _driver(client)
    observation = driver.observe("Claude")
    whitespace_action = DesktopAction(
        type=DesktopActionType.TYPE_TEXT,
        app="Claude",
        generation=observation.generation,
        element_index="0",
        text=" keep this space ",
    )

    with pytest.raises(DesktopDriverError, match="trims edge whitespace"):
        driver.execute(whitespace_action, observation)

    observation = driver.observe("Claude")
    coordinate_action = DesktopAction(
        type=DesktopActionType.CLICK,
        app="Claude",
        generation=observation.generation,
        x=10,
        y=20,
    )
    with pytest.raises(DesktopDriverError, match="coordinate actions are disabled"):
        driver.execute(coordinate_action, observation)
