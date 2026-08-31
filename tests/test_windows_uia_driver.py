from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from handsfree_pc.app_doctor import AppDoctorFailure, safe_observation_report, select_composer
from handsfree_pc.config import AppProfile
from handsfree_pc.desktop.protocol import (
    DesktopAction,
    DesktopActionType,
    DesktopExpectation,
    DesktopExpectationKind,
    ElementPlane,
    element_plane,
)
from handsfree_pc.desktop.safety import DesktopSafetyDisposition, DesktopSafetyPolicy
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
        runtime_id=None,
        aria_properties: str = "",
        aria_role: str | None = None,
        labeled_by_name: str | None = None,
        read_only: bool | None = None,
        value: str | None = None,
        password: bool = False,
        focused: bool = False,
        selected: bool | None = None,
        on_invoke=None,
        on_click=None,
        invoke_supported: bool = True,
        on_focus_check=None,
        on_visibility_check=None,
    ) -> None:
        labeled_by = (
            SimpleNamespace(CurrentName=labeled_by_name) if labeled_by_name is not None else None
        )
        self.element_info = SimpleNamespace(
            name=name,
            control_type=control_type,
            automation_id=automation_id,
            runtime_id=runtime_id,
            element=SimpleNamespace(
                CurrentAriaProperties=aria_properties,
                CurrentAriaRole=(
                    aria_role
                    if aria_role is not None
                    else "button"
                    if control_type == "Button"
                    else ""
                ),
                CurrentLabeledBy=labeled_by,
            ),
            is_password=password,
        )
        self._name = name
        self._value = value
        self.is_password = password
        self.focused = focused
        self.selected = selected
        self.read_only = read_only
        self.on_invoke = on_invoke
        self.on_click = on_click
        self.on_focus_check = on_focus_check
        self.on_visibility_check = on_visibility_check
        self.focus_checks = 0
        self.visibility_checks = 0
        self.value_reads = 0
        self.invocations = 0
        self.clicks = 0
        self.values_set: list[str] = []
        if not invoke_supported:
            self.invoke = None

    @property
    def aria_properties(self):
        return self.element_info.element.CurrentAriaProperties

    @aria_properties.setter
    def aria_properties(self, value):
        self.element_info.element.CurrentAriaProperties = value

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
        self.value_reads += 1
        return self._value

    def is_read_only(self):
        return self.read_only

    def is_selected(self):
        return self.selected

    def invoke(self):
        self.invocations += 1
        if self.on_invoke is not None:
            self.on_invoke()

    def click_input(self, *, button, double):
        assert button == "left"
        assert double is False
        self.clicks += 1
        if self.on_click is not None:
            self.on_click()

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


class FakeRawInfo:
    def __init__(self, wrapper=None, *, name="", control_type=None) -> None:
        self.wrapper = wrapper
        self.name = name
        self.control_type = control_type
        self.automation_id = None


class FakeRawTree:
    def __init__(self, elements) -> None:
        self.elements = list(elements)

    def descendants(self):
        return list(self.elements)


class FakeRawBackend:
    @staticmethod
    def generic_wrapper_class(element_info):
        if element_info.wrapper is None:
            raise KeyError(element_info.control_type)
        return element_info.wrapper


class FakeRawRoot(FakeRoot):
    def __init__(self, elements) -> None:
        super().__init__([])
        self.element_info = FakeRawTree(elements)
        self.backend = FakeRawBackend()


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

    def enumerate_windows(self):
        self.calls.append(("enumerate_windows",))
        return list(self.windows)

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


def _profile(**overrides) -> AppProfile:
    values = {
        "name": "Claude",
        "process_names": ["claude.exe"],
        "executable": None,
        "title_patterns": ["Claude"],
        "search_hotkey": None,
        "native_voice_hotkey": None,
        "voice_button_names": [],
    }
    values.update(overrides)
    return AppProfile(**values)


def _window(hwnd: int = 101, title: str = "Claude") -> WindowInfo:
    return WindowInfo(hwnd, title, 5000 + hwnd, "claude.exe")


def _driver(
    *,
    native: FakeNative,
    desktop: FakeDesktop,
    max_elements: int = 500,
    profile: AppProfile | None = None,
) -> WindowsUiaDriver:
    return WindowsUiaDriver(
        {"Claude": profile or _profile()},
        native=native,
        desktop_factory=lambda **_kwargs: desktop,
        max_elements=max_elements,
    )


def _bounded_chat_profile(**overrides) -> AppProfile:
    values = {
        "include_control_types": ["Button", "TabItem", "Edit"],
        "content_control_types": ["Text", "Document", "Pane", "Group"],
        "drop_long_content": True,
        "max_control_name_chars": 100,
        "max_content_chars": 80,
        "max_content_nodes": 10,
        "composer_names": ["Prompt", "Message"],
    }
    values.update(overrides)
    return _profile(**values)


@pytest.mark.parametrize(
    ("fixture_name", "expected_control", "composer_name"),
    [
        ("claude_uia_snapshot.json", "Chat and Cowork", "Prompt"),
        ("codex_uia_snapshot.json", "Projects", "随心输入"),
    ],
)
def test_deidentified_snapshots_pass_through_real_driver_bounding_and_planes(
    fixture_name,
    expected_control,
    composer_name,
):
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / fixture_name).read_text(encoding="utf-8")
    )
    window = _window(title=payload["window_title"])
    wrappers = [FakeElement(**item) for item in payload["uia_elements"]]
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot(wrappers)})
    profile = _bounded_chat_profile(composer_names=[composer_name])
    driver = WindowsUiaDriver(
        {payload["app"]: profile},
        native=native,
        desktop_factory=lambda **_kwargs: desktop,
        max_elements=20,
    )

    observation = driver.observe(payload["app"])
    stats = json.loads(observation.accessibility_text.splitlines()[0])["uia_stats"]
    policy = DesktopSafetyPolicy("personal_trusted")

    assert expected_control in {element.name for element in observation.elements}
    assert composer_name in {element.name for element in observation.elements}
    assert len(observation.elements) == 4
    assert all(
        element_plane(element) in {ElementPlane.CONTROL, ElementPlane.INPUT}
        for element in observation.elements
    )
    assert max(len(item["name"]) for item in payload["uia_elements"]) > 500
    assert stats["total"] == 5
    assert stats["kept"] == 4
    assert stats["skipped_long_content"] == 1
    assert observation.high_credential_count == 5
    assert observation.credential_affected_element_count == 1
    assert policy.inspect_observation(observation).disposition == DesktopSafetyDisposition.ALLOW
    planner_text = json.dumps(
        policy.planner_observation(
            observation,
            user_text=f"In {payload['app']}, click {expected_control}",
        ).planner_context(max_chars=8000),
        ensure_ascii=False,
    )
    assert "sk-proj-" not in planner_text
    assert "github_pat_" not in planner_text


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


