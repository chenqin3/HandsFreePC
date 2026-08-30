from __future__ import annotations

import threading
import time

import pytest

from handsfree_pc.session import (
    CommandWorker,
    JobOutcome,
    PromptAssembler,
    QueuedCommand,
    SessionState,
    WorkerState,
)


def wait_for_state(worker: CommandWorker, state: WorkerState, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while worker.state != state and time.monotonic() < deadline:
        time.sleep(0.005)
    assert worker.state == state


def successful(command: QueuedCommand, _cancel: threading.Event) -> JobOutcome:
    return JobOutcome(command, success=True, message="ok")


def test_session_and_worker_states_have_stable_wire_values() -> None:
    assert SessionState.ACTIVE.value == "active"
    assert SessionState.DRAINING.value == "draining"
    assert WorkerState.RUNNING.value == "running"
    assert WorkerState.PAUSED.value == "paused"


def test_prompt_assembler_collects_fragments_until_over() -> None:
    assembler = PromptAssembler()

    assert assembler.feed("打开D盘") == []
    assert assembler.feed("下的资料文件夹") == []
    assert assembler.pending_text == "打开D盘 下的资料文件夹"
    assert assembler.feed("over") == ["打开D盘 下的资料文件夹"]
    assert not assembler.has_pending


def test_prompt_assembler_emits_multiple_prompts_from_one_fragment() -> None:
    assembler = PromptAssembler()

    completed = assembler.feed("打开文档 over 然后打开浏览器，OVER。留下半句")

    assert completed == ["打开文档", "然后打开浏览器"]
    assert assembler.pending_text == "留下半句"


def test_prompt_assembler_ignores_empty_delimiters_and_does_not_split_mouseover() -> None:
    assembler = PromptAssembler()

    assert assembler.feed("over，OVER") == []
    assert assembler.feed("打开 mouseover 设置 over") == ["打开 mouseover 设置"]
    assert not assembler.has_pending


def test_prompt_assembler_allows_over_directly_after_chinese_but_not_ascii_word() -> None:
    assembler = PromptAssembler()

    assert assembler.feed("打开下载目录over") == ["打开下载目录"]
    assert assembler.feed("voiceover") == []
    assert assembler.pending_text == "voiceover"


def test_prompt_assembler_supports_configurable_delimiters() -> None:
    assembler = PromptAssembler(["完毕", "done"])

    assert assembler.feed("第一条完毕 second DONE") == ["第一条", "second"]
    assert assembler.delimiters == ("done", "完毕")


def test_prompt_assembler_discards_and_returns_unfinished_prompt() -> None:
    assembler = PromptAssembler()
    assembler.feed("尚未提交的内容")

    assert assembler.discard_pending() == "尚未提交的内容"
    assert assembler.discard_pending() == ""
    assert not assembler.has_pending


@pytest.mark.parametrize("delimiters", [[], [""], ["  "]])
def test_prompt_assembler_rejects_empty_delimiter_configuration(delimiters) -> None:
    with pytest.raises(ValueError, match="delimiter"):
        PromptAssembler(delimiters)


def test_command_worker_is_strict_fifo_and_never_overlaps_handlers() -> None:
    calls: list[int] = []
    outcomes: list[JobOutcome] = []
    active = 0
    max_active = 0
    lock = threading.Lock()

    def handler(command: QueuedCommand, _cancel: threading.Event) -> JobOutcome:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        calls.append(command.sequence)
        with lock:
            active -= 1
        return JobOutcome(command, success=True)

    worker = CommandWorker(handler, on_outcome=outcomes.append)
    worker.start()
    commands = [QueuedCommand(f"command {value}", sequence=value) for value in (3, 1, 2)]

    assert all(worker.enqueue(command) for command in commands)
    assert worker.drain(timeout=1)
    assert worker.stop(cancel_pending=False, timeout=1)

    assert calls == [3, 1, 2]
    assert [item.command.sequence for item in outcomes] == [3, 1, 2]
    assert max_active == 1
    assert worker.state == WorkerState.STOPPED


def test_command_worker_reports_observable_state_transitions() -> None:
    states: list[WorkerState] = []
    worker = CommandWorker(successful, on_state_change=states.append)

    worker.start()
    assert worker.enqueue(QueuedCommand("one"))
    assert worker.drain(timeout=1)
    assert worker.stop(cancel_pending=False, timeout=1)

    assert states[0] == WorkerState.IDLE
    assert WorkerState.RUNNING in states
    assert states[-1] == WorkerState.STOPPED


def test_command_worker_bounded_queue_reports_rejection() -> None:
    started = threading.Event()
    release = threading.Event()

    def handler(command: QueuedCommand, _cancel: threading.Event) -> JobOutcome:
        started.set()
        assert release.wait(1)
        return JobOutcome(command, success=True)

    worker = CommandWorker(handler, max_queue_size=1)
    worker.start()
    first = QueuedCommand("first", sequence=1)
    second = QueuedCommand("second", sequence=2)
    third = QueuedCommand("third", sequence=3)

    assert worker.enqueue(first)
    assert started.wait(1)
    assert worker.enqueue(second)
    assert not worker.enqueue(third)
    assert worker.pending_count == 1
    release.set()
    assert worker.drain(timeout=1)
    assert worker.stop(cancel_pending=False, timeout=1)


def test_command_worker_pauses_after_failure_and_resumes_fifo() -> None:
    calls: list[int] = []

    def handler(command: QueuedCommand, _cancel: threading.Event) -> JobOutcome:
        calls.append(command.sequence)
        return JobOutcome(command, success=command.sequence != 1)

    worker = CommandWorker(handler)
    worker.start()
    assert worker.enqueue(QueuedCommand("fails", sequence=1))
    assert worker.enqueue(QueuedCommand("waits", sequence=2))

    wait_for_state(worker, WorkerState.PAUSED)
    assert calls == [1]
    assert worker.unfinished_count == 1
    assert not worker.drain(timeout=0.02)
    assert worker.resume()
    assert worker.drain(timeout=1)
    assert calls == [1, 2]
    assert worker.stop(cancel_pending=False, timeout=1)


def test_control_command_runs_before_ordinary_fifo_after_confirmation_pause() -> None:
    calls: list[str] = []

    def handler(command: QueuedCommand, _cancel: threading.Event) -> JobOutcome:
        calls.append(command.text)
        return JobOutcome(command, success=command.text != "needs confirmation")

    worker = CommandWorker(handler)
    worker.start()
    assert worker.enqueue(QueuedCommand("needs confirmation", sequence=1))
    assert worker.enqueue(QueuedCommand("ordinary second", sequence=2))
    wait_for_state(worker, WorkerState.PAUSED)

    assert worker.enqueue_control(QueuedCommand("confirmation", sequence=3))
    assert worker.control_pending_count == 1
    assert worker.resume()
    assert worker.drain(timeout=1)
    assert worker.stop(cancel_pending=False, timeout=1)

    assert calls == ["needs confirmation", "confirmation", "ordinary second"]


def test_control_queue_is_bounded_independently_while_paused() -> None:
    worker = CommandWorker(successful, max_queue_size=1, max_control_queue_size=1)
    worker.start()
    assert worker.pause()

    normal = QueuedCommand("normal")
    control = QueuedCommand("control")
    assert worker.enqueue(normal)
    assert worker.enqueue_control(control)
    assert not worker.enqueue(QueuedCommand("normal overflow"))
    assert not worker.enqueue_control(QueuedCommand("control overflow"))
    assert worker.pending_count == 2
    assert worker.control_pending_count == 1

    cancelled = worker.cancel_pending()
    assert cancelled == (control, normal)
    assert worker.unfinished_count == 0
    assert worker.stop(timeout=1)


def test_manual_pause_during_active_job_holds_the_next_command() -> None:
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def handler(command: QueuedCommand, _cancel: threading.Event) -> JobOutcome:
        calls.append(command.sequence)
        if command.sequence == 1:
            started.set()
            assert release.wait(1)
        return JobOutcome(command, success=True)

    worker = CommandWorker(handler)
    worker.start()
    assert worker.enqueue(QueuedCommand("first", sequence=1))
    assert worker.enqueue(QueuedCommand("second", sequence=2))
    assert started.wait(1)

    assert worker.pause()
    release.set()
    wait_for_state(worker, WorkerState.PAUSED)
    assert calls == [1]
    assert worker.resume()
    assert worker.drain(timeout=1)
    assert calls == [1, 2]
    assert worker.stop(cancel_pending=False, timeout=1)


def test_handler_exception_becomes_failure_and_observer_exception_isolated() -> None:
    outcomes: list[JobOutcome] = []

    def handler(_command: QueuedCommand, _cancel: threading.Event) -> JobOutcome:
        raise RuntimeError("boom")

    def observe(outcome: JobOutcome) -> None:
        outcomes.append(outcome)
        raise RuntimeError("observer must not kill worker")

    worker = CommandWorker(handler, on_outcome=observe)
    worker.start()
    command = QueuedCommand("explode")
    assert worker.enqueue(command)

    wait_for_state(worker, WorkerState.PAUSED)
    assert len(outcomes) == 1
    assert outcomes[0].command == command
    assert not outcomes[0].success
    assert outcomes[0].error_type == "RuntimeError"
    assert worker.stop(timeout=1)


def test_cancel_pending_returns_commands_and_emits_cancelled_outcomes() -> None:
    started = threading.Event()
    release = threading.Event()
    outcomes: list[JobOutcome] = []

    def handler(command: QueuedCommand, _cancel: threading.Event) -> JobOutcome:
        started.set()
        assert release.wait(1)
        return JobOutcome(command, success=True)

    worker = CommandWorker(handler, on_outcome=outcomes.append)
    worker.start()
    commands = [QueuedCommand(str(value), sequence=value) for value in (1, 2, 3)]
    assert worker.enqueue(commands[0])
    assert started.wait(1)
    assert worker.enqueue(commands[1])
    assert worker.enqueue(commands[2])

    cancelled = worker.cancel_pending(reason="cleared by voice")
    assert cancelled == tuple(commands[1:])
    assert worker.unfinished_count == 1
    release.set()
    assert worker.drain(timeout=1)
    assert worker.stop(cancel_pending=False, timeout=1)

    cancelled_outcomes = [item for item in outcomes if item.cancelled]
    assert [item.command.sequence for item in cancelled_outcomes] == [2, 3]
    assert all(item.message == "cleared by voice" for item in cancelled_outcomes)


def test_stop_can_cooperatively_cancel_active_handler() -> None:
    started = threading.Event()
    saw_cancel = threading.Event()

    def handler(command: QueuedCommand, cancel: threading.Event) -> JobOutcome:
        started.set()
        assert cancel.wait(1)
        saw_cancel.set()
        return JobOutcome(command, success=False, cancelled=True)

    worker = CommandWorker(handler)
    worker.start()
    assert worker.enqueue(QueuedCommand("long task"))
    assert started.wait(1)

    assert worker.stop(timeout=1)
    assert saw_cancel.is_set()
    assert worker.state == WorkerState.STOPPED


def test_stop_without_cancellation_drains_all_accepted_commands() -> None:
    calls: list[int] = []

    def handler(command: QueuedCommand, _cancel: threading.Event) -> JobOutcome:
        calls.append(command.sequence)
        return JobOutcome(command, success=True)

    worker = CommandWorker(handler)
    worker.start()
    for value in range(4):
        assert worker.enqueue(QueuedCommand(str(value), sequence=value))

    assert worker.stop(cancel_pending=False, timeout=1)
    assert calls == [0, 1, 2, 3]
    assert not worker.accepting
    assert not worker.enqueue(QueuedCommand("too late"))


def test_stop_and_join_are_bounded_when_handler_ignores_cancellation() -> None:
    started = threading.Event()
    release = threading.Event()

    def handler(command: QueuedCommand, _cancel: threading.Event) -> JobOutcome:
        started.set()
        assert release.wait(1)
        return JobOutcome(command, success=True)

    worker = CommandWorker(handler)
    worker.start()
    assert worker.enqueue(QueuedCommand("blocked"))
    assert started.wait(1)

    assert not worker.stop(timeout=0.01)
    assert not worker.join(timeout=0.01)
    release.set()
    assert worker.join(timeout=1)
    assert worker.state == WorkerState.STOPPED


def test_stop_before_start_cancels_preloaded_commands() -> None:
    outcomes: list[JobOutcome] = []
    worker = CommandWorker(successful, on_outcome=outcomes.append)
    command = QueuedCommand("preloaded")
    assert worker.enqueue(command)

    assert worker.stop(timeout=0.1)

    assert worker.state == WorkerState.STOPPED
    assert worker.unfinished_count == 0
    assert len(outcomes) == 1
    assert outcomes[0].command == command
    assert outcomes[0].cancelled


def test_graceful_stop_before_start_cancels_because_no_worker_can_drain() -> None:
    outcomes: list[JobOutcome] = []
    worker = CommandWorker(successful, on_outcome=outcomes.append)
    command = QueuedCommand("cannot run before start")
    assert worker.enqueue_control(command)

    assert worker.stop(timeout=0.1, cancel_pending=False)

    assert worker.unfinished_count == 0
    assert len(outcomes) == 1
    assert outcomes[0].command == command
    assert outcomes[0].cancelled
