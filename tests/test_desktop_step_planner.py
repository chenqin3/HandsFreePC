from __future__ import annotations

import io
import json
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from handsfree_pc.desktop import step_planner
from handsfree_pc.desktop.protocol import (
    DesktopActionType,
    DesktopDecisionKind,
    DesktopElement,
    DesktopElementAction,
    DesktopExpectationKind,
    DesktopObservation,
    ElementPlane,
    visual_state_binding_token,
)
from handsfree_pc.desktop.step_planner import (
    ClaudeDesktopStepPlanner,
    CodexDesktopStepPlanner,
    DesktopPlannerError,
    DesktopPlannerUnavailable,
    _parse_decision_payload,
    _planner_image_png,
)


def _observation() -> DesktopObservation:
    return DesktopObservation(
        app="claude",
        generation=8,
        accessibility_text='0 name="Chat" control_type="TabItem"',
        window_title="Claude",
    )


def _decision_payload() -> dict[str, Any]:
    return {
        "kind": "action",
        "reason": "Select the requested tab",
        "app": "claude",
        "action": {
            "type": "click",
            "element_index": "0",
            "click_count": 1,
            "mouse_button": "left",
            "direction": None,
            "pages": None,
            "action_name": None,
            "text": None,
            "key": None,
            "value": None,
        },
        "expectation": {"kind": "element_selected", "text": "Chat"},
    }


def _png(width: int, height: int) -> bytes:
    from PIL import Image

    stream = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(stream, format="PNG")
    return stream.getvalue()


class FakeProcess:
    _next_pid = 8100

    def __init__(self, args, kwargs, *, payload, claude: bool) -> None:
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.args = list(args)
        self.kwargs = kwargs
        self.payload = payload
        self.claude = claude
        self.returncode = 0
        self.inputs: list[str | None] = []

    def communicate(self, *, input=None, timeout=None):
        self.inputs.append(input)
        if self.claude:
            stdout = json.dumps({"structured_output": self.payload}, ensure_ascii=False)
            return stdout, ""
        output_index = self.args.index("--output-last-message") + 1
        Path(self.args[output_index]).write_text(
            json.dumps(self.payload, ensure_ascii=False),
            encoding="utf-8",
        )
        return "", ""

    def poll(self):
        return self.returncode


class RecordingPopen:
    def __init__(self, payload, *, claude: bool = False) -> None:
        self.payload = payload
        self.claude = claude
        self.processes: list[FakeProcess] = []

    def __call__(self, args, **kwargs):
        process = FakeProcess(args, kwargs, payload=self.payload, claude=self.claude)
        self.processes.append(process)
        return process


def test_parser_rejects_non_object_unknown_fields_and_wrong_observed_app():
    observation = _observation()
    with pytest.raises(DesktopPlannerError, match="must be an object"):
        _parse_decision_payload("[]", observation=observation)

    payload = _decision_payload()
    payload["unexpected"] = True
    with pytest.raises(DesktopPlannerError, match="unknown fields"):
        _parse_decision_payload(payload, observation=observation)

    payload = _decision_payload()
    payload["app"] = "codex"
    with pytest.raises(DesktopPlannerError, match="observed application"):
        _parse_decision_payload(payload, observation=observation)


def test_parser_enforces_planner_action_subset_even_without_cli_schema_validation():
    payload = _decision_payload()
    payload["action"] = {
        "type": "click",
        "element_index": None,
        "x": 20,
        "y": 30,
    }

    with pytest.raises(DesktopPlannerError, match="planner action fields|coordinates"):
        _parse_decision_payload(payload, observation=_observation())


