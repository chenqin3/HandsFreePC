from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from handsfree_pc.config import AppProfile
from handsfree_pc.desktop.protocol import DesktopAction, DesktopActionType
from handsfree_pc.desktop.windows_uia import (
    WindowsUiaDriver,
    WindowsUiaDriverError,
    WindowsUiaStaleObservation,
)
from handsfree_pc.windows.native import WindowActivationError, WindowInfo
from handsfree_pc.windows.uia import PasswordFieldError


class FakeElement:
    def __init__(
        self,
        name: str,
        control_type: str,
        *,
        automation_id: str = "",
        value: str | None = None,
        password: bool = False,
        focused: bool = False,
        on_invoke=None,
        invoke_supported: bool = True,
        on_focus_check=None,
        on_visibility_check=None,
    ) -> None:
        self.element_info = SimpleNamespace(
            name=name,
            control_type=control_type,
            automation_id=automation_id,
            is_password=password,
        )
        self._name = name
        self._value = value
        self.is_password = password
        self.focused = focused
        self.on_invoke = on_invoke
        self.on_focus_check = on_focus_check
        self.on_visibility_check = on_visibility_check
        self.focus_checks = 0
        self.visibility_checks = 0
        self.invocations = 0
        self.clicks = 0
        self.values_set: list[str] = []
        if not invoke_supported:
            self.invoke = None

    def window_text(self):
        return self._name

    def is_visible(self):
        self.visibility_checks += 1
        if self.on_visibility_check is not None:
            self.on_visibility_check(self.visibility_checks)
        return True

    def is_enabled(self):
        return True

    def has_keyboard_focus(self):
        self.focus_checks += 1
        if self.on_focus_check is not None:
            self.on_focus_check(self.focus_checks)
        return self.focused

    def get_value(self):
        return self._value

    def invoke(self):
        self.invocations += 1
        if self.on_invoke is not None:
            self.on_invoke()

    def click_input(self, *, button, double):
        assert button == "left"
        assert double is False
        self.clicks += 1

    def set_edit_text(self, value):
        self.values_set.append(value)


class FakeRoot:
    def __init__(self, elements) -> None:
        self.elements = list(elements)
        self.waits: list[tuple[str, int]] = []

    def wait(self, condition, *, timeout):
        self.waits.append((condition, timeout))

    def descendants(self):
        return list(self.elements)


class FakeDesktop:
    def __init__(self, roots) -> None:
        self.roots = roots
        self.handles: list[int] = []

    def window(self, *, handle):
        self.handles.append(handle)
        return self.roots[handle]


class FakeNative:
    def __init__(self, windows) -> None:
        self.windows = list(windows)
        self.foreground = self.windows[0]
        self.calls: list[tuple] = []
        self.text: list[str] = []

    def assert_interactive_desktop(self):
        self.calls.append(("assert_interactive_desktop",))

    def find_windows(self, *, title_patterns, process_names):
        self.calls.append(("find_windows", tuple(title_patterns), tuple(process_names)))
        return list(self.windows)

    def get_foreground_window_info(self):
        self.calls.append(("get_foreground_window_info",))
        return self.foreground

    def activate_window(self, hwnd):
        self.calls.append(("activate_window", hwnd))
        match = next(item for item in self.windows if item.hwnd == hwnd)
        self.foreground = match
        return match

    def assert_foreground(self, hwnd):
        self.calls.append(("assert_foreground", hwnd))
        if self.foreground.hwnd != hwnd:
            raise WindowActivationError("foreground window changed")

    def send_text(self, text):
        self.calls.append(("send_text", text))
        self.text.append(text)

    def send_hotkey(self, key):
        self.calls.append(("send_hotkey", key))


def _profile() -> AppProfile:
    return AppProfile(
        name="Claude",
        process_names=["claude.exe"],
        executable=None,
        title_patterns=["Claude"],
        search_hotkey=None,
        native_voice_hotkey=None,
        voice_button_names=[],
    )


def _window(hwnd: int = 101, title: str = "Claude") -> WindowInfo:
    return WindowInfo(hwnd, title, 5000 + hwnd, "claude.exe")


def _driver(
    *, native: FakeNative, desktop: FakeDesktop, max_elements: int = 500
) -> WindowsUiaDriver:
    return WindowsUiaDriver(
        {"Claude": _profile()},
        native=native,
        desktop_factory=lambda **_kwargs: desktop,
        max_elements=max_elements,
    )


