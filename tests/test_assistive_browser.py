from __future__ import annotations

from types import SimpleNamespace

from handsfree_pc.desktop.assistive.policy import AssistivePolicy
from handsfree_pc.desktop.assistive.skills.browser import BrowserNavigationSkill
from handsfree_pc.desktop.assistive.task_parser import parse_task
from handsfree_pc.desktop.protocol import (
    DesktopElement,
    DesktopObservation,
    ElementPlane,
)
from handsfree_pc.windows.native import WindowInfo


def _address_observation(
    *,
    generation: int,
    value: str,
    focused: bool = True,
) -> DesktopObservation:
    return DesktopObservation(
        app="chrome",
        generation=generation,
        accessibility_text='0 name="Address and search bar" control_type="Edit"',
        window_title="Google - Google Chrome",
        process_name="chrome.exe",
        local_window_id="hwnd:101",
        elements=(
            DesktopElement(
                "0",
                "Address and search bar",
                "Edit",
                automation_id="address-bar",
                value=value,
                value_observed=True,
                focused=focused,
                plane=ElementPlane.INPUT,
                local_identity="a" * 64,
                browser_chrome=True,
            ),
        ),
    )


class FakeBrowserNative:
    def __init__(self, *, process_name: str = "chrome.exe") -> None:
        self.window = WindowInfo(
            101,
            "Google - Google Chrome",
            501,
            process_name,
            "Chrome_WidgetWin_1",
        )
        self.events: list[tuple[str, str | int]] = []

    def assert_interactive_desktop(self) -> None:
        self.events.append(("interactive", self.window.hwnd))

    def get_foreground_window_info(self) -> WindowInfo:
        return self.window

    def assert_foreground(self, hwnd: int) -> None:
        assert hwnd == self.window.hwnd
        self.events.append(("foreground", hwnd))

    def send_hotkey(self, specification: str) -> tuple[int, ...]:
        self.events.append(("hotkey", specification))
        return (1,)

    def send_text(self, text: str) -> int:
        self.events.append(("text", text))
        return len(text)

    def keystrokes(self) -> list[tuple[str, str | int]]:
        return [event for event in self.events if event[0] in {"hotkey", "text"}]


class FakeBrowserDriver:
    """Serve scripted live omnibox states; the skill never re-walks the window."""

    def __init__(
        self,
        observations: list[DesktopObservation],
        *,
        states: list[tuple[str | None, bool | None]] | None = None,
    ) -> None:
        self.observations = list(observations)
        self.states = list(states or [])
        self.state_reads: list[tuple[int, str]] = []

    def observe(self, app: str, *, cancel_event=None) -> DesktopObservation:
        assert app == "chrome"
        return self.observations.pop(0)

    def read_element_state(self, before, element) -> tuple[str | None, bool | None]:
        self.state_reads.append((before.generation, element.index))
        if len(self.states) > 1:
            return self.states.pop(0)
        if self.states:
            return self.states[0]
        return element.value, element.focused

    @staticmethod
    def assert_browser_address_bound(before, element, *, require_focus=False) -> None:
        assert element in before.elements
        assert element.browser_chrome is True
        if require_focus:
            assert element.focused is True


def _profiles() -> dict[str, object]:
    return {"chrome": SimpleNamespace(process_names=("chrome.exe",))}


def test_browser_skill_preserves_exact_query_and_uses_bound_address_bar() -> None:
    url = "https://www.google.com/?handsfreepc-scenario=abc123&lang=zh-CN#top"
    native = FakeBrowserNative()
    initial = _address_observation(generation=1, value="about:blank", focused=False)
    driver = FakeBrowserDriver([], states=[("about:blank", True), (url, True)])
    task = parse_task(f"切换到 Chrome 打开 {url}")

    skill = BrowserNavigationSkill(
        native,
        driver,
        AssistivePolicy(),
        _profiles(),
    )

    result = skill.navigate(task, app="chrome", raw_url=url, observation=initial)

    assert result.status == "succeeded"
    assert result.details["actions"] == 3
    assert native.keystrokes() == [("hotkey", "ctrl+l"), ("text", url), ("hotkey", "enter")]
    # Every state read is bound to the observation the skill planned against.
    assert all(generation == 1 and index == "0" for generation, index in driver.state_reads)


