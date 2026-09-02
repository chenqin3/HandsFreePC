"""Kimi Code CLI as the desktop executor.

Each spoken command is handed to the Kimi Code agent in non-interactive prompt mode. Kimi loads the
user's ``gui-control`` skill and completes the task the way a person would:
screenshot, look, click/paste with pyautogui, screenshot again to verify. The
agent reports a one-line verdict that this controller turns into the queue
outcome the voice runtime already understands.

Prompt mode (``kimi -p``) executes tool calls without approval prompts, so no
permission flag is needed; ``--yolo``/``--auto`` are rejected alongside ``-p``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .control import ControlResult

DEFAULT_PREAMBLE = """你是 HandsFreePC 的桌面执行代理。用户通过语音下达指令，转写文本可能含糊、
中英混杂、有口误；先按 gui-control 技能里的"意图定位"原则，把转写当线索去对真实清单
（窗口标题、侧栏会话名、目录里的文件名、WorkMap 项目）做模糊匹配，再用键鼠完成。
执行要求：
- 只用 gui-control 技能的方法：截图 → 看图算坐标 → pyautogui 点击/粘贴 → 再截图核对。
  脚本和 venv 按该技能里写的位置。
- 查找文件用 find/ls 按修改时间排序，不要用 Glob 扫大目录。
- 不要问我问题，自己决定并完成；遇到不确定就选最像的一个。
- 用户说了"不要发送"的内容绝不按 Enter，也不点发送按钮。
- Claude 桌面端没特别说明就用 Code 页签。
- 完成后，最后两行必须严格是：
RESULT: <成功|失败> - <一句话说明做了什么、核对到了什么>
SCREENSHOT: <最终截图的完整路径>"""

_RESULT_RE = re.compile(
    r"RESULT:\s*(?P<verdict>成功|失败|SUCCESS|FAIL(?:URE|ED)?)\s*[-—:：]?\s*(?P<note>.*)",
    re.IGNORECASE,
)
_SCREENSHOT_RE = re.compile(r"SCREENSHOT:\s*(?P<path>\S+)")
_MAX_STDERR_CHARS = 4000


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


@dataclass(slots=True)
class KimiRun:
    """What one ``kimi -p`` invocation produced, parsed from its stream-json."""

    tool_calls: int = 0
    tool_names: list[str] = field(default_factory=list)
    # (tool name, first 200 chars of its arguments) — local debugging only.
    tool_log: list[tuple[str, str]] = field(default_factory=list)
    final_text: str = ""
    session_id: str | None = None
    stderr_tail: str = ""
    returncode: int | None = None
    events: int = 0

    @property
    def verdict(self) -> bool | None:
        match = _RESULT_RE.search(self.final_text)
        if match is None:
            return None
        return match.group("verdict").casefold() in {"成功", "success"}

    @property
    def note(self) -> str:
        match = _RESULT_RE.search(self.final_text)
        if match is not None and match.group("note").strip():
            return " ".join(match.group("note").split())
        lines = [line.strip() for line in self.final_text.splitlines() if line.strip()]
        return lines[-1][:300] if lines else ""

    @property
    def screenshot(self) -> str | None:
        match = _SCREENSHOT_RE.search(self.final_text)
        return match.group("path") if match is not None else None


def parse_stream_line(run: KimiRun, line: str) -> dict[str, Any] | None:
    """Fold one stream-json line into ``run``; return the event for callers."""

    line = line.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    run.events += 1
    role = event.get("role")
    if role == "assistant":
        calls = event.get("tool_calls")
        if isinstance(calls, list) and calls:
            run.tool_calls += len(calls)
            for call in calls:
                function = call.get("function") if isinstance(call, dict) else None
                name = function.get("name") if isinstance(function, dict) else None
                if isinstance(name, str):
                    run.tool_names.append(name)
                    arguments = function.get("arguments") if isinstance(function, dict) else ""
                    run.tool_log.append((name, str(arguments or "")[:200]))
        content = event.get("content")
        if isinstance(content, str) and content.strip():
            run.final_text = content
    elif role == "meta":
        session = event.get("session_id")
        if isinstance(session, str) and session.strip():
            run.session_id = session.strip()
    return event


class KimiAgentController:
    """Voice-queue controller that delegates every command to Kimi Code CLI."""

    def __init__(
        self,
        *,
        executable: str = "kimi",
        working_directory: str | Path | None = None,
        model: str | None = None,
        preamble: str | None = None,
        skills_dir: str | Path | None = None,
        timeout_seconds: float = 600.0,
        resume_session: bool = False,
        diagnostics: object | None = None,
        environment: Mapping[str, str] | None = None,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.executable = str(executable)
        self.working_directory = (
            Path(working_directory).expanduser() if working_directory else Path.home()
        )
        self.model = model
        self.preamble = (preamble or DEFAULT_PREAMBLE).strip()
        self.skills_dir = Path(skills_dir).expanduser() if skills_dir else None
        self.timeout_seconds = float(timeout_seconds)
        self.resume_session = bool(resume_session)
        self.diagnostics = diagnostics
        self._environment = dict(os.environ if environment is None else environment)
        self._popen = popen_factory
        self._monotonic = monotonic
        self._sleep = sleeper
        self._on_progress = on_progress
        self._session_id = str(uuid.uuid4())
        self._kimi_session: str | None = None
        self._lock = threading.Lock()
        self._process: Any | None = None
        self._cancel = threading.Event()
        self._closed = False
        self.last_run: KimiRun | None = None

    # -- Controller protocol ----------------------------------------------------

    @property
    def session_id(self) -> str | None:
        return self._kimi_session or self._session_id

    def build_prompt(self, instruction: str) -> str:
        return f"{self.preamble}\n用户指令：{instruction.strip()}"

    def build_args(self, prompt: str) -> list[str]:
        args = [self.executable, "-p", prompt, "--output-format", "stream-json"]
        if self.model:
            args.extend(["--model", self.model])
        if self.skills_dir is not None:
            args.extend(["--skills-dir", str(self.skills_dir)])
        if self.resume_session and self._kimi_session:
            args.extend(["--session", self._kimi_session])
        return args

    def _trace(self, *, error_code: str, safe_message: str, level: str = "info") -> None:
        event = getattr(self.diagnostics, "event", None)
        if not callable(event):
            return
        with suppress(Exception):
            event(
                stage="kimi_agent",
                error_code=error_code,
                safe_message=" ".join(str(safe_message).split())[:320],
                level=level,
                session_id=self._session_id,
            )

    def _progress(self, text: str) -> None:
        if self._on_progress is None:
            return
        with suppress(Exception):
            self._on_progress(text)

    def _failure(
        self,
        message: str,
        *,
        error_code: str,
        run: KimiRun | None = None,
        cancelled: bool = False,
        timed_out: bool = False,
    ) -> ControlResult:
        self._trace(error_code=error_code, safe_message=message, level="error")
        return ControlResult(
            False,
            f"FAILURE: {message}",
            session_id=self.session_id,
            cancelled=cancelled,
            timed_out=timed_out,
            returncode=run.returncode if run is not None else None,
            stage="kimi_agent",
            error_code=error_code,
            safe_message=message,
        )

    def _terminate(self, process: Any) -> None:
        if process.poll() is not None:
            return
        try:
            import psutil

            root = psutil.Process(process.pid)
            targets = [root, *reversed(root.children(recursive=True))]
            for target in targets:
                with suppress(psutil.Error):
                    target.terminate()
            _gone, alive = psutil.wait_procs(targets, timeout=2.0)
            for target in alive:
                with suppress(psutil.Error):
                    target.kill()
        except Exception:
            pass
        if process.poll() is None:
            with suppress(Exception):
                process.kill()
            with suppress(Exception):
                process.wait(timeout=2.0)

    def run(
        self,
        instruction: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ControlResult:
        if not isinstance(instruction, str) or not instruction.strip():
            return self._failure("电脑控制指令为空", error_code="EMPTY_INSTRUCTION")
        if len(instruction) > 8000:
            return self._failure("电脑控制指令过长", error_code="INSTRUCTION_TOO_LONG")
        with self._lock:
            if self._closed:
                return self._failure("桌面控制器已经关闭", error_code="CONTROLLER_CLOSED")
            if self._process is not None:
                return self._failure("Kimi 正在执行另一条指令", error_code="CONTROLLER_BUSY")
            self._cancel.clear()
        run = KimiRun()
        self.last_run = run
        prompt = self.build_prompt(instruction)
        try:
            process = self._popen(
                self.build_args(prompt),
                cwd=str(self.working_directory),
                env=self._environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_creation_flags(),
            )
        except OSError as exc:
            return self._failure(
                f"无法启动 Kimi CLI（{self.executable}）：{type(exc).__name__}",
                error_code="KIMI_NOT_AVAILABLE",
            )
        with self._lock:
            self._process = process
        self._trace(error_code="KIMI_STARTED", safe_message=f"Kimi 开始执行：{instruction[:120]}")
        self._progress("已交给 Kimi 执行")
        stderr_chunks: list[str] = []

        def drain_stderr() -> None:
            stream = getattr(process, "stderr", None)
            if stream is None:
                return
            with suppress(Exception):
                for chunk in stream:
                    if sum(len(item) for item in stderr_chunks) < _MAX_STDERR_CHARS:
                        stderr_chunks.append(chunk)

        def read_stdout() -> None:
            stream = getattr(process, "stdout", None)
            if stream is None:
                return
            with suppress(Exception):
                for line in stream:
                    event = parse_stream_line(run, line)
                    if event is None:
                        continue
                    if event.get("role") == "assistant" and event.get("tool_calls"):
                        names = ", ".join(run.tool_names[-len(event["tool_calls"]) :])
                        self._trace(error_code="KIMI_TOOL_CALL", safe_message=names)
                        self._progress(f"Kimi 第 {run.tool_calls} 步：{names}")

        readers = [
            threading.Thread(target=read_stdout, name="kimi-stdout", daemon=True),
            threading.Thread(target=drain_stderr, name="kimi-stderr", daemon=True),
        ]
        for reader in readers:
            reader.start()

        deadline = self._monotonic() + self.timeout_seconds
        cancelled = timed_out = False
        try:
            while process.poll() is None:
                if self._cancel.is_set() or (cancel_event is not None and cancel_event.is_set()):
                    cancelled = True
                    self._terminate(process)
                    break
                if self._monotonic() >= deadline:
                    timed_out = True
                    self._terminate(process)
                    break
                self._sleep(0.2)
            for reader in readers:
                reader.join(timeout=5.0)
            run.returncode = process.poll()
            run.stderr_tail = "".join(stderr_chunks)[-_MAX_STDERR_CHARS:]
        finally:
            with self._lock:
                self._process = None
        if run.session_id:
            self._kimi_session = run.session_id
        if cancelled:
            return self._failure("桌面任务已取消", error_code="CANCELLED", run=run, cancelled=True)
        if timed_out:
            return self._failure(
                f"Kimi 超过 {int(self.timeout_seconds)} 秒仍未完成，已终止",
                error_code="KIMI_TIMEOUT",
                run=run,
                timed_out=True,
            )
        verdict = run.verdict
        note = run.note
        if run.returncode not in (0, None) and verdict is None:
            stderr_lines = run.stderr_tail.strip().splitlines()
            detail = note or (stderr_lines[-1] if stderr_lines else "无输出")
            return self._failure(
                f"Kimi CLI 退出码 {run.returncode}：{detail}",
                error_code="KIMI_EXIT_ERROR",
                run=run,
            )
        if verdict is None:
            return self._failure(
                note or "Kimi 没有给出明确结果",
                error_code="KIMI_NO_VERDICT",
                run=run,
            )
        if not verdict:
            return self._failure(
                note or "Kimi 报告任务失败",
                error_code="KIMI_REPORTED_FAILURE",
                run=run,
            )
        self._trace(
            error_code="KIMI_COMPLETED",
            safe_message=f"{note} (tool_calls={run.tool_calls})",
        )
        return ControlResult(
            True,
            note or "Kimi 已完成",
            session_id=self.session_id,
            returncode=run.returncode,
            stage="kimi_agent",
            error_code="KIMI_COMPLETED",
            safe_message=note or "Kimi 已完成",
        )

    def cancel(self) -> bool:
        self._cancel.set()
        with self._lock:
            process = self._process
        if process is None:
            return False
        self._terminate(process)
        return True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.cancel()


def build_kimi_controller(
    settings: Any,
    *,
    diagnostics: object | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> KimiAgentController:
    """Create the controller from the ``kimi`` configuration section."""

    preamble = None
    if settings.preamble_file is not None:
        preamble = Path(settings.preamble_file).read_text(encoding="utf-8")
    return KimiAgentController(
        executable=settings.executable,
        working_directory=settings.working_directory,
        model=settings.model,
        preamble=preamble,
        skills_dir=settings.skills_dir,
        timeout_seconds=settings.timeout_seconds,
        resume_session=settings.resume_session,
        diagnostics=diagnostics,
        on_progress=on_progress,
    )


__all__ = [
    "DEFAULT_PREAMBLE",
    "KimiAgentController",
    "KimiRun",
    "build_kimi_controller",
    "parse_stream_line",
]