def test_list_apps_and_observe_are_restricted_to_configured_app_and_hwnd():
    window = _window()
    root = FakeRoot([FakeElement("Chat", "TabItem")])
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: root})
    driver = _driver(native=native, desktop=desktop)

    assert json.loads(driver.list_apps()) == [{"app": "claude", "visible_window_count": 1}]
    observation = driver.observe("CLAUDE")

    assert observation.app == "claude"
    assert observation.window_title == "Claude"
    assert desktop.handles == [window.hwnd]
    assert '"process_name": "claude.exe"' in observation.accessibility_text

    with pytest.raises(WindowsUiaDriverError, match="not configured"):
        driver.observe("terminal")


def test_ambiguous_matches_bind_to_the_single_foreground_hwnd():
    first = _window(101, "Claude - First")
    second = _window(202, "Claude - Foreground")
    native = FakeNative([first, second])
    native.foreground = second
    desktop = FakeDesktop(
        {
            first.hwnd: FakeRoot([FakeElement("First", "Button")]),
            second.hwnd: FakeRoot([FakeElement("Foreground", "Button")]),
        }
    )
    driver = _driver(native=native, desktop=desktop)

    observation = driver.observe("claude")

    assert observation.window_title == second.title
    assert desktop.handles == [second.hwnd]


def test_observe_fails_closed_instead_of_returning_a_partial_element_surface():
    window = _window()
    root = FakeRoot(
        [
            FakeElement("Open", "Button"),
            FakeElement("Continue", "Button"),
            FakeElement("Payment method", "Text"),
        ]
    )
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: root})
    driver = _driver(native=native, desktop=desktop, max_elements=2)

    with pytest.raises(WindowsUiaDriverError, match="partial observation"):
        driver.observe("claude")


def test_observe_allows_a_surface_exactly_at_the_element_limit():
    window = _window()
    root = FakeRoot([FakeElement("Open", "Button"), FakeElement("Continue", "Button")])
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: root})
    driver = _driver(native=native, desktop=desktop, max_elements=2)

    observation = driver.observe("claude")

    assert [element.name for element in observation.elements] == ["Open", "Continue"]


@pytest.mark.parametrize(
    "element",
    [
        FakeElement("A" * 500 + "Payment method", "Text"),
        FakeElement("Ordinary", "Edit", value="A" * 4000 + "Payment method"),
    ],
)
def test_observe_fails_closed_instead_of_truncating_long_uia_properties(element):
    window = _window()
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([element])})
    driver = _driver(native=native, desktop=desktop)

    with pytest.raises(WindowsUiaDriverError, match="truncated observation"):
        driver.observe("claude")


def test_action_requires_same_hwnd_generation_foreground_and_fresh_reobserve():
    first_window = _window()
    button = FakeElement("Chat", "Button")
    root = FakeRoot([button])
    native = FakeNative([first_window])
    desktop = FakeDesktop({first_window.hwnd: root})
    driver = _driver(native=native, desktop=desktop)
    first = driver.observe("claude")
    action = DesktopAction(
        type=DesktopActionType.CLICK,
        app="claude",
        generation=first.generation,
        element_index="0",
    )

    receipt = driver.execute(action, first)

    assert receipt.accepted is True
    assert button.invocations == 1
    assert ("activate_window", first_window.hwnd) in native.calls
    assert native.calls.count(("assert_foreground", first_window.hwnd)) == 2
    with pytest.raises(WindowsUiaStaleObservation, match="fresh observation"):
        driver.execute(action, first)

    second = driver.observe("claude")
    assert second.generation == first.generation + 1
    with pytest.raises(WindowsUiaStaleObservation, match="stale observation"):
        driver.execute(action, first)

    replacement = _window(202, "Claude - Replaced")
    native.windows = [replacement]
    native.foreground = replacement
    desktop.roots[replacement.hwnd] = root
    replacement_action = DesktopAction(
        type=DesktopActionType.CLICK,
        app="claude",
        generation=second.generation,
        element_index="0",
    )
    with pytest.raises(WindowsUiaStaleObservation, match="window changed"):
        driver.execute(replacement_action, second)


def test_password_metadata_is_redacted_and_password_elements_are_never_activated():
    window = _window()
    password = FakeElement(
        "Password",
        "Edit",
        value="must-not-enter-observation",
        password=True,
        focused=True,
    )
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([password])})
    driver = _driver(native=native, desktop=desktop)
    observation = driver.observe("claude")

    assert observation.elements[0].password is True
    assert observation.elements[0].value is None
    assert "must-not-enter-observation" not in observation.accessibility_text

    click = DesktopAction(
        type=DesktopActionType.CLICK,
        app="claude",
        generation=observation.generation,
        element_index="0",
    )
    with pytest.raises(PasswordFieldError, match="password"):
        driver.execute(click, observation)
    assert password.invocations == 0