class _FakePngImage:
    def save(self, stream, *, format):
        assert format == "PNG"
        stream.write(b"\x89PNG\r\n\x1a\nfixture")


class _ScreenshotRoot(FakeRoot):
    def capture_as_image(self):
        return _FakePngImage()


def test_unrestricted_inventory_exposes_each_top_level_window_as_a_unique_target():
    first = WindowInfo(101, "First tab - Chrome", 5101, "chrome.exe")
    second = WindowInfo(202, "Second tab - Chrome", 5202, "chrome.exe")
    explorer = WindowInfo(303, "Downloads", 5303, "explorer.exe")
    native = FakeNative([first, second, explorer])
    desktop = FakeDesktop(
        {
            first.hwnd: _ScreenshotRoot([FakeElement("Address", "Edit")]),
            second.hwnd: _ScreenshotRoot([FakeElement("Second", "Button")]),
            explorer.hwnd: _ScreenshotRoot([FakeElement("Downloads", "TreeItem")]),
        }
    )
    driver = WindowsUiaDriver(
        {"claude": _profile()},
        native=native,
        desktop_factory=lambda **_kwargs: desktop,
        discover_all_windows=True,
        activate_on_observe=True,
        capture_screenshots=True,
    )

    inventory = json.loads(driver.list_apps())
    app_ids = [item["app"] for item in inventory]
    chrome_entries = [item for item in inventory if item["process_name"] == "chrome.exe"]

    assert len(inventory) == 3
    assert len(app_ids) == len(set(app_ids))
    assert len(chrome_entries) == 2
    assert all(item["visible_window_count"] == 1 for item in inventory)
    assert {item["window_title"] for item in chrome_entries} == {
        first.title,
        second.title,
    }

    second_app = next(
        item["app"] for item in chrome_entries if item["window_title"] == second.title
    )
    observation = driver.observe(second_app)

    assert observation.app == second_app
    assert observation.local_window_id == f"hwnd:{second.hwnd}"
    assert observation.window_title == second.title
    assert observation.screenshot_png == b"\x89PNG\r\n\x1a\nfixture"
    assert desktop.handles == [second.hwnd]
    assert ("activate_window", second.hwnd) in native.calls
    assert ("assert_foreground", second.hwnd) in native.calls


@pytest.mark.parametrize(
    "replacement",
    [
        WindowInfo(101, "Different title", 5101, "chrome.exe"),
        WindowInfo(101, "First tab - Chrome", 9999, "chrome.exe"),
        WindowInfo(101, "First tab - Chrome", 5101, "other.exe"),
    ],
)
def test_unrestricted_observe_rejects_stale_window_rebinding(replacement):
    original = WindowInfo(101, "First tab - Chrome", 5101, "chrome.exe")
    native = FakeNative([original])
    driver = WindowsUiaDriver(
        {},
        native=native,
        desktop_factory=lambda **_kwargs: FakeDesktop(
            {replacement.hwnd: FakeRoot([FakeElement("Open", "Button")])}
        ),
        discover_all_windows=True,
    )
    app_id = json.loads(driver.list_apps())[0]["app"]
    native.windows = [replacement]

    with pytest.raises(WindowsUiaStaleObservation, match="identity changed"):
        driver.observe(app_id)


def test_unrestricted_observe_rejects_a_window_that_disappeared_after_inventory():
    window = WindowInfo(101, "First tab - Chrome", 5101, "chrome.exe")
    native = FakeNative([window])
    driver = WindowsUiaDriver(
        {},
        native=native,
        desktop_factory=lambda **_kwargs: FakeDesktop({}),
        discover_all_windows=True,
    )
    app_id = json.loads(driver.list_apps())[0]["app"]
    native.windows = []

    with pytest.raises(WindowsUiaStaleObservation, match="no longer visible"):
        driver.observe(app_id)


def test_unrestricted_binding_accepts_title_transition_only_after_one_bound_action():
    original = WindowInfo(101, "Before - Chrome", 5101, "chrome.exe")
    changed = WindowInfo(101, "After - Chrome", 5101, "chrome.exe")
    native = FakeNative([original])

    def change_title():
        native.windows = [changed]

    button = FakeElement("Open", "Button", on_invoke=change_title)
    desktop = FakeDesktop({original.hwnd: FakeRoot([button])})
    driver = WindowsUiaDriver(
        {},
        native=native,
        desktop_factory=lambda **_kwargs: desktop,
        discover_all_windows=True,
        activate_on_observe=True,
    )
    app_id = json.loads(driver.list_apps())[0]["app"]
    before = driver.observe(app_id)
    action = DesktopAction(
        DesktopActionType.CLICK,
        app=app_id,
        generation=before.generation,
        element_index=before.elements[0].index,
        mouse_button="left",
        click_count=1,
    )

    driver.execute(action, before)
    after = driver.observe(app_id)

    assert after.window_title == changed.title
    assert after.local_window_id == f"hwnd:{changed.hwnd}"


