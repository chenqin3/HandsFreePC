from __future__ import annotations

import json
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import pytest

import handsfree_pc.computer_control as computer_control
from handsfree_pc.computer_control import (
    CodexComputerController,
    ComputerControlResult,
    Controller,
)

THREAD_ID = "019d0000-0000-7000-8000-000000000001"
OTHER_THREAD_ID = "019d0000-0000-7000-8000-000000000002"


def jsonl(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


def successful_jsonl(thread_id: str = THREAD_ID) -> str:
    return jsonl(
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 4}},
    )


@dataclass(slots=True)
class ProcessSpec:
    stdout: str
    message: str | None = "VERIFIED_COMPLETION: verified completion"
    returncode: int = 0
    timeouts_before_completion: int = 0
    never_complete: bool = False


class FakeProcess:
    def __init__(self, args: list[str], spec: ProcessSpec) -> None:
        self.args = args
        self.spec = spec
        self.returncode: int | None = None
        self.inputs: list[str] = []
        self.terminated = False
        self.killed = False
        self.started = threading.Event()

    def _output_path(self) -> Path:
        index = self.args.index("--output-last-message")
        return Path(self.args[index + 1])

    def communicate(self, input=None, timeout=None):
        self.started.set()
        if input is not None:
            self.inputs.append(input)
        if self.returncode is not None:
            return self.spec.stdout, "provider diagnostics must stay private"
        if self.spec.never_complete or self.spec.timeouts_before_completion > 0:
            if self.spec.timeouts_before_completion > 0:
                self.spec.timeouts_before_completion -= 1
            time.sleep(min(float(timeout or 0.001), 0.01))
            raise subprocess.TimeoutExpired(self.args, timeout or 0)
        if self.spec.message is not None:
            self._output_path().write_text(self.spec.message, encoding="utf-8")
        self.returncode = self.spec.returncode
        return self.spec.stdout, "provider diagnostics must stay private"

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        del timeout
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.args, 0)
        return self.returncode


class FakePopenFactory:
    def __init__(self, *specs: ProcessSpec) -> None:
        self.specs = deque(specs)
        self.calls: list[tuple[list[str], dict]] = []
        self.processes: list[FakeProcess] = []

    def __call__(self, args, **kwargs):
        assert self.specs, "unexpected Popen call"
        process = FakeProcess(list(args), self.specs.popleft())
        self.calls.append((list(args), kwargs))
        self.processes.append(process)
        return process


@pytest.fixture
def fake_executable(monkeypatch):
    monkeypatch.setattr(computer_control.shutil, "which", lambda _value: "C:\\fake\\codex.exe")


def test_first_run_preserves_user_config_and_builds_safe_computer_use_prompt(
    fake_executable, tmp_path
):
    factory = FakePopenFactory(
        ProcessSpec(successful_jsonl(), message="VERIFIED_COMPLETION: verified first task")
    )
    environment = {
        "PATH": "safe-path",
        "CODEX_HOME": "safe-codex-home",
        "OPENAI_API_KEY": "remove-me",
        "GITHUB_TOKEN": "remove-me-too",
        "MY_PASSWORD": "also-remove",
    }
    controller = CodexComputerController(
        model="gpt-test",
        working_directory=tmp_path,
        environment=environment,
        popen_factory=factory,
    )

    result = controller.run("Open the configured demo app and verify its title")

    assert isinstance(result, ComputerControlResult)
    assert isinstance(controller, Controller)
    assert result.success is True
    assert result.message == "VERIFIED_COMPLETION: verified first task"
    assert result.session_id == THREAD_ID
    args, kwargs = factory.calls[0]
    assert args[:2] == ["C:\\fake\\codex.exe", "exec"]
    assert "--json" in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert "--ignore-rules" in args
    assert "--skip-git-repo-check" in args
    assert "--ignore-user-config" not in args
    assert args[args.index("--model") + 1] == "gpt-test"
    assert args[-1] == "-"
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["env"] == {"PATH": "safe-path", "CODEX_HOME": "safe-codex-home"}
    assert kwargs["stdin"] is subprocess.PIPE
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    expected_flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0) if computer_control.os.name == "nt" else 0
    )
    assert kwargs["creationflags"] == expected_flags
    prompt = factory.processes[0].inputs[0]
    for required in (
        "computer-use skill",
        "node_repl",
        "@oai/sky",
        "UI Automation",
        "exactly one atomic UI action",
        "postcondition",
        "NEEDS_CONFIRMATION",
        "VERIFIED_COMPLETION",
        "FAILURE",
        "Windows Run dialog",
        "other MCP/plugin tools",
        "ChatGPT/Codex UI",
        "authentication dialogs",
        "password managers",
        "UAC",
    ):
        assert required in prompt
    assert "Open the configured demo app" in prompt


