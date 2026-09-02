from __future__ import annotations

import re
import threading
import time
import unicodedata
import uuid
from collections import deque
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum


class SessionState(StrEnum):
    """Lifecycle of one continuous voice-control session."""

    ARMED = "armed"
    ACTIVE = "active"
    DRAINING = "draining"
    PAUSED = "paused"
    STOPPED = "stopped"


class WorkerState(StrEnum):
    """Observable state of the single-consumer command worker."""

    NEW = "new"
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class QueuedCommand:
    """An immutable prompt accepted by a voice session."""

    text: str
    sequence: int = 0
    session_id: str | None = None
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    enqueued_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("Queued command text must be a non-empty string")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise ValueError("Queued command sequence must be an integer")
        if self.sequence < 0:
            raise ValueError("Queued command sequence cannot be negative")
        if self.session_id is not None and not isinstance(self.session_id, str):
            raise ValueError("Queued command session_id must be a string or null")
        if not isinstance(self.command_id, str) or not self.command_id:
            raise ValueError("Queued command command_id must be a non-empty string")


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """Terminal result for one queued command."""

    command: QueuedCommand
    success: bool
    message: str = ""
    cancelled: bool = False
    error_type: str | None = None
    stage: str | None = None
    error_code: str | None = None
    safe_message: str | None = None
    exception_type: str | None = None
    started_at: float | None = None
    completed_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if not isinstance(self.command, QueuedCommand):
            raise ValueError("Job outcome command must be a QueuedCommand")
        if type(self.success) is not bool:
            raise ValueError("Job outcome success must be a boolean")
        if type(self.cancelled) is not bool:
            raise ValueError("Job outcome cancelled must be a boolean")
        if self.cancelled and self.success:
            raise ValueError("A cancelled job cannot be successful")


_EDGE_SEPARATORS = " \t\r\n,，.。;；!！?？"


class PromptAssembler:
    """Collect ASR fragments and emit prompts only at configured delimiters.

    ASCII word delimiters use ASCII word boundaries.  This means ``over`` can
    follow Chinese text without whitespace while words such as ``mouseover``
    and ``voiceover`` remain ordinary prompt content.
    """

    def __init__(self, delimiters: Iterable[str] = ("over",)) -> None:
        normalized: list[str] = []
        for delimiter in delimiters:
            if not isinstance(delimiter, str):
                raise ValueError("Prompt delimiters must be strings")
            value = unicodedata.normalize("NFKC", delimiter).strip()
            if not value:
                raise ValueError("Prompt delimiters cannot be empty")
            if value.casefold() not in {item.casefold() for item in normalized}:
                normalized.append(value)
        if not normalized:
            raise ValueError("At least one prompt delimiter is required")
        normalized.sort(key=len, reverse=True)
        self.delimiters = tuple(normalized)
        self._pattern = re.compile(
            "|".join(self._delimiter_pattern(item) for item in normalized),
            re.IGNORECASE,
        )
        self._pending = ""

    @staticmethod
    def _delimiter_pattern(delimiter: str) -> str:
        escaped = re.escape(delimiter)
        if re.fullmatch(r"[A-Za-z0-9_]+", delimiter):
            return rf"(?<![A-Za-z0-9_])(?:{escaped})(?![A-Za-z0-9_])"
        return rf"(?:{escaped})"

    @property
    def pending_text(self) -> str:
        return self._clean_edge(self._pending)

    @property
    def has_pending(self) -> bool:
        return bool(self.pending_text)

    def contains_delimiter(self, fragment: str) -> bool:
        """Return whether a fragment contains a configured delimiter without mutating state."""

        if not isinstance(fragment, str):
            raise ValueError("Prompt fragment must be a string")
        value = unicodedata.normalize("NFKC", fragment)
        return self._pattern.search(value) is not None

    def feed(self, fragment: str) -> list[str]:
        """Append one ASR fragment and return every newly completed prompt."""

        if not isinstance(fragment, str):
            raise ValueError("Prompt fragment must be a string")
        value = unicodedata.normalize("NFKC", fragment).strip()
        if value:
            if self._pending and not self._pending[-1].isspace():
                self._pending += " "
            self._pending += value

        completed: list[str] = []
        consumed = 0
        for match in self._pattern.finditer(self._pending):
            prompt = self._clean_edge(self._pending[consumed : match.start()])
            if prompt:
                completed.append(prompt)
            consumed = match.end()
        if consumed:
            self._pending = self._pending[consumed:].lstrip(_EDGE_SEPARATORS)
        return completed

    def discard_pending(self) -> str:
        """Discard and return the unfinished prompt, if any."""

        discarded = self.pending_text
        self._pending = ""
        return discarded

    def finalize(self) -> str | None:
        """Finish the current prompt at an out-of-band delimiter event.

        The local delimiter spotter calls this after its sample-bounded audio
        prefix has been transcribed, without injecting the delimiter word into
        the command text.
        """

        completed = self.pending_text
        self._pending = ""
        return completed or None

    @staticmethod
    def _clean_edge(value: str) -> str:
        return value.strip(_EDGE_SEPARATORS)