def test_unrestricted_inventory_refresh_preserves_an_unchanged_window_snapshot():
    window = WindowInfo(101, "Stable - Chrome", 5101, "chrome.exe")
    native = FakeNative([window])
    button = FakeElement("Open", "Button")
    desktop = FakeDesktop({window.hwnd: FakeRoot([button])})
    driver = WindowsUiaDriver(
        {},
        native=native,
        desktop_factory=lambda **_kwargs: desktop,
        discover_all_windows=True,
        activate_on_observe=True,
    )
    app_id = json.loads(driver.list_apps())[0]["app"]
    before = driver.observe(app_id)
    action = DesktopAction(
        DesktopActionType.CLICK,
        app=app_id,
        generation=before.generation,
        element_index=before.elements[0].index,
        mouse_button="left",
        click_count=1,
    )

    driver.list_apps()
    receipt = driver.execute(action, before)

    assert receipt.accepted is True


def test_observe_omits_one_transient_unwrappable_descendant_without_losing_surface():
    window = _window()
    open_button = FakeElement("Open", "Button")
    root = FakeRawRoot(
        [
            FakeRawInfo(open_button, name="Open", control_type="Button"),
            FakeRawInfo(name="transient private node", control_type=None),
        ]
    )
    native = FakeNative([window])
    driver = _driver(
        native=native,
        desktop=FakeDesktop({window.hwnd: root}),
    )

    observation = driver.observe("claude")
    stats = json.loads(observation.accessibility_text.splitlines()[0])["uia_stats"]

    assert [element.name for element in observation.elements] == ["Open"]
    assert observation.total_element_count == 2
    assert observation.property_error_count == 1
    assert observation.elements_truncated is True
    assert stats["omitted_unwrappable_descendant"] == 1
    assert "transient private node" not in observation.accessibility_text


@pytest.mark.parametrize("broken_state", ["visible", "enabled"])
def test_observe_omits_controls_with_unreadable_critical_state(broken_state):
    class BrokenStateElement(FakeElement):
        def is_visible(self):
            if broken_state == "visible":
                raise RuntimeError("visibility unavailable")
            return super().is_visible()

        def is_enabled(self):
            if broken_state == "enabled":
                raise RuntimeError("enabled state unavailable")
            return super().is_enabled()

    window = _window()
    native = FakeNative([window])
    driver = _driver(
        native=native,
        desktop=FakeDesktop({window.hwnd: FakeRoot([BrokenStateElement("Chat", "Button")])}),
    )

    observation = driver.observe("claude")
    report = safe_observation_report(
        observation,
        policy=DesktopSafetyPolicy("personal_trusted"),
    )
    stats = json.loads(observation.accessibility_text.splitlines()[0])["uia_stats"]

    assert observation.elements == ()
    assert observation.property_error_count == 1
    stat_name = "visibility" if broken_state == "visible" else "enabled"
    assert stats[f"omitted_{stat_name}_property_error"] == 1
    assert report["observe_succeeded"] is False


def test_dedicated_focus_fallback_clicks_composer_without_invoking_it():
    window = _window()
    side_effect = {"invoked": False}
    composer = None

    def mark_invoked():
        side_effect["invoked"] = True

    def mark_focused():
        assert composer is not None
        composer.focused = True

    composer = FakeElement(
        "Prompt",
        "Edit",
        value="",
        focused=False,
        on_invoke=mark_invoked,
        on_click=mark_focused,
    )
    native = FakeNative([window])
    driver = _driver(
        native=native,
        desktop=FakeDesktop({window.hwnd: FakeRoot([composer])}),
        profile=_bounded_chat_profile(composer_names=["Prompt"]),
    )
    observation = driver.observe("claude")
    target = observation.elements[0]
    action = DesktopAction(
        DesktopActionType.PERFORM_SECONDARY_ACTION,
        app=observation.app,
        generation=observation.generation,
        element_index=target.index,
        action_name="clickfocus",
    )

    receipt = driver.execute(action, observation)

    assert receipt.accepted is True
    assert composer.focused is True
    assert composer.clicks == 1
    assert composer.invocations == 0
    assert side_effect["invoked"] is False


def test_generic_click_on_input_plane_uses_physical_focus_not_invoke():
    window = _window()
    side_effect = {"invoked": False}

    def mark_invoked():
        side_effect["invoked"] = True

    composer = FakeElement(
        "Prompt",
        "Edit",
        value="",
        focused=False,
        on_invoke=mark_invoked,
    )
    native = FakeNative([window])
    driver = _driver(
        native=native,
        desktop=FakeDesktop({window.hwnd: FakeRoot([composer])}),
        profile=_bounded_chat_profile(composer_names=["Prompt"]),
    )
    observation = driver.observe("claude")
    action = DesktopAction(
        DesktopActionType.CLICK,
        app=observation.app,
        generation=observation.generation,
        element_index=observation.elements[0].index,
    )

    receipt = driver.execute(action, observation)

    assert receipt.accepted is True
    assert composer.clicks == 1
    assert composer.invocations == 0
    assert side_effect["invoked"] is False


def test_app_doctor_cleanup_clears_only_the_exact_bound_random_draft():
    class ClearingEdit(FakeElement):
        def set_edit_text(self, value):
            super().set_edit_text(value)
            self._value = value

    token = "HandsFreePC-DRAFT-0123456789-中文"
    window = _window()
    composer = ClearingEdit("Prompt", "Edit", value=token, focused=True)
    native = FakeNative([window])
    driver = _driver(
        native=native,
        desktop=FakeDesktop({window.hwnd: FakeRoot([composer])}),
        profile=_bounded_chat_profile(composer_names=["Prompt"]),
    )
    before = driver.observe("claude")
    target = before.elements[0]

    method = driver.clear_app_doctor_draft(before, target, expected_text=token)
    after = driver.observe("claude")

    assert method == "set_edit_text"
    assert composer.values_set == [""]
    assert after.elements[0].value in {None, ""}