def test_browser_skill_accepts_an_inline_autocompleted_trailing_slash() -> None:
    url = "https://google.com"
    native = FakeBrowserNative()
    initial = _address_observation(generation=1, value="", focused=True)
    driver = FakeBrowserDriver([], states=[("https://google.com/", True)])

    result = BrowserNavigationSkill(native, driver, AssistivePolicy(), _profiles()).navigate(
        parse_task(f"切换到 Chrome 打开 {url}"),
        app="chrome",
        raw_url=url,
        observation=initial,
    )

    assert result.status == "succeeded"
    assert native.keystrokes() == [("text", url), ("hotkey", "enter")]


def test_browser_skill_rejects_credential_bearing_or_non_http_urls() -> None:
    task = parse_task("切换到 Chrome 打开 Google 网页")
    native = FakeBrowserNative()
    driver = FakeBrowserDriver([])
    skill = BrowserNavigationSkill(native, driver, AssistivePolicy(), _profiles())

    credential = skill.navigate(
        task,
        app="chrome",
        raw_url="https://user:secret@example.com/",
    )
    file_url = skill.navigate(task, app="chrome", raw_url="file:///C:/private.txt")

    assert credential.status == "hard_failure"
    assert file_url.status == "retryable_failure"
    assert native.events == []


def test_browser_skill_requires_the_exact_configured_browser_foreground() -> None:
    task = parse_task("切换到 Chrome 打开 Google 网页")
    native = FakeBrowserNative(process_name="msedge.exe")
    driver = FakeBrowserDriver([])

    result = BrowserNavigationSkill(
        native,
        driver,
        AssistivePolicy(),
        _profiles(),
    ).navigate(task, app="chrome", raw_url="google.com")

    assert result.status == "retryable_failure"
    assert native.keystrokes() == []


def test_browser_skill_accepts_the_driver_dynamic_id_for_a_bound_chrome_window() -> None:
    url = "https://www.google.com/?handsfreepc-scenario=dynamic"
    native = FakeBrowserNative()
    base = _address_observation(generation=1, value="about:blank", focused=False)
    initial = DesktopObservation(
        app="chrome-deadbeef",
        generation=base.generation,
        accessibility_text=base.accessibility_text,
        window_title=base.window_title,
        process_name=base.process_name,
        local_window_id=base.local_window_id,
        elements=base.elements,
    )

    result = BrowserNavigationSkill(
        native,
        FakeBrowserDriver([], states=[("about:blank", True), (url, True)]),
        AssistivePolicy(),
        _profiles(),
    ).navigate(
        parse_task(f"切换到 Chrome 打开 {url}"),
        app="chrome-deadbeef",
        raw_url=url,
        observation=initial,
    )

    assert result.status == "succeeded"


def test_browser_skill_does_not_type_without_a_stable_address_identity() -> None:
    url = "https://www.google.com/"
    unstable = _address_observation(generation=1, value="")
    unstable = DesktopObservation(
        app=unstable.app,
        generation=unstable.generation,
        accessibility_text=unstable.accessibility_text,
        window_title=unstable.window_title,
        process_name=unstable.process_name,
        local_window_id=unstable.local_window_id,
        elements=(
            DesktopElement(
                "0",
                "Address and search bar",
                "Edit",
                automation_id="address-bar",
                value="",
                value_observed=True,
                focused=True,
                editable=True,
                plane=ElementPlane.INPUT,
                browser_chrome=True,
            ),
        ),
    )
    native = FakeBrowserNative()
    driver = FakeBrowserDriver([])

    result = BrowserNavigationSkill(
        native,
        driver,
        AssistivePolicy(),
        _profiles(),
        address_timeout=0,
    ).navigate(
        parse_task(f"切换到 Chrome 打开 {url}"),
        app="chrome",
        raw_url=url,
        observation=unstable,
    )

    assert result.status == "retryable_failure"
    assert native.keystrokes() == []


def test_browser_skill_never_substring_accepts_a_different_address() -> None:
    url = "https://google.com/"
    malicious = f"https://evil.example/?next={url}"
    native = FakeBrowserNative()
    initial = _address_observation(generation=1, value="")
    driver = FakeBrowserDriver([], states=[(malicious, True)])

    result = BrowserNavigationSkill(
        native,
        driver,
        AssistivePolicy(),
        _profiles(),
        typed_timeout=0,
    ).navigate(
        parse_task(f"切换到 Chrome 打开 {url}"),
        app="chrome",
        raw_url=url,
        observation=initial,
    )

    assert result.status == "retryable_failure"
    assert native.keystrokes() == [("text", url)]