def test_parser_allows_x_y_only_on_current_visual_viewport() -> None:
    payload = _decision_payload()
    payload["action"]["x"] = 20
    payload["action"]["y"] = 30
    ordinary = DesktopObservation(
        app="claude",
        generation=8,
        accessibility_text="ordinary",
        elements=(DesktopElement("0", "Chat", "Button"),),
    )
    viewport = DesktopObservation(
        app="claude",
        generation=8,
        accessibility_text="visual",
        elements=(
            DesktopElement(
                "0",
                "Visual OCR viewport",
                "VisualViewport",
                plane=ElementPlane.CONTROL,
                visual_ocr=True,
                supported_actions=(DesktopElementAction.CLICK,),
            ),
        ),
    )

    with pytest.raises(DesktopPlannerError, match="VisualViewport"):
        _parse_decision_payload(payload, observation=ordinary)

    decision = _parse_decision_payload(payload, observation=viewport)
    assert decision.action is not None
    assert decision.action.x == 20
    assert decision.action.y == 30
    assert decision.expectation is not None
    assert decision.expectation.kind == DesktopExpectationKind.LAST_ACTION_VERIFIED


def test_parser_rebinds_screenshot_point_to_the_one_frame_bound_viewport() -> None:
    payload = _decision_payload()
    payload["action"]["element_index"] = "0"
    payload["action"]["x"] = 20
    payload["action"]["y"] = 30
    observation = DesktopObservation(
        app="claude",
        generation=8,
        accessibility_text="visual",
        elements=(
            DesktopElement("0", "render host", "Pane"),
            DesktopElement(
                "7",
                "Visual screenshot viewport",
                "VisualViewport",
                plane=ElementPlane.CONTROL,
                visual_ocr=True,
                supported_actions=(DesktopElementAction.CLICK,),
            ),
        ),
    )

    decision = _parse_decision_payload(payload, observation=observation)

    assert decision.action is not None
    assert decision.action.element_index == "7"
    assert decision.action.x == 20
    assert decision.action.y == 30
    assert decision.expectation is not None
    assert decision.expectation.kind == DesktopExpectationKind.LAST_ACTION_VERIFIED


def test_parser_maps_bounded_planner_image_point_to_full_capture_pixels() -> None:
    payload = _decision_payload()
    payload["action"]["x"] = 279
    payload["action"]["y"] = 92
    observation = DesktopObservation(
        app="claude",
        generation=8,
        accessibility_text="visual",
        screenshot_png=_png(2952, 1866),
        elements=(
            DesktopElement(
                "7",
                "Visual screenshot viewport",
                "VisualViewport",
                plane=ElementPlane.CONTROL,
                visual_ocr=True,
                supported_actions=(DesktopElementAction.CLICK,),
            ),
        ),
    )

    decision = _parse_decision_payload(payload, observation=observation)

    assert decision.action is not None
    assert decision.action.element_index == "7"
    assert decision.action.x == 402
    assert decision.action.y == 133


def test_planner_image_is_explicitly_bounded_to_the_coordinate_canvas() -> None:
    from PIL import Image

    payload = _planner_image_png(_png(2952, 1866))
    with Image.open(io.BytesIO(payload)) as image:
        assert image.size == (2048, 1295)


def test_parser_rebinds_armed_visual_search_text_to_the_viewport() -> None:
    payload = _decision_payload()
    payload["action"] = {
        "type": "type_text",
        "element_index": "0",
        "x": None,
        "y": None,
        "click_count": None,
        "mouse_button": None,
        "direction": None,
        "pages": None,
        "action_name": None,
        "text": "文件传输助手",
        "key": None,
        "value": None,
    }
    payload["expectation"] = {"kind": "focused_contains", "text": "文件传输助手"}
    observation = DesktopObservation(
        app="claude",
        generation=8,
        accessibility_text="visual",
        elements=(
            DesktopElement("0", "render host", "Pane"),
            DesktopElement(
                "7",
                "Visual screenshot viewport",
                "VisualViewport",
                plane=ElementPlane.CONTROL,
                visual_ocr=True,
                supported_actions=(
                    DesktopElementAction.CLICK,
                    DesktopElementAction.TYPE_TEXT,
                ),
            ),
        ),
    )

    decision = _parse_decision_payload(payload, observation=observation)

    assert decision.action is not None
    assert decision.action.element_index == "7"
    assert decision.action.text == "文件传输助手"
    assert decision.expectation is not None
    assert decision.expectation.kind == DesktopExpectationKind.LAST_ACTION_VERIFIED
    assert decision.expectation.text is None