def test_app_doctor_cleanup_rejects_non_diagnostic_text_before_mutation():
    window = _window()
    composer = FakeElement("Prompt", "Edit", value="user draft", focused=True)
    driver = _driver(
        native=FakeNative([window]),
        desktop=FakeDesktop({window.hwnd: FakeRoot([composer])}),
        profile=_bounded_chat_profile(composer_names=["Prompt"]),
    )
    before = driver.observe("claude")

    with pytest.raises(WindowsUiaDriverError, match="not an app-doctor draft"):
        driver.clear_app_doctor_draft(
            before,
            before.elements[0],
            expected_text="user draft",
        )

    assert composer.values_set == []


@pytest.mark.parametrize(
    ("container_type", "aria_role", "dialog_label", "button_label", "inspection", "action"),
    [
        ("Group", "dialog", "Payment details", "Continue", "block", "block"),
        ("Pane", "alertdialog", "Sign in to continue", "OK", "block", "block"),
        ("Group", "dialog", "Windows Security", "Continue", "block", "block"),
        (
            "Pane",
            "alertdialog",
            "Delete all conversations?",
            "Continue",
            "allow",
            "confirm",
        ),
    ],
)
def test_aria_dialog_containers_are_retained_as_authoritative_safety_context(
    container_type,
    aria_role,
    dialog_label,
    button_label,
    inspection,
    action,
):
    window = _window()
    native = FakeNative([window])
    driver = _driver(
        native=native,
        desktop=FakeDesktop(
            {
                window.hwnd: FakeRoot(
                    [
                        FakeElement(dialog_label, container_type, aria_role=aria_role),
                        FakeElement(button_label, "Button"),
                    ]
                )
            }
        ),
        profile=_bounded_chat_profile(),
    )
    observation = driver.observe("claude")
    dialog = next(element for element in observation.elements if element.name == dialog_label)
    button = next(element for element in observation.elements if element.name == button_label)
    policy = DesktopSafetyPolicy("personal_trusted")

    assert element_plane(dialog) == ElementPlane.DIALOG
    assert dialog.addressable is False
    assert policy.inspect_observation(observation).disposition.value == inspection
    result = policy.evaluate(
        DesktopAction(
            DesktopActionType.CLICK,
            app=observation.app,
            generation=observation.generation,
            element_index=button.index,
        ),
        observation,
        user_text=f"In Claude, click {button_label} and show Home",
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            text="Home",
        ),
    )
    assert result.disposition.value == action


@pytest.mark.parametrize(
    "hidden_semantics",
    [
        "Payment details",
        "Sign in to continue",
        "Delete all conversations?",
        "Windows Security",
    ],
)
def test_truncated_aria_dialog_identity_blocks_before_hidden_semantics_are_lost(
    hidden_semantics,
):
    window = _window()
    long_name = ("A" * 360) + hidden_semantics + ("B" * 360)
    native = FakeNative([window])
    driver = _driver(
        native=native,
        desktop=FakeDesktop(
            {
                window.hwnd: FakeRoot(
                    [
                        FakeElement(long_name, "Group", aria_role="dialog"),
                        FakeElement("Continue", "Button"),
                    ]
                )
            }
        ),
        profile=_bounded_chat_profile(max_control_name_chars=100),
    )

    observation = driver.observe("claude")
    dialog = next(
        element for element in observation.elements if element_plane(element) == ElementPlane.DIALOG
    )
    result = DesktopSafetyPolicy("personal_trusted").inspect_observation(observation)

    assert dialog.name_metadata is not None
    assert dialog.name_metadata.truncated is True
    assert result.disposition == DesktopSafetyDisposition.BLOCK


def test_setfocus_partial_success_never_falls_through_to_a_physical_click():
    class PartialSetFocusElement(FakeElement):
        def set_focus(self):
            self.focused = True
            raise RuntimeError("UIA reported failure after focus changed")

    window = _window()
    composer = PartialSetFocusElement("Prompt", "Edit", value="", focused=False)
    native = FakeNative([window])
    driver = _driver(
        native=native,
        desktop=FakeDesktop({window.hwnd: FakeRoot([composer])}),
        profile=_bounded_chat_profile(composer_names=["Prompt"]),
    )
    observation = driver.observe("claude")
    action = DesktopAction(
        DesktopActionType.PERFORM_SECONDARY_ACTION,
        app=observation.app,
        generation=observation.generation,
        element_index=observation.elements[0].index,
        action_name="setfocus",
    )

    with pytest.raises(RuntimeError, match="after focus changed"):
        driver.execute(action, observation)

    assert composer.focused is True
    assert composer.clicks == 0
    assert composer.invocations == 0


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


def test_observe_prioritizes_actionable_controls_instead_of_failing_on_a_large_surface():
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

    observation = driver.observe("claude")
    stats = json.loads(observation.accessibility_text.splitlines()[0])["uia_stats"]

    assert [element.name for element in observation.elements] == ["Open", "Continue"]
    assert stats["interesting"] == 3
    assert stats["retained_actionable"] == 2
    assert stats["omitted_by_max_elements"] == 1
    assert stats["bounded_surface_items"] == 1
    assert len(stats["bounded_surface_sha256"]) == 64


def test_observe_allows_a_surface_exactly_at_the_element_limit():
    window = _window()
    root = FakeRoot([FakeElement("Open", "Button"), FakeElement("Continue", "Button")])
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: root})
    driver = _driver(native=native, desktop=desktop, max_elements=2)

    observation = driver.observe("claude")

    assert [element.name for element in observation.elements] == ["Open", "Continue"]


def test_chat_profile_filters_unknown_types_and_keeps_content_non_addressable():
    window = _window()
    button = FakeElement("Open chat", "Button")
    content = FakeElement("ordinary chat body", "Text")
    ignored = FakeElement("decorative image", "Image")
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([button, content, ignored])})
    driver = _driver(
        native=native,
        desktop=desktop,
        profile=_bounded_chat_profile(),
    )

    observation = driver.observe("claude")
    stats = json.loads(observation.accessibility_text.splitlines()[0])["uia_stats"]

    assert [item.name for item in observation.elements] == [
        "Open chat",
        "ordinary chat body",
    ]
    assert stats["total"] == 3
    assert stats["kept"] == 2
    assert stats["skipped"] == 1
    assert stats["omitted_by_profile"] == 1
    assert stats["property_errors"] == 0

    content_action = DesktopAction(
        type=DesktopActionType.CLICK,
        app="claude",
        generation=observation.generation,
        element_index="1",
    )
    with pytest.raises(WindowsUiaStaleObservation, match="not in this observation"):
        driver.execute(content_action, observation)
    assert content.invocations == 0