def test_second_run_resumes_exact_thread_and_keeps_one_agent(fake_executable, tmp_path):
    factory = FakePopenFactory(
        ProcessSpec(successful_jsonl(), message="VERIFIED_COMPLETION: first done"),
        ProcessSpec(successful_jsonl(), message="VERIFIED_COMPLETION: second done"),
    )
    controller = CodexComputerController(working_directory=tmp_path, popen_factory=factory)

    first = controller.execute("first")
    second = controller.run("second")

    assert first.success and second.success
    assert second.session_id == THREAD_ID
    args = factory.calls[1][0]
    resume_index = args.index("resume")
    assert args[resume_index + 1] == THREAD_ID
    assert args[-1] == "-"
    assert "--ignore-user-config" not in args
    prompt = factory.processes[1].inputs[0]
    assert "Continue the same HandsFreePC computer-control session" in prompt
    assert "fresh action-time confirmation" in prompt
    assert "second" in prompt


def test_nonzero_exit_is_failure_and_does_not_leak_stderr(fake_executable, tmp_path):
    factory = FakePopenFactory(
        ProcessSpec(
            successful_jsonl(),
            message="must not be trusted",
            returncode=7,
        )
    )
    controller = CodexComputerController(working_directory=tmp_path, popen_factory=factory)

    result = controller.run("private spoken instruction")

    assert result.success is False
    assert result.returncode == 7
    assert "code 7" in result.message
    assert "provider diagnostics" not in result.message
    assert "private spoken instruction" not in result.message
    assert controller.session_id is None


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("not-json\n", "invalid JSONL"),
        (jsonl({"type": "turn.completed"}), "did not begin with thread.started"),
        (
            jsonl({"type": "thread.started", "thread_id": "not-a-uuid"}),
            "invalid thread_id",
        ),
        (
            jsonl(
                {"type": "thread.started", "thread_id": THREAD_ID},
                {"type": "thread.started", "thread_id": OTHER_THREAD_ID},
            ),
            "conflicting thread identifiers",
        ),
        (jsonl({"thread_id": THREAD_ID}), "had no event type"),
        (
            jsonl(
                {"type": "thread.started", "thread_id": THREAD_ID},
                {"type": "item.completed"},
            ),
            "did not end in exactly one turn.completed",
        ),
        (
            jsonl(
                {"type": "thread.started", "thread_id": THREAD_ID},
                {"type": "turn.completed"},
                {"type": "turn.completed"},
            ),
            "did not end in exactly one turn.completed",
        ),
    ],
)
def test_initial_run_strictly_validates_jsonl_and_thread_id(
    fake_executable, tmp_path, stdout, expected
):
    factory = FakePopenFactory(ProcessSpec(stdout, message="untrusted"))
    controller = CodexComputerController(working_directory=tmp_path, popen_factory=factory)

    result = controller.run("test")

    assert result.success is False
    assert expected in result.message
    assert controller.session_id is None


def test_resume_rejects_different_thread_id_without_losing_original(fake_executable, tmp_path):
    factory = FakePopenFactory(
        ProcessSpec(successful_jsonl(), message="VERIFIED_COMPLETION: first"),
        ProcessSpec(successful_jsonl(OTHER_THREAD_ID), message="wrong session"),
    )
    controller = CodexComputerController(working_directory=tmp_path, popen_factory=factory)
    assert controller.run("first").success

    result = controller.run("second")

    assert result.success is False
    assert "different thread identifier" in result.message
    assert result.session_id == THREAD_ID
    assert controller.session_id == THREAD_ID