def test_parser_rejects_press_key_targeting_visual_viewport() -> None:
    payload = _decision_payload()
    payload["action"] = {
        "type": "press_key",
        "element_index": "7",
        "key": "enter",
    }
    payload["expectation"] = {"kind": "search_submitted", "text": "文件传输助手"}
    observation = DesktopObservation(
        app="claude",
        generation=8,
        accessibility_text="visual",
        elements=(
            DesktopElement("0", "render host", "Pane"),
            DesktopElement(
                "7",
                "Visual screenshot viewport",
                "VisualViewport",
                plane=ElementPlane.CONTROL,
                visual_ocr=True,
                supported_actions=(
                    DesktopElementAction.CLICK,
                    DesktopElementAction.PRESS_KEY,
                ),
            ),
        ),
    )

    with pytest.raises(DesktopPlannerError, match="VisualViewport never supports press_key"):
        _parse_decision_payload(payload, observation=observation)


def test_parser_preserves_enter_on_focused_semantic_search_input() -> None:
    payload = _decision_payload()
    payload["action"] = {
        "type": "press_key",
        "element_index": "2",
        "key": "enter",
    }
    payload["expectation"] = {"kind": "search_submitted", "text": "文件传输助手"}
    observation = DesktopObservation(
        app="claude",
        generation=8,
        accessibility_text="semantic search",
        elements=(
            DesktopElement(
                "2",
                "Search",
                "SearchBox",
                value="文件传输助手",
                focused=True,
                editable=True,
                plane=ElementPlane.INPUT,
                supported_actions=(DesktopElementAction.PRESS_KEY,),
            ),
        ),
    )

    decision = _parse_decision_payload(payload, observation=observation)

    assert decision.action is not None
    assert decision.action.type == DesktopActionType.PRESS_KEY
    assert decision.action.element_index == "2"
    assert decision.action.key == "enter"
    assert decision.expectation is not None
    assert decision.expectation.kind == DesktopExpectationKind.SEARCH_SUBMITTED
    assert decision.expectation.text == "文件传输助手"


def test_parser_never_turns_a_repeat_visual_click_into_an_unrequested_enter() -> None:
    payload = _decision_payload()
    payload["action"] = {
        "type": "click",
        "element_index": "7",
        "x": 280,
        "y": 90,
        "click_count": 1,
        "mouse_button": "left",
    }
    payload["expectation"] = {
        "kind": "last_action_verified",
        "text": None,
    }
    observation = DesktopObservation(
        app="claude",
        generation=8,
        accessibility_text="visual filter results",
        screenshot_png=_png(1200, 800),
        elements=(
            DesktopElement(
                "7",
                "Visual screenshot viewport",
                "VisualViewport",
                plane=ElementPlane.CONTROL,
                visual_ocr=True,
                supported_actions=(DesktopElementAction.CLICK,),
            ),
        ),
    )

    decision = _parse_decision_payload(payload, observation=observation)

    assert decision.action is not None
    assert decision.action.type == DesktopActionType.CLICK
    assert decision.action.element_index == "7"
    assert decision.action.key is None
    assert decision.action.x == 280
    assert decision.action.y == 90
    assert decision.expectation is not None
    assert decision.expectation.kind == DesktopExpectationKind.LAST_ACTION_VERIFIED


