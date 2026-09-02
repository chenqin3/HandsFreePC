from __future__ import annotations

import json
from pathlib import Path

import pytest

from handsfree_pc.desktop import step_planner
from handsfree_pc.desktop.assistive.models import AssistiveDecisionKind
from handsfree_pc.desktop.assistive.planner import (
    AssistiveClaudeDesktopStepPlanner,
    AssistiveCodexDesktopStepPlanner,
    _parse_decision,
    assistive_step_schema_path,
)
from handsfree_pc.desktop.protocol import DesktopElement, DesktopObservation, ElementPlane
from handsfree_pc.desktop.step_planner import DesktopPlannerError

_DONE_PAYLOAD = {
    "kind": "done",
    "reason": "all final goals are visible",
    "app": None,
    "action": None,
}


class FakeProcess:
    _pid = 9000

    def __init__(self, args: list[str], kwargs: dict[str, object], *, claude: bool) -> None:
        type(self)._pid += 1
        self.pid = type(self)._pid
        self.args = list(args)
        self.kwargs = kwargs
        self.claude = claude
        self.returncode = 0
        self.inputs: list[str | None] = []

    def communicate(self, *, input=None, timeout=None):
        self.inputs.append(input)
        if self.claude:
            return json.dumps({"structured_output": _DONE_PAYLOAD}), ""
        output_index = self.args.index("--output-last-message") + 1
        Path(self.args[output_index]).write_text(json.dumps(_DONE_PAYLOAD), encoding="utf-8")
        return "", ""

    def poll(self):
        return self.returncode


class RecordingPopen:
    def __init__(self, *, claude: bool) -> None:
        self.claude = claude
        self.processes: list[FakeProcess] = []

    def __call__(self, args, **kwargs):
        process = FakeProcess(list(args), kwargs, claude=self.claude)
        self.processes.append(process)
        return process


def _observation() -> DesktopObservation:
    return DesktopObservation(
        app="claude",
        generation=4,
        accessibility_text='0 name="输入框" control_type="Edit"',
        window_title="Claude",
        local_window_id="window-1",
        elements=(
            DesktopElement(
                "0",
                "输入框",
                "Edit",
                focused=True,
                plane=ElementPlane.INPUT,
            ),
        ),
    )


def test_assistive_schema_has_no_proof_expectation_field() -> None:
    schema = json.loads(assistive_step_schema_path().read_text(encoding="utf-8"))

    assert "expectation" not in json.dumps(schema, ensure_ascii=False).casefold()
    assert schema["required"] == ["kind", "reason", "app", "action"]
    action_object = schema["properties"]["action"]["anyOf"][1]
    assert set(action_object["required"]) == set(action_object["properties"])


def test_assistive_parser_rejects_an_expectation_instead_of_rewriting_it() -> None:
    payload = dict(_DONE_PAYLOAD)
    payload["expectation"] = {"kind": "last_action_verified"}

    with pytest.raises(DesktopPlannerError, match="exactly"):
        _parse_decision(payload, observation=None)


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "done", "reason": "done", "action": None},
        {"kind": "observe", "reason": "look", "app": None, "action": None},
        {"kind": "screenshot", "reason": "look", "app": None, "action": None},
        {
            "kind": "done",
            "reason": "done",
            "app": None,
            "action": {"type": "press_key", "element_index": "0", "key": "enter"},
        },
    ],
)
def test_assistive_parser_rejects_cross_field_schema_semantic_mismatches(payload) -> None:
    with pytest.raises(DesktopPlannerError):
        _parse_decision(payload, observation=_observation())


@pytest.mark.parametrize("kind", ["done", "fail"])
def test_assistive_parser_clears_an_echoed_app_on_terminal_decisions(kind) -> None:
    # The schema forces `app` to be present, so the model routinely echoes the
    # current app on done/fail; that is tolerated and the app is cleared.
    payload = {"kind": kind, "reason": "terminal", "app": "chrome", "action": None}

    decision = _parse_decision(payload, observation=_observation())

    assert decision.kind == AssistiveDecisionKind(kind)
    assert decision.app is None


def test_assistive_parser_preserves_exact_text_action_payload() -> None:
    payload = {
        "kind": "action",
        "reason": "type the exact user-authored text",
        "app": "claude",
        "action": {
            "type": "type_text",
            "element_index": "0",
            "text": "  原文中的前后空格  ",
        },
    }

    decision = _parse_decision(payload, observation=_observation())

    assert decision.kind == AssistiveDecisionKind.ACTION
    assert decision.action is not None
    assert decision.action.text == "  原文中的前后空格  "