def test_profiled_focused_document_composer_is_promoted_to_an_input_target():
    window = _window()
    composer = FakeElement(
        "Prompt",
        "Document",
        focused=True,
        value="",
        aria_properties="readonly=false;multiline=true",
        aria_role="textbox",
    )
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([composer])})
    driver = _driver(
        native=native,
        desktop=desktop,
        profile=_bounded_chat_profile(),
    )
    observation = driver.observe("claude")
    action = DesktopAction(
        type=DesktopActionType.TYPE_TEXT,
        app="claude",
        generation=observation.generation,
        element_index="0",
        text="draft only",
    )

    receipt = driver.execute(action, observation)

    assert receipt.accepted
    assert native.text == ["draft only"]


def test_focused_document_without_positive_editability_stays_content_only():
    window = _window()
    document = FakeElement("Prompt", "Document", focused=True, value="")
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([document])})
    driver = _driver(
        native=native,
        desktop=desktop,
        profile=_bounded_chat_profile(),
    )

    observation = driver.observe("claude")
    element = observation.elements[0]

    assert element_plane(element) == ElementPlane.CONTENT
    assert element.editable is False
    assert element.composer is False
    assert element.addressable is False


def test_unfocused_profile_named_editable_document_is_a_verified_composer():
    window = _window()
    document = FakeElement(
        "Prompt",
        "Document",
        focused=False,
        value="",
        runtime_id=(1, 2, 3),
        aria_properties="readonly=false;multiline=true",
        aria_role="textbox",
    )
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([document])})
    driver = _driver(
        native=native,
        desktop=desktop,
        profile=_bounded_chat_profile(),
    )

    observation = driver.observe("claude")
    element = observation.elements[0]

    assert element_plane(element) == ElementPlane.INPUT
    assert element.editable is True
    assert element.composer is True
    assert element.addressable is True
    assert select_composer(observation, hints=("prompt",)).index == element.index


def test_unnamed_focused_document_requires_textbox_multiline_semantics():
    window = _window()
    document = FakeElement(
        "",
        "Document",
        focused=True,
        value="",
        runtime_id=(4, 5, 6),
        aria_properties="readonly=false",
        aria_role="textbox",
    )
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([document])})
    driver = _driver(
        native=native,
        desktop=desktop,
        profile=_bounded_chat_profile(),
    )

    observation = driver.observe("claude")

    assert observation.elements[0].composer is False
    with pytest.raises(AppDoctorFailure, match="composer candidate"):
        select_composer(observation)


def test_unnamed_composer_semantics_are_rebound_before_typing():
    window = _window()
    document = FakeElement(
        "",
        "Document",
        focused=True,
        value="",
        runtime_id=(7, 8, 9),
        aria_properties="readonly=false;multiline=true",
        aria_role="textbox",
    )
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([document])})
    driver = _driver(
        native=native,
        desktop=desktop,
        profile=_bounded_chat_profile(),
    )
    observation = driver.observe("claude")
    assert observation.elements[0].composer is True
    document.aria_properties = "readonly=false"
    document.element_info.element.CurrentAriaRole = "document"

    action = DesktopAction(
        DesktopActionType.TYPE_TEXT,
        app="claude",
        generation=observation.generation,
        element_index="0",
        text="must not be typed",
    )
    with pytest.raises(WindowsUiaStaleObservation, match="identity or state changed"):
        driver.execute(action, observation)

    assert native.text == []


def test_configured_electron_mode_buttons_use_aria_current_as_selection_state():
    window = _window()
    chat = FakeElement(
        "Chat and Cowork",
        "Button",
        aria_properties="readonly=true",
    )
    code = FakeElement(
        "Code",
        "Button",
        aria_properties="readonly=true;current=page",
    )

    def select_chat():
        chat.aria_properties = "readonly=true;current=page"
        code.aria_properties = "readonly=true"

    chat.on_invoke = select_chat
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([chat, code])})
    driver = _driver(
        native=native,
        desktop=desktop,
        profile=_bounded_chat_profile(
            mode_names={
                "chat": ["Chat and Cowork"],
                "code": ["Code"],
            }
        ),
    )

    before = driver.observe("claude")
    by_name = {element.name: element for element in before.elements}
    assert by_name["Chat and Cowork"].selected is False
    assert by_name["Code"].selected is True

    action = DesktopAction(
        DesktopActionType.CLICK,
        app="claude",
        generation=before.generation,
        element_index=by_name["Chat and Cowork"].index,
    )
    receipt = driver.execute(action, before)
    after = driver.observe("claude")
    selected_after = {element.name: element.selected for element in after.elements}

    assert receipt.accepted
    assert selected_after == {"Chat and Cowork": True, "Code": False}


def test_profile_control_name_limit_is_honored_during_observe_and_rebind():
    window = _window()
    long_but_configured_label = "Open " + ("project " * 70)
    button = FakeElement(long_but_configured_label, "Button")
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([button])})
    driver = _driver(
        native=native,
        desktop=desktop,
        profile=_bounded_chat_profile(max_control_name_chars=700),
    )
    observation = driver.observe("claude")
    action = DesktopAction(
        type=DesktopActionType.CLICK,
        app="claude",
        generation=observation.generation,
        element_index="0",
    )

    receipt = driver.execute(action, observation)

    assert receipt.accepted
    assert button.invocations == 1