CommandHandler = Callable[[QueuedCommand, threading.Event], JobOutcome]
OutcomeCallback = Callable[[JobOutcome], None]
StateCallback = Callable[[WorkerState], None]


class CommandWorker:
    """Run desktop commands through one strict-FIFO handler thread.

    The handler receives a per-command cancellation event.  Cancellation is
    cooperative: ``stop`` can request it but cannot undo an input event that
    has already reached Windows.
    """

    def __init__(
        self,
        handler: CommandHandler,
        *,
        max_queue_size: int = 20,
        failure_policy: str = "pause",
        on_outcome: OutcomeCallback | None = None,
        on_state_change: StateCallback | None = None,
        thread_name: str = "HandsFreePC-command-worker",
    ) -> None:
        if not callable(handler):
            raise ValueError("Command worker handler must be callable")
        if isinstance(max_queue_size, bool) or not isinstance(max_queue_size, int):
            raise ValueError("max_queue_size must be an integer")
        if max_queue_size <= 0:
            raise ValueError("max_queue_size must be positive")
        if failure_policy not in {"pause", "continue"}:
            raise ValueError("failure_policy must be pause or continue")
        self._handler = handler
        self._queue: deque[QueuedCommand] = deque()
        self._max_queue_size = max_queue_size
        self._failure_policy = failure_policy
        self._on_outcome = on_outcome
        self._on_state_change = on_state_change
        self._thread = threading.Thread(target=self._run, name=thread_name, daemon=True)
        self._condition = threading.Condition(threading.RLock())
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._stop_event = threading.Event()
        self._stop_when_idle = False
        self._accepting = True
        self._state = WorkerState.NEW
        self._active_command: QueuedCommand | None = None
        self._active_cancel_event: threading.Event | None = None
        self._unfinished = 0

    @property
    def state(self) -> WorkerState:
        with self._condition:
            return self._state

    @property
    def active_command(self) -> QueuedCommand | None:
        with self._condition:
            return self._active_command

    @property
    def pending_count(self) -> int:
        with self._condition:
            return len(self._queue)

    @property
    def unfinished_count(self) -> int:
        with self._condition:
            return self._unfinished

    @property
    def accepting(self) -> bool:
        with self._condition:
            return self._accepting

    def start(self) -> None:
        with self._condition:
            if self._state != WorkerState.NEW:
                raise RuntimeError("Command worker can only be started once")
            self._state = WorkerState.IDLE
            self._condition.notify_all()
        self._notify_state(WorkerState.IDLE)
        self._thread.start()

    def enqueue(
        self,
        command: QueuedCommand,
        *,
        block: bool = False,
        timeout: float | None = None,
    ) -> bool:
        """Queue a command, returning ``False`` instead of silently dropping it."""

        if not isinstance(command, QueuedCommand):
            raise ValueError("enqueue requires a QueuedCommand")
        if block and timeout is None:
            raise ValueError("A blocking enqueue requires a finite timeout")
        if timeout is not None and timeout < 0:
            raise ValueError("timeout cannot be negative")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while len(self._queue) >= self._max_queue_size:
                if not self._accepting or self._state in {
                    WorkerState.STOPPING,
                    WorkerState.STOPPED,
                }:
                    return False
                if not block:
                    return False
                assert deadline is not None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            if not self._accepting or self._state in {
                WorkerState.STOPPING,
                WorkerState.STOPPED,
            }:
                return False
            self._queue.append(command)
            self._unfinished += 1
            self._condition.notify_all()
            return True

    def pause(self) -> bool:
        with self._condition:
            if self._state in {WorkerState.NEW, WorkerState.STOPPING, WorkerState.STOPPED}:
                return False
            self._resume_event.clear()
            if self._active_command is None:
                self._state = WorkerState.PAUSED
                state_changed = True
            else:
                state_changed = False
            self._condition.notify_all()
        if state_changed:
            self._notify_state(WorkerState.PAUSED)
        return True

    def resume(self) -> bool:
        with self._condition:
            if self._state != WorkerState.PAUSED:
                return False
            self._resume_event.set()
            self._state = WorkerState.IDLE
            self._condition.notify_all()
        self._notify_state(WorkerState.IDLE)
        return True

    def cancel_current(self) -> bool:
        with self._condition:
            if self._active_cancel_event is None:
                return False
            self._active_cancel_event.set()
            return True

    def cancel_pending(
        self, *, reason: str = "Cancelled before execution"
    ) -> tuple[QueuedCommand, ...]:
        with self._condition:
            cancelled = list(self._queue)
            self._queue.clear()
            self._unfinished -= len(cancelled)
            if self._unfinished < 0:  # defensive invariant; should never be reached
                self._unfinished = 0
            self._condition.notify_all()
        outcomes = self._cancelled_outcomes(cancelled, reason)
        for outcome in outcomes:
            self._notify_outcome(outcome)
        return tuple(cancelled)

    def drain(self, *, timeout: float | None = None) -> bool:
        """Wait until every accepted command reaches a terminal outcome."""

        if timeout is not None and timeout < 0:
            raise ValueError("timeout cannot be negative")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._unfinished:
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def stop(self, *, timeout: float = 2.0, cancel_pending: bool = True) -> bool:
        """Stop accepting commands and perform a bounded worker join.

        With ``cancel_pending=True`` (the default), queued commands receive
        cancelled outcomes and the active handler receives its cancellation
        event.  With ``False``, the worker drains the queue before stopping.
        """

        if timeout < 0:
            raise ValueError("timeout cannot be negative")
        with self._condition:
            if self._state == WorkerState.STOPPED:
                return True
            state_before_start = self._state == WorkerState.NEW
            self._accepting = False
            if cancel_pending or state_before_start:
                cancelled = list(self._queue)
                self._queue.clear()
                self._unfinished -= len(cancelled)
                if self._active_cancel_event is not None:
                    self._active_cancel_event.set()
                self._stop_event.set()
                self._resume_event.set()
            else:
                cancelled = []
                self._stop_when_idle = True
                self._resume_event.set()
                if self._unfinished == 0:
                    self._stop_event.set()
            if state_before_start:
                self._state = WorkerState.STOPPED
            else:
                self._state = WorkerState.STOPPING
            self._condition.notify_all()
        for outcome in self._cancelled_outcomes(cancelled, "Worker stopped before execution"):
            self._notify_outcome(outcome)
        if state_before_start:
            self._notify_state(WorkerState.STOPPED)
            return True

        self._notify_state(WorkerState.STOPPING)
        return self.join(timeout=timeout)

    def join(self, *, timeout: float = 2.0) -> bool:
        """Join the worker for at most ``timeout`` seconds."""

        if timeout < 0:
            raise ValueError("timeout cannot be negative")
        if not self._thread.is_alive():
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    while True:
                        if self._stop_event.is_set():
                            return
                        if not self._resume_event.is_set():
                            self._condition.wait()
                            continue
                        if self._queue:
                            command = self._queue.popleft()
                            break
                        if self._stop_when_idle and self._unfinished == 0:
                            self._stop_event.set()
                            return
                        self._condition.wait()
                    cancel_event = threading.Event()
                    started_at = time.monotonic()
                    self._active_command = command
                    self._active_cancel_event = cancel_event
                    self._state = WorkerState.RUNNING
                    self._condition.notify_all()
                self._notify_state(WorkerState.RUNNING)

                outcome = self._invoke_handler(command, cancel_event, started_at)
                pause_after_failure = bool(
                    not outcome.success
                    and not outcome.cancelled
                    and self._failure_policy == "pause"
                )
                with self._condition:
                    self._active_command = None
                    self._active_cancel_event = None
                    self._unfinished -= 1
                    if self._unfinished < 0:  # defensive invariant
                        self._unfinished = 0
                    if (
                        pause_after_failure or not self._resume_event.is_set()
                    ) and not self._stop_event.is_set():
                        self._resume_event.clear()
                        self._state = WorkerState.PAUSED
                        next_state = WorkerState.PAUSED
                    else:
                        self._state = WorkerState.IDLE
                        next_state = WorkerState.IDLE
                    should_stop = self._stop_when_idle and self._unfinished == 0
                    if should_stop:
                        self._stop_event.set()
                    self._condition.notify_all()
                if not self._stop_event.is_set():
                    self._notify_state(next_state)
                self._notify_outcome(outcome)
        finally:
            with self._condition:
                self._accepting = False
                self._state = WorkerState.STOPPED
                self._condition.notify_all()
            self._notify_state(WorkerState.STOPPED)

    @staticmethod
    def _cancelled_outcomes(commands: Iterable[QueuedCommand], reason: str) -> list[JobOutcome]:
        now = time.monotonic()
        return [
            JobOutcome(
                command=command,
                success=False,
                cancelled=True,
                message=reason,
                completed_at=now,
            )
            for command in commands
        ]

    def _invoke_handler(
        self,
        command: QueuedCommand,
        cancel_event: threading.Event,
        started_at: float,
    ) -> JobOutcome:
        try:
            outcome = self._handler(command, cancel_event)
            if not isinstance(outcome, JobOutcome):
                raise TypeError("Command handler must return JobOutcome")
            if outcome.command.command_id != command.command_id:
                raise ValueError("Command handler returned an outcome for a different command")
            return outcome
        except Exception as exc:
            return JobOutcome(
                command=command,
                success=False,
                message=str(exc) or type(exc).__name__,
                error_type=type(exc).__name__,
                started_at=started_at,
            )

    def _notify_outcome(self, outcome: JobOutcome) -> None:
        if self._on_outcome is None:
            return
        # Observer failures must never kill the desktop-command worker.
        with suppress(Exception):
            self._on_outcome(outcome)

    def _notify_state(self, state: WorkerState) -> None:
        if self._on_state_change is None:
            return
        # State observers are informational and cannot own worker liveness.
        with suppress(Exception):
            self._on_state_change(state)
