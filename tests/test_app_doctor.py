from __future__ import annotations

import json
from pathlib import Path

import pytest

from handsfree_pc.app_doctor import (
    AppDoctorFailure,
    run_app_doctor,
    safe_observation_report,
    select_composer,
)
from handsfree_pc.config import load_settings
from handsfree_pc.desktop.protocol import (
    ActionReceipt,
    DesktopActionType,
    DesktopElement,
    DesktopObservation,
    ElementPlane,
)
from handsfree_pc.desktop.safety import DesktopSafetyPolicy

_IDENTITY_A = "a" * 64
_IDENTITY_B = "b" * 64


def _observation(
    app: str = "claude",
    *,
    generation: int = 1,
    elements: tuple[DesktopElement, ...],
    local_window_id: str = "hwnd:123",
    captured_at: float | None = None,
) -> DesktopObservation:
    header = json.dumps(
        {
            "uia_stats": {
                "descendants": 100,
                "retained": len(elements),
                "bounded_surface_items": 2,
                "bounded_surface_sha256": "a" * 64,
            },
            "window_title": "private conversation title",
            "process_id": 1234,
        }
    )
    lines = [header]
    for element in elements:
        payload = element.planner_payload()
        lines.append(
            f"{element.index} "
            + " ".join(
                f"{key}={json.dumps(value, ensure_ascii=False)}"
                for key, value in payload.items()
                if key != "index"
            )
        )
    values = {}
    if captured_at is not None:
        values["captured_at"] = captured_at
    return DesktopObservation(
        app=app,
        generation=generation,
        accessibility_text="\n".join(lines),
        window_title=f"{app.title()} private conversation title",
        local_window_id=local_window_id,
        elements=elements,
        **values,
    )


def _settings(tmp_path: Path, *, safety_profile: str = "personal_trusted"):
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
privacy:
  allow_cloud_planner: false
computer_control:
  enabled: true
  backend: local_agent
  driver: windows_uia
  planner_backend: none
  safety_profile: {safety_profile}
execution:
  dry_run: false