def test_profile_drops_one_long_content_node_without_losing_short_controls():
    window = _window()
    long_content = "private conversation " * 40
    native = FakeNative([window])
    desktop = FakeDesktop(
        {window.hwnd: FakeRoot([FakeElement(long_content, "Text"), FakeElement("Chat", "TabItem")])}
    )
    driver = _driver(
        native=native,
        desktop=desktop,
        profile=_bounded_chat_profile(max_content_chars=40),
    )

    observation = driver.observe("claude")
    stats = json.loads(observation.accessibility_text.splitlines()[0])["uia_stats"]

    assert [item.name for item in observation.elements] == ["Chat"]
    assert long_content not in observation.accessibility_text
    assert stats["total"] == 2
    assert stats["kept"] == 1
    assert stats["skipped"] == 1
    assert stats["skipped_long_content"] == 1
    assert stats["bounded_surface_items"] == 1
    assert len(stats["bounded_surface_sha256"]) == 64


def test_profile_can_digest_summarize_instead_of_drop_long_content():
    window = _window()
    long_value = "chat value " * 50
    native = FakeNative([window])
    desktop = FakeDesktop(
        {window.hwnd: FakeRoot([FakeElement("Conversation", "Text", value=long_value)])}
    )
    driver = _driver(
        native=native,
        desktop=desktop,
        profile=_bounded_chat_profile(drop_long_content=False, max_content_chars=30),
    )

    observation = driver.observe("claude")
    stats = json.loads(observation.accessibility_text.splitlines()[0])["uia_stats"]

    assert observation.elements[0].value_metadata is not None
    assert observation.elements[0].value_metadata.truncated is True
    assert observation.elements[0].value_metadata.original_length == len(long_value)
    assert long_value not in observation.accessibility_text
    assert stats["summarized_long_content"] == 1
    assert stats["summarized_long_values"] == 1


def test_profile_bounds_content_node_count_independently_from_controls():
    window = _window()
    native = FakeNative([window])
    desktop = FakeDesktop(
        {
            window.hwnd: FakeRoot(
                [
                    FakeElement("First chat line", "Text"),
                    FakeElement("Second chat line", "Text"),
                    FakeElement("Code", "TabItem"),
                ]
            )
        }
    )
    driver = _driver(
        native=native,
        desktop=desktop,
        profile=_bounded_chat_profile(max_content_nodes=1),
    )

    observation = driver.observe("claude")
    stats = json.loads(observation.accessibility_text.splitlines()[0])["uia_stats"]

    assert [item.name for item in observation.elements] == ["First chat line", "Code"]
    assert stats["omitted_by_content_limit"] == 1
    assert stats["total"] == 3
    assert stats["kept"] == 2
    assert stats["skipped"] == 1


def test_property_error_is_counted_and_does_not_fail_the_window():
    class WindowTextErrorElement(FakeElement):
        def window_text(self):
            raise RuntimeError("unavailable UIA property")

    window = _window()
    element = WindowTextErrorElement("Recovered label", "Button")
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([element])})
    driver = _driver(
        native=native,
        desktop=desktop,
        profile=_bounded_chat_profile(),
    )

    observation = driver.observe("claude")
    stats = json.loads(observation.accessibility_text.splitlines()[0])["uia_stats"]

    assert [item.name for item in observation.elements] == ["Recovered label"]
    assert stats["property_errors"] == 1
    assert stats["total"] == stats["kept"] == 1


def test_password_property_error_omits_only_that_element_and_counts_once():
    class PasswordPropertyErrorElement(FakeElement):
        def __getattribute__(self, name):
            if name == "is_password":
                raise RuntimeError("password property unavailable")
            return super().__getattribute__(name)

    class PasswordInfoProxy:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __getattr__(self, name):
            if name == "is_password":
                raise RuntimeError("password property unavailable")
            return getattr(self.wrapped, name)

    window = _window()
    broken = PasswordPropertyErrorElement("Unknown input", "Edit", focused=True)
    broken.element_info = PasswordInfoProxy(broken.element_info)
    safe = FakeElement("Chat", "Button")
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([broken, safe])})
    driver = _driver(
        native=native,
        desktop=desktop,
        profile=_bounded_chat_profile(),
    )

    observation = driver.observe("claude")
    stats = json.loads(observation.accessibility_text.splitlines()[0])["uia_stats"]

    assert [element.name for element in observation.elements] == ["Chat"]
    assert stats["property_errors"] == 1
    assert stats["omitted_password_property_error"] == 1
    assert observation.property_error_count == 1


def test_explicit_labeled_by_secret_relation_blocks_but_flat_neighbor_text_does_not():
    window = _window()
    labeled = FakeElement(
        "Prompt",
        "Edit",
        value="",
        focused=True,
        labeled_by_name="API Key",
    )
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([labeled])})
    driver = _driver(
        native=native,
        desktop=desktop,
        profile=_bounded_chat_profile(),
    )

    sensitive = driver.observe("claude")

    assert sensitive.elements[0].secret_labeled is True
    assert (
        DesktopSafetyPolicy("personal_trusted").inspect_observation(sensitive).disposition
        == DesktopSafetyDisposition.BLOCK
    )

    plain_label = FakeElement("API Key", "Text")
    ordinary = FakeElement("Prompt", "Edit", value="", focused=True)
    desktop.roots[window.hwnd] = FakeRoot([plain_label, ordinary])
    ordinary_surface = driver.observe("claude")

    prompt = next(item for item in ordinary_surface.elements if item.name == "Prompt")
    assert prompt.secret_labeled is False
    assert (
        DesktopSafetyPolicy("personal_trusted").inspect_observation(ordinary_surface).disposition
        == DesktopSafetyDisposition.ALLOW
    )


def test_null_com_labeled_by_relation_is_absent_and_can_be_revalidated():
    class NullRelation:
        def __bool__(self):
            return False

        @property
        def CurrentName(self):
            raise ValueError("NULL COM pointer access")

    window = _window()
    edit = FakeElement("Prompt", "Edit", value="", focused=True)
    edit.element_info.element.CurrentLabeledBy = NullRelation()
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([edit])})
    driver = _driver(
        native=native,
        desktop=desktop,
        profile=_bounded_chat_profile(),
    )

    observation = driver.observe("claude")
    assert observation.elements[0].secret_labeled is False
    action = DesktopAction(
        DesktopActionType.TYPE_TEXT,
        app="claude",
        generation=observation.generation,
        element_index="0",
        text="verified text",
    )

    receipt = driver.execute(action, observation)

    assert receipt.accepted is True
    assert native.text == ["verified text"]