def test_parser_preserves_unique_result_button_click_with_visual_results() -> None:
    label = "文件传输助手 在手机和电脑之间传输各类文件 前往"
    payload = _decision_payload()
    payload["action"] = {
        "type": "click",
        "element_index": "3",
        "click_count": 1,
        "mouse_button": "left",
    }
    payload["expectation"] = {
        "kind": "text_absent",
        "text": label,
    }
    observation = DesktopObservation(
        app="claude",
        generation=8,
        accessibility_text="visual search result ready",
        screenshot_png=_png(1200, 800),
        elements=(
            DesktopElement(
                "3",
                label,
                "Button",
                plane=ElementPlane.CONTROL,
                supported_actions=(DesktopElementAction.CLICK,),
            ),
            DesktopElement(
                "7",
                "Visual screenshot viewport",
                "VisualViewport",
                plane=ElementPlane.CONTROL,
                visual_ocr=True,
                supported_actions=(DesktopElementAction.CLICK,),
            ),
        ),
    )

    decision = _parse_decision_payload(payload, observation=observation)

    assert decision.action is not None
    assert decision.action.type == DesktopActionType.CLICK
    assert decision.action.element_index == "3"
    assert decision.action.key is None
    assert decision.expectation is not None
    assert decision.expectation.kind == DesktopExpectationKind.TEXT_ABSENT
    assert decision.expectation.text == label


def test_parser_preserves_viewport_click_outside_the_top_zone() -> None:
    payload = _decision_payload()
    payload["action"] = {
        "type": "click",
        "element_index": "7",
        "x": 600,
        "y": 400,
        "click_count": 1,
        "mouse_button": "left",
    }
    payload["expectation"] = {
        "kind": "last_action_verified",
        "text": None,
    }
    observation = DesktopObservation(
        app="claude",
        generation=8,
        accessibility_text="visual filter results",
        screenshot_png=_png(1200, 800),
        elements=(
            DesktopElement(
                "7",
                "Visual screenshot viewport",
                "VisualViewport",
                plane=ElementPlane.CONTROL,
                visual_ocr=True,
                supported_actions=(DesktopElementAction.CLICK,),
            ),
        ),
    )

    decision = _parse_decision_payload(payload, observation=observation)

    assert decision.action is not None
    assert decision.action.type == DesktopActionType.CLICK
    assert decision.action.element_index == "7"
    assert decision.action.x == 600
    assert decision.action.y == 400
    assert decision.action.key is None
    assert decision.expectation is not None
    assert decision.expectation.kind == DesktopExpectationKind.LAST_ACTION_VERIFIED


def test_parser_binds_visual_done_to_the_exact_inspected_screenshot() -> None:
    payload = {
        "kind": "done",
        "reason": "the requested rendered destination is visible",
        "app": "claude",
        "action": None,
        "expectation": {"kind": "focused_contains", "text": "Target"},
    }
    observation = DesktopObservation(
        app="claude",
        generation=9,
        accessibility_text="visual",
        screenshot_png=b"\x89PNG\r\n\x1a\nfixture",
        local_window_id="window-a",
        elements=(
            DesktopElement(
                "7",
                "Visual screenshot viewport",
                "VisualViewport",
                plane=ElementPlane.CONTROL,
                visual_ocr=True,
                supported_actions=(DesktopElementAction.CLICK,),
            ),
        ),
    )

    decision = _parse_decision_payload(payload, observation=observation)

    assert decision.kind == DesktopDecisionKind.DONE
    assert decision.expectation is not None
    assert decision.expectation.kind == DesktopExpectationKind.VISUAL_STATE_VERIFIED
    assert decision.expectation.text == visual_state_binding_token(observation)


def test_parser_rejects_model_authored_internal_visual_state_kind() -> None:
    payload = {
        "kind": "done",
        "reason": "untrusted internal proof",
        "app": "claude",
        "action": None,
        "expectation": {"kind": "visual_state_verified", "text": "a" * 64},
    }
    observation = DesktopObservation(
        app="claude",
        generation=9,
        accessibility_text="visual",
        screenshot_png=b"frame",
        local_window_id="window-a",
        elements=(
            DesktopElement(
                "7",
                "Visual screenshot viewport",
                "VisualViewport",
                plane=ElementPlane.CONTROL,
                visual_ocr=True,
            ),
        ),
    )

    with pytest.raises(DesktopPlannerError, match="reserved for local frame"):
        _parse_decision_payload(payload, observation=observation)