@pytest.mark.parametrize(
    ("planner_type", "executable", "claude"),
    [
        (AssistiveCodexDesktopStepPlanner, "codex", False),
        (AssistiveClaudeDesktopStepPlanner, "claude", True),
    ],
)
def test_assistive_cli_planners_pass_the_explicit_model_argument(
    monkeypatch,
    planner_type,
    executable: str,
    claude: bool,
) -> None:
    popen = RecordingPopen(claude=claude)
    monkeypatch.setattr(step_planner, "resolve_executable", lambda _value: f"{executable}.exe")
    planner = planner_type(
        executable=executable,
        model="fast-model-id",
        timeout_seconds=1,
        safety_profile="local_unrestricted",
        environment={"PATH": "safe"},
        popen_factory=popen,
    )

    decision = planner.decide(
        json.dumps(
            {
                "goals": [{"kind": "app_foreground", "target": "claude"}],
                "forbid_submit": False,
                "side_effect": False,
                "raw_text": "切换到 Claude",
            }
        ),
        apps="[]",
        observation=None,
        history=(),
    )

    args = popen.processes[0].args
    assert decision.kind == AssistiveDecisionKind.DONE
    assert args[args.index("--model") + 1] == "fast-model-id"
    assert '"expectation":' not in (popen.processes[0].inputs[0] or "").casefold()


def test_assistive_parser_maps_visual_points_from_planner_image_to_capture(monkeypatch) -> None:
    from handsfree_pc.desktop.assistive import planner as planner_module

    monkeypatch.setattr(
        planner_module,
        "_planner_point_to_capture",
        lambda x, y, *, observation: (x * 2, y * 2),
    )
    payload = {
        "kind": "action",
        "reason": "click the visible chat entry",
        "app": "claude",
        "action": {"type": "click", "element_index": "0", "x": 10, "y": 20},
    }

    decision = _parse_decision(payload, observation=_observation())

    assert decision.action is not None
    assert (decision.action.x, decision.action.y) == (20, 40)


def test_codex_assistive_planner_passes_reasoning_effort(monkeypatch) -> None:
    popen = RecordingPopen(claude=False)
    monkeypatch.setattr(step_planner, "resolve_executable", lambda _value: "codex.exe")
    planner = AssistiveCodexDesktopStepPlanner(
        executable="codex",
        model=None,
        timeout_seconds=1,
        safety_profile="local_unrestricted",
        environment={"PATH": "safe"},
        popen_factory=popen,
        reasoning_effort="low",
    )

    planner.decide(
        json.dumps(
            {
                "goals": [{"kind": "free_form", "target": "看看"}],
                "forbid_submit": False,
                "side_effect": False,
                "raw_text": "看看",
            }
        ),
        apps="[]",
        observation=None,
        history=(),
    )

    args = popen.processes[0].args
    position = args.index('model_reasoning_effort="low"')
    assert args[position - 1] == "-c"
    assert "--model" not in args


def test_assistive_planner_rejects_unknown_reasoning_effort() -> None:
    with pytest.raises(ValueError, match="reasoning_effort"):
        AssistiveCodexDesktopStepPlanner(
            executable="codex",
            model=None,
            timeout_seconds=1,
            reasoning_effort="turbo",
        )


def _obs_for_planner():
    from handsfree_pc.desktop.protocol import (
        DesktopElement,
        DesktopElementAction,
        DesktopObservation,
        ElementPlane,
    )

    return DesktopObservation(
        app="weixin-1",
        generation=3,
        accessibility_text="wechat",
        window_title="微信",
        process_name="Weixin.exe",
        local_window_id="hwnd:9",
        elements=(
            DesktopElement(
                "5",
                "文件传输助手",
                "VisualText",
                plane=ElementPlane.CONTROL,
                editable=False,
                visual_ocr=True,
                local_identity="a" * 64,
                supported_actions=(DesktopElementAction.CLICK,),
            ),
        ),
        screenshot_png=b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
    )


def test_planner_click_tolerates_a_stray_action_name_field():
    from handsfree_pc.desktop.assistive.planner import _parse_decision
    from handsfree_pc.desktop.protocol import DesktopActionType

    payload = {
        "kind": "action",
        "reason": "click the search box",
        "app": "weixin-1",
        "action": {
            "type": "click",
            "element_index": "5",
            "x": None,
            "y": None,
            "click_count": None,
            "mouse_button": None,
            "direction": None,
            "pages": None,
            "action_name": "clickfocus",
            "text": None,
            "key": None,
            "value": None,
        },
    }
    decision = _parse_decision(payload, observation=_obs_for_planner())
    assert decision.action.type == DesktopActionType.CLICK
    assert decision.action.action_name is None
    assert decision.action.element_index == "5"


def test_planner_done_with_echoed_app_is_accepted():
    from handsfree_pc.desktop.assistive.models import AssistiveDecisionKind
    from handsfree_pc.desktop.assistive.planner import _parse_decision

    payload = {"kind": "done", "reason": "goal reached", "app": "weixin-1", "action": None}
    decision = _parse_decision(payload, observation=_obs_for_planner())
    assert decision.kind == AssistiveDecisionKind.DONE
    assert decision.app is None


def test_planner_click_on_ocr_target_drops_raw_point():
    from handsfree_pc.desktop.assistive.planner import _parse_decision

    payload = {
        "kind": "action",
        "reason": "click the OCR result",
        "app": "weixin-1",
        "action": {
            "type": "click",
            "element_index": "5",
            "x": 1200,
            "y": 800,
            "click_count": None,
            "mouse_button": None,
            "direction": None,
            "pages": None,
            "action_name": None,
            "text": None,
            "key": None,
            "value": None,
        },
    }
    decision = _parse_decision(payload, observation=_obs_for_planner())
    assert decision.action.x is None and decision.action.y is None
    assert decision.action.element_index == "5"
