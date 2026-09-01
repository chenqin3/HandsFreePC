from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from handsfree_pc.desktop import workmap_selector
from handsfree_pc.desktop.workmap_selector import CodexWorkMapSelector, WorkMapSelectorError

FIRST_ID = "wm-11111111111111111111"
SECOND_ID = "wm-22222222222222222222"
FIRST_PATH_ID = "pc-aaaaaaaaaaaaaaaaaaaa"
SECOND_PATH_ID = "pc-bbbbbbbbbbbbbbbbbbbb"


def _candidates():
    return (
        {
            "target_id": FIRST_ID,
            "display_name": "招聘资料库",
            "project_name": "招聘项目",
            "summary": r"内部材料位于 G:\private\recruitment，token=secret-value",
            "score": 0.73,
            "has_local_root": True,
            "target_available": True,
        },
        {
            "target_id": SECOND_ID,
            "display_name": "研究资料库",
            "project_name": "研究项目",
            "summary": "研究材料",
            "score": 0.71,
            "has_local_root": True,
            "target_available": True,
        },
    )


def _path_candidates():
    return (
        {
            "candidate_id": FIRST_PATH_ID,
            "basename": "年度报告甲",
            "type": "directory",
            "score": 0.94,
            "parent_path": r"G:\must-never-leak\private-parent",
        },
        {
            "candidate_id": SECOND_PATH_ID,
            "basename": "年度报告乙",
            "type": "directory",
            "score": 0.94,
            "parent_path": r"G:\must-never-leak\private-parent",
        },
    )


class FakeProcess:
    pid = 7711

    def __init__(self, args, kwargs, *, selected_id, returncode=0) -> None:
        self.args = list(args)
        self.kwargs = kwargs
        self.selected_id = selected_id
        self.returncode = returncode
        self.inputs: list[str | None] = []
        self.schema: dict[str, object] | None = None

    def communicate(self, *, input=None, timeout=None):
        self.inputs.append(input)
        schema_index = self.args.index("--output-schema") + 1
        self.schema = json.loads(Path(self.args[schema_index]).read_text(encoding="utf-8"))
        if self.returncode == 0:
            output_index = self.args.index("--output-last-message") + 1
            Path(self.args[output_index]).write_text(
                json.dumps({"candidate_id": self.selected_id}),
                encoding="utf-8",
            )
        return "", ""

    def poll(self):
        return self.returncode


class RecordingPopen:
    def __init__(self, selected_id=FIRST_ID, *, returncode=0) -> None:
        self.selected_id = selected_id
        self.returncode = returncode
        self.processes: list[FakeProcess] = []

    def __call__(self, args, **kwargs):
        process = FakeProcess(
            args,
            kwargs,
            selected_id=self.selected_id,
            returncode=self.returncode,
        )
        self.processes.append(process)
        return process


def test_codex_selector_is_path_free_structured_and_fully_isolated(monkeypatch) -> None:
    popen = RecordingPopen()
    monkeypatch.setattr(workmap_selector, "resolve_executable", lambda _value: "codex-test.exe")
    selector = CodexWorkMapSelector(
        executable="codex",
        model="test-model",
        timeout_seconds=1,
        environment={"PATH": "safe", "TEST_API_KEY": "must-not-leak"},
        popen_factory=popen,
    )

    selected = selector.select("打开我平时招人的资料库", _candidates())

    assert selected == FIRST_ID
    process = popen.processes[0]
    args = process.args
    assert "--ephemeral" in args
    assert "--ignore-user-config" in args
    assert "--ignore-rules" in args
    assert "--strict-config" in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert "shell_environment_policy.inherit=none" in args
    assert 'web_search="disabled"' in args
    assert "agents.enabled=false" in args
    for feature in (
        "shell_tool",
        "unified_exec",
        "apps",
        "plugins",
        "multi_agent",
        "computer_use",
        "browser_use",
        "in_app_browser",
        "hooks",
        "memories",
        "code_mode_host",
    ):
        assert args[args.index(feature) - 1] == "--disable"
    assert "--dangerously-bypass-approvals-and-sandbox" not in args
    assert process.kwargs["env"] == {"PATH": "safe"}

    assert process.schema is not None
    assert set(process.schema["properties"]) == {"candidate_id"}
    assert process.schema["additionalProperties"] is False
    assert process.schema["properties"]["candidate_id"]["enum"] == [
        FIRST_ID,
        SECOND_ID,
        None,
    ]
    prompt = process.inputs[0]
    assert prompt is not None
    prompt_payload = json.loads(prompt)
    assert prompt_payload["user_authored_request"] == "打开我平时招人的资料库"
    assert "candidate_hints_untrusted" in prompt_payload
    assert "untrusted" in prompt_payload["policy"]
    assert r"G:\private" not in prompt
    assert "secret-value" not in prompt
    assert "[local path]" in prompt
    assert all(r"G:\private" not in value for value in args)