def test_dialog_is_retained_ahead_of_max_elements_and_never_addressable():
    window = _window()
    button = FakeElement("Continue", "Button")
    dialog = FakeElement("Payment details", "Window")
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([button, dialog])})
    driver = _driver(
        native=native,
        desktop=desktop,
        max_elements=1,
        profile=_bounded_chat_profile(),
    )

    observation = driver.observe("claude")
    element = observation.elements[0]

    assert element.name == "Payment details"
    assert element_plane(element) == ElementPlane.DIALOG
    assert element.addressable is False
    assert (
        DesktopSafetyPolicy("personal_trusted").inspect_observation(observation).disposition
        == DesktopSafetyDisposition.BLOCK
    )
    action = DesktopAction(
        DesktopActionType.CLICK,
        app="claude",
        generation=observation.generation,
        element_index=element.index,
    )
    with pytest.raises(WindowsUiaStaleObservation, match="not in this observation"):
        driver.execute(action, observation)
    assert dialog.invocations == 0


def test_unreadable_dialog_identity_blocks_underlying_controls():
    class NameErrorElement(FakeElement):
        def window_text(self):
            raise RuntimeError("dialog name unavailable")

    class NameErrorInfoProxy:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __getattr__(self, name):
            if name == "name":
                raise RuntimeError("dialog name unavailable")
            return getattr(self.wrapped, name)

    window = _window()
    button = FakeElement("Chat", "Button")
    dialog = NameErrorElement("Payment details", "Window")
    dialog.element_info = NameErrorInfoProxy(dialog.element_info)
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([button, dialog])})
    driver = _driver(
        native=native,
        desktop=desktop,
        profile=_bounded_chat_profile(),
    )

    observation = driver.observe("claude")
    dialog_element = next(
        element for element in observation.elements if element_plane(element) == ElementPlane.DIALOG
    )
    action = DesktopAction(
        DesktopActionType.CLICK,
        app="claude",
        generation=observation.generation,
        element_index=next(
            element.index for element in observation.elements if element.name == "Chat"
        ),
    )
    policy = DesktopSafetyPolicy("personal_trusted")

    assert dialog_element.name == ""
    assert dialog_element.addressable is False
    assert policy.inspect_observation(observation).disposition == DesktopSafetyDisposition.BLOCK
    assert (
        policy.evaluate(
            action,
            observation,
            user_text="In Claude, click Chat",
        ).disposition
        == DesktopSafetyDisposition.BLOCK
    )
    assert button.invocations == 0


@pytest.mark.parametrize("control_type", ["Text", "Document", "Pane", "Group"])
def test_long_container_values_are_digest_summarized_without_losing_short_controls(
    control_type,
):
    window = _window()
    long_value = "private conversation " * 300
    element = FakeElement("Conversation", control_type, value=long_value)
    button = FakeElement("Send", "Button")
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([element, button])})
    driver = _driver(native=native, desktop=desktop)

    observation = driver.observe("claude")
    stats = json.loads(observation.accessibility_text.splitlines()[0])["uia_stats"]

    assert [item.name for item in observation.elements] == ["Conversation", "Send"]
    assert observation.elements[0].value_metadata is not None
    assert observation.elements[0].value_metadata.truncated is True
    assert observation.elements[0].value_metadata.original_length == len(long_value)
    assert long_value not in observation.accessibility_text
    assert stats["summarized_long_values"] == 1
    assert stats["bounded_surface_items"] == 1
    assert len(stats["bounded_surface_sha256"]) == 64


@pytest.mark.parametrize("control_type", ["Text", "Document", "Pane", "Group"])
def test_long_container_names_are_omitted_but_hashed_for_observation_freshness(
    control_type,
):
    window = _window()
    long_text = FakeElement("First message " * 80, control_type)
    button = FakeElement("Send", "Button")
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([long_text, button])})
    driver = _driver(native=native, desktop=desktop)

    first = driver.observe("claude")
    first_stats = json.loads(first.accessibility_text.splitlines()[0])["uia_stats"]
    long_text._name = "Second message " * 80
    long_text.element_info.name = long_text._name
    second = driver.observe("claude")
    second_stats = json.loads(second.accessibility_text.splitlines()[0])["uia_stats"]

    assert [item.control_type for item in first.elements] == [control_type, "Button"]
    assert first.elements[0].addressable is False
    assert first.elements[0].name_metadata is not None
    assert first.elements[0].name_metadata.truncated is True
    assert first_stats["omitted_oversized_name"] == 1
    assert first_stats["bounded_surface_sha256"] != second_stats["bounded_surface_sha256"]
    assert first.fingerprint != second.fingerprint
    assert "First message " * 80 not in first.accessibility_text
    assert "Second message " * 80 not in second.accessibility_text


def test_long_actionable_label_is_not_exposed_as_a_truncated_addressable_target():
    window = _window()
    long_label = "Delete this conversation " * 30
    long_button = FakeElement(long_label, "Button")
    safe_button = FakeElement("Open chat", "Button")
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([long_button, safe_button])})
    driver = _driver(native=native, desktop=desktop)

    observation = driver.observe("claude")
    stats = json.loads(observation.accessibility_text.splitlines()[0])["uia_stats"]

    assert [item.index for item in observation.elements] == ["0", "1"]
    assert observation.elements[0].addressable is False
    assert observation.elements[0].name_metadata is not None
    assert observation.elements[0].name_metadata.truncated is True
    assert observation.elements[1].name == "Open chat"
    assert long_label not in observation.accessibility_text
    assert stats["omitted_oversized_name"] == 1

    action = DesktopAction(
        type=DesktopActionType.CLICK,
        app="claude",
        generation=observation.generation,
        element_index="0",
    )
    with pytest.raises(WindowsUiaStaleObservation, match="not in this observation"):
        driver.execute(action, observation)
    assert long_button.invocations == 0