""",
        encoding="utf-8",
    )
    return load_settings(path)


def test_safe_observation_report_excludes_chat_values_ids_and_paths() -> None:
    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz012345"
    opaque = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_TOKEN"
    observation = _observation(
        elements=(
            DesktopElement("1", r"Open C:\Users\person\Private", "Button"),
            DesktopElement(
                "2",
                "chat body",
                "Document",
                automation_id=opaque,
                value=f"example only {secret}",
            ),
            DesktopElement("3", "Prompt", "Edit", focused=True),
        )
    )

    report = safe_observation_report(
        observation,
        policy=DesktopSafetyPolicy("personal_trusted"),
    )
    serialized = json.dumps(report, ensure_ascii=False)

    assert report["observe_succeeded"] is True
    assert report["credential_findings"]["high"] == 1
    assert report["chat_content_included"] is False
    assert report["element_values_included"] is False
    assert report["automation_ids_included"] is False
    assert secret not in serialized
    assert opaque not in serialized
    assert r"C:\Users\person\Private" not in serialized
    assert "private conversation title" not in serialized
    assert report["uia_stats"]["bounded_surface_sha256"] == "a" * 64


def test_select_composer_prefers_unique_focused_edit() -> None:
    observation = _observation(
        elements=(
            DesktopElement("1", "Search", "Edit", focused=False),
            DesktopElement("2", "Message", "Edit", focused=True, composer=True),
        )
    )

    assert select_composer(observation).index == "2"


def test_select_composer_fails_closed_when_ambiguous() -> None:
    observation = _observation(
        elements=(
            DesktopElement("1", "Prompt", "Edit", focused=False, composer=True),
            DesktopElement("2", "Message", "Edit", focused=False, composer=True),
        )
    )

    with pytest.raises(AppDoctorFailure) as caught:
        select_composer(observation)

    assert caught.value.error_code == "COMPOSER_NOT_UNIQUE"


def test_select_composer_requires_driver_semantics_before_using_profile_hints() -> None:
    observation = _observation(
        elements=(
            DesktopElement("1", "Search messages", "Edit", focused=True),
            DesktopElement("2", "Prompt", "Edit", focused=False, composer=True),
        )
    )

    assert select_composer(observation, hints=("message", "prompt")).index == "2"


def test_run_app_doctor_draft_smoke_freshly_verifies_without_sending(tmp_path) -> None:
    settings = _settings(tmp_path)

    class FakeDriver:
        def __init__(self, _profiles) -> None:
            self.action = None
            self.closed = False

        def observe(self, app):
            value = self.action.text if self.action is not None else ""
            return _observation(
                app,
                generation=2 if self.action is not None else 1,
                elements=(
                    DesktopElement("1", "Chat", "Button"),
                    DesktopElement(
                        "2",
                        "Prompt",
                        "Edit",
                        value=value,
                        focused=True,
                        composer=True,
                        local_identity=_IDENTITY_A,
                    ),
                ),
            )

        def execute(self, action, before):
            assert action.type == DesktopActionType.TYPE_TEXT
            assert action.text is not None
            self.action = action
            return ActionReceipt(action, True, before.generation, "accepted")

        def close(self):
            self.closed = True

    report = run_app_doctor(
        settings,
        app="claude",
        draft_smoke=True,
        driver_factory=FakeDriver,
    )

    smoke = report["draft_smoke"]
    assert smoke["performed"] is True
    assert smoke["sent"] is False
    assert smoke["focus_verified"] is True
    assert smoke["fresh_observation"] is True
    assert smoke["action_verified"] is True
    assert smoke["expectation_verified"] is True
    assert smoke["verified"] is True
    assert "HandsFreePC-DRAFT" not in json.dumps(report)


def test_draft_smoke_requires_explicit_personal_profile_before_action(tmp_path) -> None:
    settings = _settings(tmp_path, safety_profile="strict")

    class ObserveOnlyDriver:
        execute_called = False

        def __init__(self, _profiles) -> None:
            pass

        @staticmethod
        def observe(app):
            return _observation(
                app,
                elements=(DesktopElement("2", "Prompt", "Edit", focused=True),),
            )

        def execute(self, *_args):
            self.execute_called = True
            raise AssertionError("strict mode must fail before input")

        @staticmethod
        def close():
            pass

    with pytest.raises(AppDoctorFailure) as caught:
        run_app_doctor(
            settings,
            app="claude",
            draft_smoke=True,
            driver_factory=ObserveOnlyDriver,
        )

    assert caught.value.error_code == "PERSONAL_TRUSTED_REQUIRED"


def test_draft_smoke_refuses_an_unreadable_composer_value(tmp_path) -> None:
    settings = _settings(tmp_path)

    class UnreadableDriver:
        execute_called = False

        def __init__(self, _profiles) -> None:
            pass

        @staticmethod
        def observe(app):
            return _observation(
                app,
                elements=(
                    DesktopElement(
                        "2",
                        "Prompt",
                        "Edit",
                        focused=True,
                        composer=True,
                        local_identity=_IDENTITY_A,
                    ),
                ),
            )

        def execute(self, *_args):
            self.execute_called = True
            raise AssertionError("unreadable composer must not be changed")

        @staticmethod
        def close():
            pass

    with pytest.raises(AppDoctorFailure) as caught:
        run_app_doctor(
            settings,
            app="claude",
            draft_smoke=True,
            driver_factory=UnreadableDriver,
        )

    assert caught.value.error_code == "COMPOSER_VALUE_UNREADABLE"
    assert UnreadableDriver.execute_called is False


def test_draft_smoke_refuses_nonempty_value_even_when_it_equals_the_label(tmp_path) -> None:
    settings = _settings(tmp_path)

    class NonemptyDriver:
        execute_called = False

        def __init__(self, _profiles) -> None:
            pass

        @staticmethod
        def observe(app):
            return _observation(
                app,
                elements=(
                    DesktopElement(
                        "2",
                        "Prompt",
                        "Edit",
                        value="Prompt",
                        focused=True,
                        composer=True,
                        local_identity=_IDENTITY_A,
                    ),
                ),
            )

        def execute(self, *_args):
            self.execute_called = True
            raise AssertionError("nonempty composer must not be changed")

        @staticmethod
        def close():
            pass

    with pytest.raises(AppDoctorFailure) as caught:
        run_app_doctor(
            settings,
            app="claude",
            draft_smoke=True,
            driver_factory=NonemptyDriver,
        )

    assert caught.value.error_code == "COMPOSER_NOT_EMPTY"
    assert NonemptyDriver.execute_called is False


def test_draft_smoke_never_reselects_a_different_composer_identity(tmp_path) -> None:
    settings = _settings(tmp_path)

    class ChangedComposerDriver:
        def __init__(self, _profiles) -> None:
            self.action = None

        def observe(self, app):
            if self.action is None:
                element = DesktopElement(
                    "2",
                    "Prompt",
                    "Edit",
                    value="",
                    focused=True,
                    composer=True,
                    local_identity=_IDENTITY_A,
                )
                generation = 1
            else:
                element = DesktopElement(
                    "3",
                    "Message",
                    "Edit",
                    value=self.action.text,
                    focused=True,
                    composer=True,
                    local_identity=_IDENTITY_B,
                )
                generation = 2
            return _observation(app, generation=generation, elements=(element,))

        def execute(self, action, before):
            self.action = action
            return ActionReceipt(action, True, before.generation, "accepted")

        @staticmethod
        def close():
            pass

    with pytest.raises(AppDoctorFailure) as caught:
        run_app_doctor(
            settings,
            app="claude",
            draft_smoke=True,
            driver_factory=ChangedComposerDriver,
        )

    assert caught.value.error_code == "COMPOSER_IDENTITY_CHANGED"


def test_draft_smoke_requires_stable_identity_even_for_same_named_composer(tmp_path) -> None:
    settings = _settings(tmp_path)

    class IdentitylessDriver:
        execute_called = False

        def __init__(self, _profiles) -> None:
            pass

        @staticmethod
        def observe(app):
            return _observation(
                app,
                elements=(
                    DesktopElement(
                        "2",
                        "Prompt",
                        "Edit",
                        value="",
                        focused=True,
                        composer=True,
                    ),
                ),
            )

        def execute(self, *_args):
            self.execute_called = True
            raise AssertionError("identityless composer must not be changed")

        @staticmethod
        def close():
            pass

    with pytest.raises(AppDoctorFailure) as caught:
        run_app_doctor(
            settings,
            app="claude",
            draft_smoke=True,
            driver_factory=IdentitylessDriver,
        )

    assert caught.value.error_code == "COMPOSER_IDENTITY_UNSTABLE"
    assert IdentitylessDriver.execute_called is False


def test_focus_success_with_a_different_hwnd_stops_before_click_fallback(tmp_path) -> None:
    settings = _settings(tmp_path)

    class CrossWindowDriver:
        actions = []

        def __init__(self, _profiles) -> None:
            self.after_focus = False

        def observe(self, app):
            return _observation(
                app,
                generation=2 if self.after_focus else 1,
                local_window_id="hwnd:999" if self.after_focus else "hwnd:123",
                elements=(
                    DesktopElement(
                        "2",
                        "Prompt",
                        "Edit",
                        value="",
                        focused=self.after_focus,
                        composer=True,
                        local_identity=_IDENTITY_A,
                    ),
                ),
            )

        def execute(self, action, before):
            self.actions.append(action.type)
            self.after_focus = True
            return ActionReceipt(action, True, before.generation, "accepted")

        @staticmethod
        def close():
            pass

    with pytest.raises(AppDoctorFailure) as caught:
        run_app_doctor(
            settings,
            app="claude",
            draft_smoke=True,
            driver_factory=CrossWindowDriver,
        )

    assert caught.value.error_code == "COMPOSER_FOCUS_OBSERVATION_STALE"
    assert CrossWindowDriver.actions == [DesktopActionType.PERFORM_SECONDARY_ACTION]


def test_focus_exception_with_a_sensitive_fresh_surface_never_clicks(tmp_path) -> None:
    settings = _settings(tmp_path)

    class SensitiveFocusDriver:
        actions = []

        def __init__(self, _profiles) -> None:
            self.failed = False

        def observe(self, app):
            elements = [
                DesktopElement(
                    "2",
                    "Prompt",
                    "Edit",
                    value="",
                    focused=False,
                    composer=True,
                    local_identity=_IDENTITY_A,
                )
            ]
            if self.failed:
                elements.append(
                    DesktopElement(
                        "9",
                        "API Key",
                        "Window",
                        plane=ElementPlane.DIALOG,
                        addressable=False,
                    )
                )
            return _observation(
                app,
                generation=2 if self.failed else 1,
                elements=tuple(elements),
            )

        def execute(self, action, _before):
            self.actions.append(action.type)
            self.failed = True
            raise RuntimeError("focus failed")

        @staticmethod
        def close():
            pass

    with pytest.raises(AppDoctorFailure) as caught:
        run_app_doctor(
            settings,
            app="claude",
            draft_smoke=True,
            driver_factory=SensitiveFocusDriver,
        )

    assert caught.value.error_code == "COMPOSER_FOCUS_SURFACE_BLOCKED"
    assert SensitiveFocusDriver.actions == [DesktopActionType.PERFORM_SECONDARY_ACTION]


def test_focus_exception_with_a_different_hwnd_never_clicks(tmp_path) -> None:
    settings = _settings(tmp_path)

    class FailedCrossWindowDriver:
        actions = []

        def __init__(self, _profiles) -> None:
            self.failed = False

        def observe(self, app):
            return _observation(
                app,
                generation=2 if self.failed else 1,
                local_window_id="hwnd:999" if self.failed else "hwnd:123",
                elements=(
                    DesktopElement(
                        "2",
                        "Prompt",
                        "Edit",
                        value="",
                        focused=False,
                        composer=True,
                        local_identity=_IDENTITY_A,
                    ),
                ),
            )

        def execute(self, action, _before):
            self.actions.append(action.type)
            self.failed = True
            raise RuntimeError("focus failed")

        @staticmethod
        def close():
            pass

    with pytest.raises(AppDoctorFailure) as caught:
        run_app_doctor(
            settings,
            app="claude",
            draft_smoke=True,
            driver_factory=FailedCrossWindowDriver,
        )

    assert caught.value.error_code == "COMPOSER_FOCUS_OBSERVATION_STALE"
    assert FailedCrossWindowDriver.actions == [
        DesktopActionType.PERFORM_SECONDARY_ACTION
    ]


@pytest.mark.parametrize(
    ("after_generation", "after_captured_at"),
    [(1, 11.0), (2, 9.0)],
)
def test_focus_success_requires_new_generation_and_nondecreasing_timestamp(
    tmp_path,
    after_generation,
    after_captured_at,
) -> None:
    settings = _settings(tmp_path)

    class StaleObservationDriver:
        actions = []

        def __init__(self, _profiles) -> None:
            self.after_focus = False

        def observe(self, app):
            return _observation(
                app,
                generation=after_generation if self.after_focus else 1,
                captured_at=after_captured_at if self.after_focus else 10.0,
                elements=(
                    DesktopElement(
                        "2",
                        "Prompt",
                        "Edit",
                        value="",
                        focused=self.after_focus,
                        composer=True,
                        local_identity=_IDENTITY_A,
                    ),
                ),
            )

        def execute(self, action, before):
            self.actions.append(action.type)
            self.after_focus = True
            return ActionReceipt(action, True, before.generation, "accepted")

        @staticmethod
        def close():
            pass

    with pytest.raises(AppDoctorFailure) as caught:
        run_app_doctor(
            settings,
            app="claude",
            draft_smoke=True,
            driver_factory=StaleObservationDriver,
        )

    assert caught.value.error_code == "COMPOSER_FOCUS_OBSERVATION_STALE"
    assert StaleObservationDriver.actions == [
        DesktopActionType.PERFORM_SECONDARY_ACTION
    ]


def test_focus_uses_click_fallback_then_types_only_after_fresh_verification(tmp_path) -> None:
    settings = _settings(tmp_path)

    class FallbackDriver:
        actions = []

        def __init__(self, _profiles) -> None:
            self.generation = 0
            self.focused = False
            self.value = ""

        def observe(self, app):
            self.generation += 1
            return _observation(
                app,
                generation=self.generation,
                elements=(
                    DesktopElement(
                        "2",
                        "Prompt",
                        "Edit",
                        value=self.value,
                        focused=self.focused,
                        composer=True,
                        local_identity=_IDENTITY_A,
                    ),
                ),
            )

        def execute(self, action, before):
            self.actions.append(action.type)
            if (
                action.type == DesktopActionType.PERFORM_SECONDARY_ACTION
                and action.action_name == "clickfocus"
            ):
                self.focused = True
            elif action.type == DesktopActionType.TYPE_TEXT:
                self.value = action.text or ""
            return ActionReceipt(action, True, before.generation, "accepted")

        @staticmethod
        def close():
            pass

    report = run_app_doctor(
        settings,
        app="claude",
        draft_smoke=True,
        driver_factory=FallbackDriver,
    )

    assert report["draft_smoke"]["verified"] is True
    assert FallbackDriver.actions == [
        DesktopActionType.PERFORM_SECONDARY_ACTION,
        DesktopActionType.PERFORM_SECONDARY_ACTION,
        DesktopActionType.TYPE_TEXT,
    ]


def test_partial_setfocus_success_is_reobserved_without_physical_click(tmp_path) -> None:
    settings = _settings(tmp_path)

    class PartialFocusDriver:
        actions = []

        def __init__(self, _profiles) -> None:
            self.generation = 0
            self.focused = False
            self.value = ""

        def observe(self, app):
            self.generation += 1
            return _observation(
                app,
                generation=self.generation,
                elements=(
                    DesktopElement(
                        "2",
                        "Prompt",
                        "Edit",
                        value=self.value,
                        focused=self.focused,
                        composer=True,
                        local_identity=_IDENTITY_A,
                    ),
                ),
            )

        def execute(self, action, before):
            self.actions.append((action.type, action.action_name))
            if action.action_name == "setfocus":
                self.focused = True
                raise RuntimeError("SetFocus changed state before reporting failure")
            if action.type == DesktopActionType.TYPE_TEXT:
                self.value = action.text or ""
            return ActionReceipt(action, True, before.generation, "accepted")

        @staticmethod
        def close():
            pass

    report = run_app_doctor(
        settings,
        app="claude",
        draft_smoke=True,
        driver_factory=PartialFocusDriver,
    )

    assert report["draft_smoke"]["verified"] is True
    assert PartialFocusDriver.actions == [
        (DesktopActionType.PERFORM_SECONDARY_ACTION, "setfocus"),
        (DesktopActionType.TYPE_TEXT, None),
    ]


def test_draft_smoke_blocks_a_sensitive_dialog_that_appears_after_typing(tmp_path) -> None:
    settings = _settings(tmp_path)

    class SensitiveAfterDriver:
        def __init__(self, _profiles) -> None:
            self.action = None

        def observe(self, app):
            elements = [
                DesktopElement(
                    "2",
                    "Prompt",
                    "Edit",
                    value="" if self.action is None else self.action.text,
                    focused=True,
                    composer=True,
                    local_identity=_IDENTITY_A,
                )
            ]
            if self.action is not None:
                elements.append(
                    DesktopElement(
                        "9",
                        "API Key",
                        "Window",
                        plane=ElementPlane.DIALOG,
                        addressable=False,
                    )
                )
            return _observation(
                app,
                generation=1 if self.action is None else 2,
                elements=tuple(elements),
            )

        def execute(self, action, before):
            self.action = action
            return ActionReceipt(action, True, before.generation, "accepted")

        @staticmethod
        def close():
            pass

    with pytest.raises(AppDoctorFailure) as caught:
        run_app_doctor(
            settings,
            app="claude",
            draft_smoke=True,
            driver_factory=SensitiveAfterDriver,
        )

    assert caught.value.error_code == "POST_DRAFT_OBSERVATION_BLOCKED"


def test_draft_smoke_requires_exact_readback_not_substring_contains(tmp_path) -> None:
    settings = _settings(tmp_path)

    class AffixedValueDriver:
        def __init__(self, _profiles) -> None:
            self.action = None

        def observe(self, app):
            value = ""
            generation = 1
            if self.action is not None:
                value = f"prefix-{self.action.text}-suffix"
                generation = 2
            return _observation(
                app,
                generation=generation,
                elements=(
                    DesktopElement(
                        "2",
                        "Prompt",
                        "Edit",
                        value=value,
                        focused=True,
                        composer=True,
                        local_identity=_IDENTITY_A,
                    ),
                ),
            )

        def execute(self, action, before):
            self.action = action
            return ActionReceipt(action, True, before.generation, "accepted")

        @staticmethod
        def close():
            pass

    with pytest.raises(AppDoctorFailure) as caught:
        run_app_doctor(
            settings,
            app="claude",
            draft_smoke=True,
            driver_factory=AffixedValueDriver,
        )

    assert caught.value.error_code == "POST_DRAFT_VALUE_MISMATCH"


def test_observe_only_rejects_missing_observation_stats(tmp_path) -> None:
    settings = _settings(tmp_path)

    class IncompleteDriver:
        def __init__(self, _profiles) -> None:
            pass

        @staticmethod
        def observe(app):
            return DesktopObservation(
                app=app,
                generation=1,
                accessibility_text="not a structured UIA header",
                local_window_id="hwnd:123",
                elements=(DesktopElement("1", "Chat", "Button"),),
            )

        @staticmethod
        def close():
            pass

    with pytest.raises(AppDoctorFailure) as caught:
        run_app_doctor(
            settings,
            app="claude",
            draft_smoke=False,
            driver_factory=IncompleteDriver,
        )

    assert caught.value.error_code == "OBSERVATION_INCOMPLETE"


@pytest.mark.parametrize(
    ("fixture_name", "task", "expected_control"),
    [
        ("claude_uia_snapshot.json", "In Claude, click Chat and Cowork", "Chat and Cowork"),
        ("codex_uia_snapshot.json", "In Codex, click Projects", "Projects"),
    ],
)
def test_deidentified_ai_app_fixtures_keep_controls_but_drop_chat_content(
    fixture_name,
    task,
    expected_control,
) -> None:
    path = Path(__file__).parent / "fixtures" / fixture_name
    payload = json.loads(path.read_text(encoding="utf-8"))
    elements = tuple(DesktopElement(**item) for item in payload["elements"])
    observation = _observation(payload["app"], elements=elements)
    policy = DesktopSafetyPolicy("personal_trusted")

    assert max(len(element.name) for element in elements) > 500
    assert policy.inspect_observation(observation).disposition.value == "allow"
    planner_view = policy.planner_observation(observation, user_text=task)
    planner_serialized = json.dumps(
        planner_view.planner_context(max_chars=8000),
        ensure_ascii=False,
    )
    report_serialized = json.dumps(
        safe_observation_report(observation, policy=policy),
        ensure_ascii=False,
    )

    assert expected_control in planner_serialized
    assert "conversation-content-" not in planner_serialized
    assert "model-output-" not in planner_serialized
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz012345" not in planner_serialized
    assert "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_TOKEN" not in planner_serialized
    assert "conversation-content-" not in report_serialized
    assert "model-output-" not in report_serialized