@pytest.mark.parametrize(
    ("message", "success", "expected"),
    [
        ("VERIFIED_COMPLETION: target window now shows the requested file", True, "target window"),
        ("NEEDS_CONFIRMATION: send the prepared message", False, "NEEDS_CONFIRMATION"),
        ("FAILURE: target window was ambiguous", False, "FAILURE"),
        ("I think it probably worked", False, "no valid verified-completion status"),
        ("VERIFIED_COMPLETION:", False, "no valid verified-completion status"),
        (
            "VERIFIED_COMPLETION: one line\nextra unstructured text",
            False,
            "no valid verified-completion status",
        ),
        (
            f"NEEDS_CONFIRMATION: {'x' * 161}",
            False,
            "confirmation description exceeded the safe length",
        ),
        (
            "NEEDS_CONFIRMATION: send\tthe form",
            False,
            "no valid verified-completion status",
        ),
    ],
)
def test_final_message_requires_explicit_verified_status(
    fake_executable, tmp_path, message, success, expected
):
    factory = FakePopenFactory(ProcessSpec(successful_jsonl(), message=message))
    controller = CodexComputerController(working_directory=tmp_path, popen_factory=factory)

    result = controller.run("test")

    assert result.success is success
    assert expected in result.message


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (None, "did not create"),
        ("   \n", "was empty"),
    ],
)
def test_last_message_file_is_required(fake_executable, tmp_path, message, expected):
    factory = FakePopenFactory(ProcessSpec(successful_jsonl(), message=message))
    controller = CodexComputerController(working_directory=tmp_path, popen_factory=factory)

    result = controller.run("test")

    assert result.success is False
    assert expected in result.message
    assert controller.session_id is None


def test_timeout_terminates_process_and_reports_timeout(fake_executable, tmp_path):
    factory = FakePopenFactory(
        ProcessSpec(successful_jsonl(), never_complete=True),
    )
    ticks = iter((0.0, 0.0, 2.0))
    controller = CodexComputerController(
        timeout_seconds=1.0,
        poll_interval=0.01,
        working_directory=tmp_path,
        popen_factory=factory,
        monotonic=lambda: next(ticks),
    )

    result = controller.run("wait forever")

    assert result.success is False
    assert result.timed_out is True
    assert result.cancelled is False
    assert factory.processes[0].terminated is True


def test_cancel_cooperatively_stops_active_popen(fake_executable, tmp_path):
    factory = FakePopenFactory(ProcessSpec(successful_jsonl(), never_complete=True))
    controller = CodexComputerController(
        timeout_seconds=30,
        poll_interval=0.01,
        working_directory=tmp_path,
        popen_factory=factory,
    )
    result_holder: list[ComputerControlResult] = []
    worker = threading.Thread(target=lambda: result_holder.append(controller.run("long task")))
    worker.start()
    deadline = time.monotonic() + 2
    while not factory.processes and time.monotonic() < deadline:
        time.sleep(0.005)
    assert factory.processes
    assert factory.processes[0].started.wait(timeout=2)

    assert controller.cancel() is True
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert len(result_holder) == 1
    assert result_holder[0].success is False
    assert result_holder[0].cancelled is True
    assert result_holder[0].timed_out is False
    assert factory.processes[0].terminated is True
    assert controller.cancel() is False


def test_stop_process_terminates_and_kills_descendant_tree(monkeypatch):
    calls: list[tuple[str, int]] = []

    class TreeNode:
        def __init__(self, pid, children=()):
            self.pid = pid
            self._children = list(children)

        def children(self, recursive=True):
            assert recursive
            return self._children

        def terminate(self):
            calls.append(("terminate", self.pid))

        def kill(self):
            calls.append(("kill", self.pid))

    child = TreeNode(202)
    root = TreeNode(101, [child])
    monkeypatch.setattr(computer_control.psutil, "Process", lambda pid: root)
    waits = iter([([], [root, child]), ([root, child], [])])
    monkeypatch.setattr(computer_control.psutil, "wait_procs", lambda items, timeout: next(waits))

    class PopenDouble:
        pid = 101

        def __init__(self):
            self.returncode = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            del timeout
            return self.returncode

    process = PopenDouble()
    CodexComputerController._stop_process(process)

    assert calls == [
        ("terminate", 101),
        ("terminate", 202),
        ("kill", 101),
        ("kill", 202),
    ]