def test_max_elements_priority_is_focused_then_selected_then_actionable():
    window = _window()
    passive = FakeElement("Passive", "Text")
    actionable = FakeElement("Open", "Button")
    selected = FakeElement("Selected row", "Text", selected=True)
    focused = FakeElement("Prompt", "Document", focused=True)
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([passive, actionable, selected, focused])})
    driver = _driver(native=native, desktop=desktop, max_elements=2)

    observation = driver.observe("claude")
    stats = json.loads(observation.accessibility_text.splitlines()[0])["uia_stats"]

    assert [item.name for item in observation.elements] == ["Selected row", "Prompt"]
    assert stats["retained_focused"] == 1
    assert stats["retained_selected"] == 1
    assert stats["omitted_by_max_elements"] == 2


def test_task_context_prioritizes_matching_controls_and_is_explicitly_cleared():
    window = _window()
    other = FakeElement("Other", "Button")
    target = FakeElement("Target", "Button")
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([other, target])})
    driver = _driver(native=native, desktop=desktop, max_elements=1)

    driver.set_task_context("In Claude, click Target")
    first = driver.observe("claude")
    driver.set_task_context(None)
    second = driver.observe("claude")

    assert [element.name for element in first.elements] == ["Target"]
    assert [element.name for element in second.elements] == ["Other"]


def test_typed_observation_stats_report_bounding_without_parsing_header():
    window = _window()
    native = FakeNative([window])
    desktop = FakeDesktop(
        {window.hwnd: FakeRoot([FakeElement("Open", "Button"), FakeElement("Status", "Text")])}
    )
    driver = _driver(native=native, desktop=desktop, max_elements=1)

    observation = driver.observe("claude")

    assert observation.total_element_count == 2
    assert observation.elements_truncated is True
    assert observation.skipped_long_content_count == 0
    assert observation.property_error_count == 0


def test_empty_and_unreadable_values_remain_distinguishable():
    window = _window()
    unreadable = FakeElement("Prompt", "Edit", value=None)
    empty = FakeElement("Message", "Edit", value="")
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([unreadable, empty])})
    driver = _driver(
        native=native,
        desktop=desktop,
        profile=_bounded_chat_profile(),
    )

    observation = driver.observe("claude")
    by_name = {element.name: element for element in observation.elements}

    assert by_name["Prompt"].value is None
    assert by_name["Prompt"].value_observed is False
    assert by_name["Message"].value is None
    assert by_name["Message"].value_observed is True


def test_raw_credential_finding_counts_survive_long_value_bounding():
    window = _window()
    first_secret = "sk-proj-" + ("A" * 32)
    second_secret = "github_pat_" + ("B" * 32)
    long_value = ("x" * 2500) + " " + first_secret + " " + ("y" * 2500) + " " + second_secret
    composer = FakeElement("Prompt", "Edit", value=long_value, focused=True)
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([composer])})
    driver = _driver(
        native=native,
        desktop=desktop,
        profile=_bounded_chat_profile(),
    )

    observation = driver.observe("claude")
    element = observation.elements[0]

    assert element.high_credential is True
    assert observation.high_credential_count == 2
    assert observation.low_credential_count == 0
    assert observation.credential_affected_element_count == 1
    assert first_secret not in observation.accessibility_text
    assert second_secret not in observation.accessibility_text
    assert (
        DesktopSafetyPolicy("personal_trusted").inspect_observation(observation).disposition
        == DesktopSafetyDisposition.BLOCK
    )


def test_changes_to_max_element_omissions_still_change_the_observation_fingerprint():
    window = _window()
    button = FakeElement("Open", "Button", focused=True)
    omitted_text = FakeElement("Old status", "Text")
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([button, omitted_text])})
    driver = _driver(native=native, desktop=desktop, max_elements=1)

    first = driver.observe("claude")
    first_stats = json.loads(first.accessibility_text.splitlines()[0])["uia_stats"]
    omitted_text._name = "New status"
    omitted_text.element_info.name = omitted_text._name
    second = driver.observe("claude")
    second_stats = json.loads(second.accessibility_text.splitlines()[0])["uia_stats"]

    assert [item.name for item in first.elements] == ["Open"]
    assert [item.name for item in second.elements] == ["Open"]
    assert first_stats["bounded_surface_sha256"] != second_stats["bounded_surface_sha256"]
    assert first.fingerprint != second.fingerprint


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
    password_value = "must-not-enter-observation" * 300
    password = FakeElement(
        "Password",
        "Edit",
        value=password_value,
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
    assert password.value_reads == 0

    click = DesktopAction(
        type=DesktopActionType.CLICK,
        app="claude",
        generation=observation.generation,
        element_index="0",
    )
    with pytest.raises(PasswordFieldError, match="password"):
        driver.execute(click, observation)
    assert password.invocations == 0


def test_digest_summarized_value_is_rebound_before_keyboard_input():
    window = _window()
    edit = FakeElement("Prompt", "Edit", value="A" * 5000, focused=True)
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([edit])})
    driver = _driver(native=native, desktop=desktop)
    observation = driver.observe("claude")
    edit._value = "B" * 5000
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


@pytest.mark.parametrize("state_method", ["is_visible", "is_enabled"])
def test_action_fails_closed_when_runtime_state_property_raises(state_method):
    window = _window()
    button = FakeElement("Chat", "Button")
    native = FakeNative([window])
    desktop = FakeDesktop({window.hwnd: FakeRoot([button])})
    driver = _driver(native=native, desktop=desktop)
    observation = driver.observe("claude")

    def unavailable():
        raise RuntimeError("UIA state unavailable")

    setattr(button, state_method, unavailable)
    action = DesktopAction(
        DesktopActionType.CLICK,
        app="claude",
        generation=observation.generation,
        element_index="0",
    )

    with pytest.raises(WindowsUiaStaleObservation, match="could not be revalidated"):
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