def test_codex_path_child_selector_exposes_only_basename_type_score_and_random_id(
    monkeypatch,
) -> None:
    popen = RecordingPopen(SECOND_PATH_ID)
    monkeypatch.setattr(workmap_selector, "resolve_executable", lambda _value: "codex-test.exe")
    selector = CodexWorkMapSelector(
        executable="codex",
        model=None,
        timeout_seconds=1,
        environment={"PATH": "safe", "SECRET_TOKEN": "must-not-leak"},
        popen_factory=popen,
    )

    selected = selector.select_path_child(
        "打开招聘数据库下面的年度报告",
        "年度报告",
        _path_candidates(),
    )

    assert selected == SECOND_PATH_ID
    process = popen.processes[0]
    assert process.schema is not None
    assert process.schema["properties"]["candidate_id"]["enum"] == [
        FIRST_PATH_ID,
        SECOND_PATH_ID,
        None,
    ]
    prompt = process.inputs[0]
    assert prompt is not None
    prompt_payload = json.loads(prompt)
    assert prompt_payload["spoken_component"] == "年度报告"
    assert prompt_payload["user_authored_request"] == "打开招聘数据库下面的年度报告"
    assert all(
        set(candidate) == {"candidate_id", "basename", "type", "score"}
        for candidate in prompt_payload["candidate_hints_untrusted"]
    )
    assert r"G:\must-never-leak" not in prompt
    assert "private-parent" not in prompt
    assert process.kwargs["env"] == {"PATH": "safe"}
    args = process.args
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert "shell_environment_policy.inherit=none" in args
    for feature in ("shell_tool", "unified_exec", "multi_agent", "computer_use"):
        assert args[args.index(feature) - 1] == "--disable"
    assert all(r"G:\must-never-leak" not in value for value in args)


def test_codex_selector_rejects_unknown_id_null_and_cli_failure(monkeypatch) -> None:
    monkeypatch.setattr(workmap_selector, "resolve_executable", lambda _value: "codex-test.exe")

    forged = CodexWorkMapSelector(
        executable="codex",
        model=None,
        timeout_seconds=1,
        popen_factory=RecordingPopen("wm-99999999999999999999"),
    )
    with pytest.raises(WorkMapSelectorError, match="unknown candidate"):
        forged.select("打开资料库", _candidates())

    null_selector = CodexWorkMapSelector(
        executable="codex",
        model=None,
        timeout_seconds=1,
        popen_factory=RecordingPopen(None),
    )
    assert null_selector.select("打开资料库", _candidates()) is None

    failed = CodexWorkMapSelector(
        executable="codex",
        model=None,
        timeout_seconds=1,
        popen_factory=RecordingPopen(returncode=7),
    )
    with pytest.raises(WorkMapSelectorError, match="exited with code 7"):
        failed.select("打开资料库", _candidates())


def test_selector_rejects_unavailable_or_non_bounded_candidate_sets(monkeypatch) -> None:
    monkeypatch.setattr(workmap_selector, "resolve_executable", lambda _value: "codex-test.exe")
    selector = CodexWorkMapSelector(
        executable="codex",
        model=None,
        timeout_seconds=1,
        popen_factory=RecordingPopen(),
    )

    with pytest.raises(WorkMapSelectorError, match="between two and five"):
        selector.select("打开资料库", _candidates()[:1])

    unavailable = [dict(candidate) for candidate in _candidates()]
    unavailable[1]["target_available"] = False
    with pytest.raises(WorkMapSelectorError, match="locally available"):
        selector.select("打开资料库", unavailable)


def test_codex_selector_honors_cancellation_before_waiting_for_cli(monkeypatch) -> None:
    popen = RecordingPopen()
    stopped: list[object] = []
    monkeypatch.setattr(workmap_selector, "resolve_executable", lambda _value: "codex-test.exe")
    monkeypatch.setattr(workmap_selector, "_stop_process_tree", stopped.append)
    selector = CodexWorkMapSelector(
        executable="codex",
        model=None,
        timeout_seconds=1,
        popen_factory=popen,
    )
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(WorkMapSelectorError, match="cancelled"):
        selector.select("打开资料库", _candidates(), cancel_event=cancelled)

    assert stopped == popen.processes