def test_browser_skill_rejects_page_spoofed_omnibox_name_and_automation_id() -> None:
    page_field = DesktopObservation(
        app="chrome",
        generation=1,
        accessibility_text='0 name="Address and search bar" control_type="Edit"',
        window_title="Profile - Google Chrome",
        process_name="chrome.exe",
        local_window_id="hwnd:101",
        elements=(
            DesktopElement(
                "0",
                "Address and search bar",
                "Edit",
                automation_id="view_1012",
                value="https://example.com/",
                value_observed=True,
                focused=True,
                editable=True,
                local_identity="b" * 64,
            ),
        ),
    )
    native = FakeBrowserNative()
    driver = FakeBrowserDriver([])

    result = BrowserNavigationSkill(
        native,
        driver,
        AssistivePolicy(),
        _profiles(),
    ).navigate(
        parse_task("切换到 Chrome 打开 https://google.com/"),
        app="chrome",
        raw_url="https://google.com/",
        observation=page_field,
    )

    assert result.status == "retryable_failure"
    assert native.keystrokes() == []


def test_browser_skill_blocks_sensitive_window_before_ctrl_l() -> None:
    sensitive = _address_observation(
        generation=1,
        value="https://example.com/login",
        focused=False,
    )
    sensitive = DesktopObservation(
        app=sensitive.app,
        generation=sensitive.generation,
        accessibility_text=sensitive.accessibility_text,
        window_title="Sign in - Google Chrome",
        process_name=sensitive.process_name,
        local_window_id=sensitive.local_window_id,
        elements=sensitive.elements
        + (
            DesktopElement(
                "1",
                "Password",
                "Edit",
                password=True,
                focused=False,
                local_identity="c" * 64,
            ),
        ),
    )
    native = FakeBrowserNative()
    driver = FakeBrowserDriver([])

    result = BrowserNavigationSkill(
        native,
        driver,
        AssistivePolicy(),
        _profiles(),
    ).navigate(
        parse_task("切换到 Chrome 打开 https://google.com/"),
        app="chrome",
        raw_url="https://google.com/",
        observation=sensitive,
    )

    assert result.status == "hard_failure"
    assert native.keystrokes() == []


def test_browser_skill_does_not_type_if_ctrl_l_never_focuses_the_bound_address_bar() -> None:
    initial = _address_observation(
        generation=1,
        value="about:blank",
        focused=False,
    )
    native = FakeBrowserNative()
    driver = FakeBrowserDriver([], states=[("about:blank", False)])

    result = BrowserNavigationSkill(
        native,
        driver,
        AssistivePolicy(),
        _profiles(),
        address_timeout=0,
    ).navigate(
        parse_task("切换到 Chrome 打开 https://google.com/"),
        app="chrome",
        raw_url="https://google.com/",
        observation=initial,
    )

    assert result.status == "retryable_failure"
    assert native.keystrokes() == [("hotkey", "ctrl+l")]


def test_browser_skill_does_not_press_enter_when_the_typed_text_is_not_in_the_bar() -> None:
    url = "https://google.com/"
    initial = _address_observation(generation=1, value="", focused=True)
    native = FakeBrowserNative()
    driver = FakeBrowserDriver([], states=[(f"{url}junk", True)])

    result = BrowserNavigationSkill(
        native,
        driver,
        AssistivePolicy(),
        _profiles(),
        typed_timeout=0,
    ).navigate(
        parse_task(f"切换到 Chrome 打开 {url}"),
        app="chrome",
        raw_url=url,
        observation=initial,
    )

    assert result.status == "retryable_failure"
    assert native.keystrokes() == [("text", url)]


def test_browser_skill_requires_a_driver_that_can_read_live_element_state() -> None:
    class ObserveOnlyDriver:
        @staticmethod
        def assert_browser_address_bound(before, element, *, require_focus=False) -> None:
            return None

    native = FakeBrowserNative()
    initial = _address_observation(generation=1, value="", focused=True)

    result = BrowserNavigationSkill(
        native,
        ObserveOnlyDriver(),
        AssistivePolicy(),
        _profiles(),
    ).navigate(
        parse_task("切换到 Chrome 打开 https://google.com/"),
        app="chrome",
        raw_url="https://google.com/",
        observation=initial,
    )

    assert result.status == "retryable_failure"
    assert native.keystrokes() == []