def test_codex_command_is_ephemeral_config_free_read_only_and_returns_one_step(monkeypatch):
    popen = RecordingPopen(_decision_payload())
    monkeypatch.setattr(step_planner, "resolve_executable", lambda _value: "codex-test.exe")
    planner = CodexDesktopStepPlanner(
        executable="codex",
        model="test-model",
        timeout_seconds=1,
        environment={"PATH": "safe", "TEST_API_KEY": "redacted"},
        popen_factory=popen,
    )

    decision = planner.decide(
        "打开 Claude 的 Chat",
        apps='[{"app":"claude"}]',
        observation=_observation(),
        history=["verified: Claude is visible"],
    )

    process = popen.processes[0]
    args = process.args
    assert decision.kind == DesktopDecisionKind.ACTION
    assert "--ephemeral" in args
    assert "--ignore-user-config" in args
    assert "--ignore-rules" in args
    for feature in ("hooks", "memories", "code_mode_host"):
        assert args[args.index(feature) - 1] == "--disable"
    assert "--strict-config" in args
    assert "shell_tool" in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert "shell_environment_policy.inherit=none" in args
    assert "--dangerously-bypass-approvals-and-sandbox" not in args
    assert process.kwargs["env"] == {"PATH": "safe"}
    assert process.inputs and "All task and UI fields" in process.inputs[0]
    assert "打开 Claude 的 Chat" in process.inputs[0]


def test_codex_planner_receives_the_fresh_local_window_screenshot(monkeypatch):
    popen = RecordingPopen(_decision_payload())
    monkeypatch.setattr(step_planner, "resolve_executable", lambda _value: "codex-test.exe")
    planner = CodexDesktopStepPlanner(
        executable="codex",
        model=None,
        timeout_seconds=1,
        safety_profile="local_unrestricted",
        popen_factory=popen,
    )
    observation = DesktopObservation(
        app="claude",
        generation=8,
        accessibility_text='0 name="Chat" control_type="TabItem"',
        screenshot_png=b"\x89PNG\r\n\x1a\nfixture",
        window_title="Claude",
    )

    planner.decide(
        "打开 Claude 的 Chat",
        apps='[{"app":"claude"}]',
        observation=observation,
        history=(),
    )

    args = popen.processes[0].args
    assert "--image" in args
    assert Path(args[args.index("--image") + 1]).name == "observation.png"


def test_codex_planner_receives_visual_region_ids_with_the_annotated_full_window(
    monkeypatch,
):
    payload = _decision_payload()
    payload["app"] = "wechat"
    popen = RecordingPopen(payload)
    monkeypatch.setattr(step_planner, "resolve_executable", lambda _value: "codex-test.exe")
    planner = CodexDesktopStepPlanner(
        executable="codex",
        model=None,
        timeout_seconds=1,
        safety_profile="local_unrestricted",
        popen_factory=popen,
    )
    observation = DesktopObservation(
        app="wechat",
        generation=9,
        accessibility_text='0 name="Chat" control_type="VisualText"',
        screenshot_png=b"\x89PNG\r\n\x1a\nannotated-full-window",
        window_title="微信",
        elements=(
            DesktopElement(
                index="0",
                name="Chat",
                control_type="VisualText",
                plane=ElementPlane.CONTROL,
                visual_ocr=True,
                supported_actions=(DesktopElementAction.CLICK,),
            ),
        ),
    )

    planner.decide(
        "打开 Chat",
        apps='[{"app":"wechat"}]',
        observation=observation,
        history=(),
    )

    process = popen.processes[0]
    assert "--image" in process.args
    assert '"visual_ocr": true' in process.inputs[0]
    assert "set-of-marks" in process.inputs[0]