def test_focus_theft_during_action_fails_postcondition_and_invalidates_snapshot():
    window = _window()
    native = FakeNative([window])

    def steal_focus():
        native.foreground = _window(999, "Other app")

    button = FakeElement("Chat", "Button", on_invoke=steal_focus)
    desktop = FakeDesktop({window.hwnd: FakeRoot([button])})
    driver = _driver(native=native, desktop=desktop)
    observation = driver.observe("claude")
    action = DesktopAction(
        type=DesktopActionType.CLICK,
        app="claude",
        generation=observation.generation,
        element_index="0",
    )

    with pytest.raises(WindowActivationError, match="foreground window changed"):
        driver.execute(action, observation)
    with pytest.raises(WindowsUiaStaleObservation, match="stale observation"):
        driver.execute(action, observation)


@pytest.mark.parametrize(
    ("action_type", "action_kwargs", "forbidden_call"),
    [
        (DesktopActionType.TYPE_TEXT, {"text": "hello"}, "send_text"),
        (DesktopActionType.PRESS_KEY, {"key": "TAB"}, "send_hotkey"),
    ],
)
def test_focus_is_rechecked_immediately_before_keyboard_input(
    action_type, action_kwargs, forbidden_call
):
    window = _window()
    native = FakeNative([window])

    def steal_focus(check_number):
        if check_number == 2:
            native.foreground = _window(999, "Other app")

    edit = FakeElement(
        "Prompt",
        "Edit",
        focused=True,
        on_focus_check=steal_focus,
    )
    desktop = FakeDesktop({window.hwnd: FakeRoot([edit])})
    driver = _driver(native=native, desktop=desktop)
    observation = driver.observe("claude")
    action = DesktopAction(
        type=action_type,
        app="claude",
        generation=observation.generation,
        element_index="0",
        **action_kwargs,
    )

    with pytest.raises(WindowActivationError, match="foreground window changed"):
        driver.execute(action, observation)

    assert not any(call[0] == forbidden_call for call in native.calls)


def test_focus_is_rechecked_immediately_before_physical_click_fallback():
    window = _window()
    native = FakeNative([window])

    def steal_focus(check_number):
        if check_number == 2:
            native.foreground = _window(999, "Other app")

    button = FakeElement(
        "Chat",
        "Button",
        invoke_supported=False,
        on_visibility_check=steal_focus,
    )
    desktop = FakeDesktop({window.hwnd: FakeRoot([button])})
    driver = _driver(native=native, desktop=desktop)
    observation = driver.observe("claude")
    action = DesktopAction(
        type=DesktopActionType.CLICK,
        app="claude",
        generation=observation.generation,
        element_index="0",
    )

    with pytest.raises(WindowActivationError, match="foreground window changed"):
        driver.execute(action, observation)

    assert button.clicks == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda element: setattr(element, "_name", "Delete"),
        lambda element: setattr(element.element_info, "control_type", "MenuItem"),
        lambda element: setattr(element.element_info, "automation_id", "destructive-action"),
        lambda element: setattr(element, "_value", "changed state"),
    ],
)
def test_element_identity_is_rebound_immediately_before_activation(mutate):
    window = _window()
    button = FakeElement(
        "Chat",
        "Button",
        automation_id="chat-tab",
        value="original state",
    )
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([button])})
    driver = _driver(native=native, desktop=desktop)
    observation = driver.observe("claude")
    action = DesktopAction(
        type=DesktopActionType.CLICK,
        app="claude",
        generation=observation.generation,
        element_index="0",
    )
    mutate(button)

    with pytest.raises(WindowsUiaStaleObservation, match="identity or state changed"):
        driver.execute(action, observation)

    assert button.invocations == 0
    assert button.clicks == 0


def test_keyboard_input_cannot_use_focus_acquired_after_the_observation():
    window = _window()
    edit = FakeElement("Prompt", "Edit", value="", focused=False)
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([edit])})
    driver = _driver(native=native, desktop=desktop)
    observation = driver.observe("claude")
    edit.focused = True
    action = DesktopAction(
        type=DesktopActionType.TYPE_TEXT,
        app="claude",
        generation=observation.generation,
        element_index="0",
        text="hello",
    )

    with pytest.raises(WindowsUiaStaleObservation, match="identity or state changed"):
        driver.execute(action, observation)

    assert not any(call[0] == "send_text" for call in native.calls)