def test_external_cancel_event_is_honored_before_starting_process(fake_executable, tmp_path):
    factory = FakePopenFactory(ProcessSpec(successful_jsonl(), never_complete=True))
    cancel_event = threading.Event()
    cancel_event.set()
    controller = CodexComputerController(working_directory=tmp_path, popen_factory=factory)

    result = controller.run("cancelled", cancel_event=cancel_event)

    assert result.cancelled is True
    assert factory.calls == []
    assert factory.processes == []


def test_close_during_executable_discovery_prevents_late_spawn(
    fake_executable, monkeypatch, tmp_path
):
    factory = FakePopenFactory(ProcessSpec(successful_jsonl()))
    controller = CodexComputerController(working_directory=tmp_path, popen_factory=factory)
    discovery_started = threading.Event()
    release_discovery = threading.Event()

    def delayed_resolve():
        discovery_started.set()
        assert release_discovery.wait(timeout=2)
        return "C:\\fake\\codex.exe"

    monkeypatch.setattr(controller, "_resolve_executable", delayed_resolve)
    results: list[ComputerControlResult] = []
    run_thread = threading.Thread(target=lambda: results.append(controller.run("late task")))
    run_thread.start()
    assert discovery_started.wait(timeout=2)

    close_thread = threading.Thread(target=controller.close)
    close_thread.start()
    deadline = time.monotonic() + 2
    while not controller._closed and time.monotonic() < deadline:
        time.sleep(0.005)
    assert controller._closed
    release_discovery.set()
    run_thread.join(timeout=2)
    close_thread.join(timeout=2)

    assert not run_thread.is_alive()
    assert not close_thread.is_alive()
    assert len(results) == 1
    assert results[0].success is False
    assert "closed" in results[0].message
    assert factory.calls == []


def test_cancel_during_executable_discovery_is_latched_before_spawn(
    fake_executable, monkeypatch, tmp_path
):
    factory = FakePopenFactory(ProcessSpec(successful_jsonl()))
    controller = CodexComputerController(working_directory=tmp_path, popen_factory=factory)
    discovery_started = threading.Event()
    release_discovery = threading.Event()

    def delayed_resolve():
        discovery_started.set()
        assert release_discovery.wait(timeout=2)
        return "C:\\fake\\codex.exe"

    monkeypatch.setattr(controller, "_resolve_executable", delayed_resolve)
    results: list[ComputerControlResult] = []
    run_thread = threading.Thread(target=lambda: results.append(controller.run("late task")))
    run_thread.start()
    assert discovery_started.wait(timeout=2)

    assert controller.cancel() is True
    release_discovery.set()
    run_thread.join(timeout=2)

    assert not run_thread.is_alive()
    assert len(results) == 1
    assert results[0].cancelled is True
    assert factory.calls == []
    assert controller.cancel() is False


def test_close_returns_promptly_while_process_creation_is_blocked(fake_executable, tmp_path):
    entered = threading.Event()
    release = threading.Event()
    processes = []

    def blocking_factory(args, **_kwargs):
        entered.set()
        assert release.wait(timeout=2)
        process = FakeProcess(list(args), ProcessSpec(successful_jsonl(), never_complete=True))
        processes.append(process)
        return process

    controller = CodexComputerController(working_directory=tmp_path, popen_factory=blocking_factory)
    results: list[ComputerControlResult] = []
    run_thread = threading.Thread(target=lambda: results.append(controller.run("late task")))
    run_thread.start()
    assert entered.wait(timeout=2)

    started_at = time.monotonic()
    controller.close()
    close_elapsed = time.monotonic() - started_at

    assert close_elapsed < 0.5
    assert controller._closed
    release.set()
    run_thread.join(timeout=2)
    assert not run_thread.is_alive()
    assert len(results) == 1
    assert results[0].cancelled is True
    assert processes[0].terminated is True


def test_missing_executable_empty_input_and_close_are_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr(computer_control.shutil, "which", lambda _value: None)
    controller = CodexComputerController(working_directory=tmp_path)

    assert controller.run(" ").success is False
    missing = controller.run("do something")
    assert missing.success is False
    assert "not found" in missing.message
    controller.close()
    controller.close()
    assert controller.run("after close").success is False