def test_claude_text_only_planner_refuses_visual_regions(monkeypatch):
    monkeypatch.setattr(step_planner, "resolve_executable", lambda _value: "claude-test.exe")
    planner = ClaudeDesktopStepPlanner(
        executable="claude",
        model=None,
        timeout_seconds=1,
        safety_profile="local_unrestricted",
        popen_factory=RecordingPopen(_decision_payload(), claude=True),
    )
    observation = DesktopObservation(
        app="wechat",
        generation=1,
        accessibility_text="visual",
        screenshot_png=b"full-window",
        elements=(
            DesktopElement(
                index="0",
                name="Chat",
                control_type="VisualText",
                plane=ElementPlane.CONTROL,
                visual_ocr=True,
                supported_actions=(DesktopElementAction.CLICK,),
            ),
        ),
    )

    with pytest.raises(DesktopPlannerUnavailable, match="Codex planner"):
        planner.decide("打开 Chat", apps="wechat", observation=observation, history=())


def test_local_unrestricted_policy_supports_unscoped_cross_app_search():
    policy = step_planner._planner_policy("local_unrestricted")

    assert "choose any application" in policy
    assert "across listed applications" in policy
    assert "search for X" in policy
    assert "return done with app_visible" in policy
    assert "unsent draft/message/prompt" in policy
    assert "last_action_verified" in policy
    assert "scrollintoview" in policy
    assert "does not complete a user step" in policy
    assert "non-null supported_actions" in policy
    assert "direction matches scroll_axes" in policy
    assert "Never infer expand, collapse, scrollintoview, or scroll support" in policy
    assert "only when that exact operation is present" in policy
    assert "set-of-marks" in policy
    assert "Never invent an unmarked" in policy
    assert "proved a visible system caret" in policy
    assert "Do not\n  click that field again" in policy


def test_claude_command_explicitly_disables_all_tools_and_session_persistence(monkeypatch):
    popen = RecordingPopen(_decision_payload(), claude=True)
    monkeypatch.setattr(step_planner, "resolve_executable", lambda _value: "claude-test.exe")
    planner = ClaudeDesktopStepPlanner(
        executable="claude",
        model=None,
        timeout_seconds=1,
        popen_factory=popen,
    )

    decision = planner.decide(
        "打开 Chat",
        apps="claude",
        observation=_observation(),
        history=(),
    )

    process = popen.processes[0]
    args = process.args
    assert decision.kind == DesktopDecisionKind.ACTION
    for flag in (
        "--safe-mode",
        "--restricted",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-chrome",
        "--exclude-dynamic-system-prompt-sections",
        "--no-session-persistence",
    ):
        assert flag in args
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--disallowedTools") + 1] == "mcp__*"
    # Claude implements --json-schema through an internal structured-output
    # tool. Capping max turns at one terminates that protocol before the JSON
    # result is returned, even though every external tool is disabled.
    assert "--max-turns" not in args
    assert args[args.index("--output-format") + 1] == "json"
    system_prompt = args[args.index("--system-prompt") + 1]
    data_prompt = json.loads(process.inputs[0])
    assert "打开 Chat" not in system_prompt
    assert data_prompt["user_authored_task"] == "打开 Chat"
    assert data_prompt["observation"]["app"] == "claude"


