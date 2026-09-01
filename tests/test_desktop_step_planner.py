from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from handsfree_pc.desktop import step_planner
from handsfree_pc.desktop.protocol import DesktopDecisionKind, DesktopObservation
from handsfree_pc.desktop.step_planner import (
    ClaudeDesktopStepPlanner,
    CodexDesktopStepPlanner,
    DesktopPlannerError,
    _parse_decision_payload,
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


def test_local_unrestricted_policy_supports_unscoped_cross_app_search():
    policy = step_planner._planner_policy("local_unrestricted")

    assert "choose any application" in policy
    assert "across listed applications" in policy
    assert "search for X" in policy
    assert "return done with app_visible" in policy


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