def test_claude_text_only_prompt_does_not_claim_a_local_screenshot_was_supplied(monkeypatch):
    popen = RecordingPopen(_decision_payload(), claude=True)
    monkeypatch.setattr(step_planner, "resolve_executable", lambda _value: "claude-test.exe")
    planner = ClaudeDesktopStepPlanner(
        executable="claude",
        model=None,
        timeout_seconds=1,
        safety_profile="local_unrestricted",
        popen_factory=popen,
    )
    observation = DesktopObservation(
        app="claude",
        generation=8,
        accessibility_text='0 name="Chat" control_type="TabItem"',
        screenshot_png=b"local-window-png",
        window_title="Claude",
    )

    planner.decide(
        "打开 Chat",
        apps='[{"app":"claude"}]',
        observation=observation,
        history=(),
    )

    data_prompt = json.loads(popen.processes[0].inputs[0])
    assert data_prompt["observation"]["screenshot_available"] is False


def test_personal_trusted_planner_policy_allows_only_safe_navigation_bridges(monkeypatch):
    popen = RecordingPopen(_decision_payload(), claude=True)
    monkeypatch.setattr(step_planner, "resolve_executable", lambda _value: "claude-test.exe")
    planner = ClaudeDesktopStepPlanner(
        executable="claude",
        model=None,
        timeout_seconds=1,
        safety_profile="personal_trusted",
        popen_factory=popen,
    )

    planner.decide(
        "打开旧对话",
        apps="claude",
        observation=_observation(),
        history=(),
    )

    args = popen.processes[0].args
    system_prompt = args[args.index("--system-prompt") + 1]
    assert "ordinary enabled navigation control" in system_prompt
    assert "necessary intermediate" in system_prompt
    assert "Send, Submit, Delete, Upload, Install" in system_prompt
    assert "does not authorize invented content" in system_prompt
    assert "Strict navigation mode is enabled" not in system_prompt
    assert "explicitly named in\nuser_authored_task" not in system_prompt


def test_strict_and_personal_navigation_policies_are_mutually_exclusive():
    strict = step_planner._planner_policy("strict")
    personal = step_planner._planner_policy("personal_trusted")

    assert "Strict navigation mode is enabled" in strict
    assert "Never invent or infer an intermediate navigation target" in strict
    assert "Personal-trusted local navigation mode is enabled" not in strict
    assert "Personal-trusted local navigation mode is enabled" in personal
    assert "This paragraph replaces the strict navigation\nrule" in personal
    assert "Strict navigation mode is enabled" not in personal


def test_desktop_step_schema_avoids_combinators_rejected_by_cli_structured_output():
    schema = json.loads(step_planner.desktop_step_schema_path().read_text(encoding="utf-8"))

    def mappings(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from mappings(child)
        elif isinstance(value, list):
            for child in value:
                yield from mappings(child)

    assert all(
        {"allOf", "anyOf", "oneOf", "if", "then", "else"}.isdisjoint(mapping)
        for mapping in mappings(schema)
    )


@pytest.mark.parametrize(
    "path",
    [r"C:\Users\Example\secret.txt", "C:/Users/Example/secret.txt"],
)
def test_bounded_cli_error_redacts_both_windows_path_separator_styles(path: str):
    value = step_planner._bounded_cli_error(f'error: could not read "{path}"')

    assert "[LOCAL_PATH]" in value
    assert "Users" not in value


class NeverCompletesProcess:
    pid = 8200
    returncode = None

    def communicate(self, *, input=None, timeout=None):
        raise subprocess.TimeoutExpired("planner", timeout)

    def poll(self):
        return None


def test_cancel_event_stops_cli_planner_process(monkeypatch):
    stopped: list[NeverCompletesProcess] = []
    process = NeverCompletesProcess()
    monkeypatch.setattr(step_planner, "resolve_executable", lambda _value: "codex-test.exe")
    monkeypatch.setattr(step_planner, "_stop_process_tree", stopped.append)
    planner = CodexDesktopStepPlanner(
        executable="codex",
        model=None,
        timeout_seconds=5,
        popen_factory=lambda _args, **_kwargs: process,
    )
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(DesktopPlannerError, match="cancelled"):
        planner.decide(
            "打开 Chat",
            apps="claude",
            observation=_observation(),
            history=(),
            cancel_event=cancel_event,
        )

    assert stopped == [process]
