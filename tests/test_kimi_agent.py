from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace

from handsfree_pc.kimi_agent import (
    DEFAULT_PREAMBLE,
    KimiAgentController,
    KimiRun,
    parse_stream_line,
)


def _stream(*events: dict) -> str:
    return "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)


class FakeProcess:
    def __init__(self, stdout: str, *, returncode: int = 0, hang: bool = False) -> None:
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO("saved\n")
        self.pid = 4321
        self.returncode: int | None = None
        self._final = returncode
        self._hang = hang
        self.killed = False

    def poll(self):
        if self._hang and not self.killed:
            return None
        self.returncode = -9 if self.killed else self._final
        return self.returncode

    def wait(self, timeout=None):
        return self.poll()

    def kill(self):
        self.killed = True


class RecordingPopen:
    def __init__(self, stdout: str, *, returncode: int = 0, hang: bool = False) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.hang = hang
        self.calls: list[tuple[list[str], dict]] = []
        self.process: FakeProcess | None = None

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        self.process = FakeProcess(self.stdout, returncode=self.returncode, hang=self.hang)
        return self.process


_SUCCESS_STREAM = _stream(
    {
        "role": "assistant",
        "tool_calls": [
            {"type": "function", "id": "t1", "function": {"name": "Skill", "arguments": "{}"}},
            {"type": "function", "id": "t2", "function": {"name": "Bash", "arguments": "{}"}},
        ],
    },
    {"role": "tool", "tool_call_id": "t1", "content": "loaded"},
    {"role": "tool", "tool_call_id": "t2", "content": "saved"},
    {
        "role": "assistant",
        "content": "发送完成。\n\nRESULT: 成功 - 已把文件发到文件传输助手，截图核对到文件卡片\n"
        "SCREENSHOT: C:\\Users\\me\\shots\\sent.png",
    },
    {"role": "meta", "type": "session.resume_hint", "session_id": "session_abc"},
)


def test_stream_parsing_collects_tools_verdict_screenshot_and_session() -> None:
    run = KimiRun()
    for line in _SUCCESS_STREAM.splitlines():
        parse_stream_line(run, line)

    assert run.tool_calls == 2
    assert run.tool_names == ["Skill", "Bash"]
    assert run.verdict is True
    assert run.note == "已把文件发到文件传输助手，截图核对到文件卡片"
    assert run.screenshot == "C:\\Users\\me\\shots\\sent.png"
    assert run.session_id == "session_abc"


def test_controller_runs_kimi_prompt_mode_and_reports_the_verdict(tmp_path: Path) -> None:
    popen = RecordingPopen(_SUCCESS_STREAM)
    events: list[dict] = []
    diagnostics = SimpleNamespace(event=lambda **kw: events.append(kw))
    controller = KimiAgentController(
        executable="kimi.exe",
        working_directory=tmp_path,
        popen_factory=popen,
        sleeper=lambda _s: None,
        diagnostics=diagnostics,
    )

    result = controller.run("把下载里的报告发到微信的文件传输助手")

    assert result.success
    assert result.error_code == "KIMI_COMPLETED"
    assert "文件传输助手" in result.message
    args, kwargs = popen.calls[0]
    assert args[:2] == ["kimi.exe", "-p"]
    assert args[2].startswith(DEFAULT_PREAMBLE)
    assert args[2].endswith("用户指令：把下载里的报告发到微信的文件传输助手")
    assert args[3:5] == ["--output-format", "stream-json"]
    assert "--yolo" not in args and "--auto" not in args
    assert kwargs["cwd"] == str(tmp_path)
    assert controller.session_id == "session_abc"
    assert [e["error_code"] for e in events] == ["KIMI_STARTED", "KIMI_TOOL_CALL", "KIMI_COMPLETED"]


def test_controller_passes_model_and_skills_dir_when_configured(tmp_path: Path) -> None:
    popen = RecordingPopen(_SUCCESS_STREAM)
    controller = KimiAgentController(
        model="kimi-code/k3",
        skills_dir=tmp_path / "skills",
        preamble="PREAMBLE",
        popen_factory=popen,
        sleeper=lambda _s: None,
    )

    controller.run("切换到微信")

    args, _kwargs = popen.calls[0]
    assert args[args.index("--model") + 1] == "kimi-code/k3"
    assert args[args.index("--skills-dir") + 1] == str(tmp_path / "skills")
    assert args[2] == "PREAMBLE\n用户指令：切换到微信"


def test_reported_failure_and_missing_verdict_are_not_success() -> None:
    failed_content = "RESULT: 失败 - 没找到那个会话\nSCREENSHOT: x.png"
    failed = RecordingPopen(_stream({"role": "assistant", "content": failed_content}))
    result = KimiAgentController(popen_factory=failed, sleeper=lambda _s: None).run("打开会话")
    assert not result.success
    assert result.error_code == "KIMI_REPORTED_FAILURE"
    assert "没找到那个会话" in result.message

    silent = RecordingPopen(_stream({"role": "assistant", "content": "我已经完成了"}))
    result = KimiAgentController(popen_factory=silent, sleeper=lambda _s: None).run("打开会话")
    assert not result.success
    assert result.error_code == "KIMI_NO_VERDICT"
    assert "我已经完成了" in result.message


def test_nonzero_exit_without_verdict_is_an_error() -> None:
    popen = RecordingPopen("", returncode=1)
    result = KimiAgentController(popen_factory=popen, sleeper=lambda _s: None).run("切换到微信")
    assert not result.success
    assert result.error_code == "KIMI_EXIT_ERROR"


def test_cancel_event_terminates_the_agent() -> None:
    popen = RecordingPopen("", hang=True)
    cancel = threading.Event()
    cancel.set()
    result = KimiAgentController(popen_factory=popen, sleeper=lambda _s: None).run(
        "切换到微信", cancel_event=cancel
    )
    assert result.cancelled
    assert result.error_code == "CANCELLED"
    assert popen.process is not None and popen.process.killed


def test_timeout_terminates_the_agent() -> None:
    popen = RecordingPopen("", hang=True)
    clock = iter(range(0, 10_000, 100))
    result = KimiAgentController(
        popen_factory=popen,
        timeout_seconds=30,
        monotonic=lambda: next(clock),
        sleeper=lambda _s: None,
    ).run("切换到微信")
    assert result.timed_out
    assert result.error_code == "KIMI_TIMEOUT"
    assert popen.process is not None and popen.process.killed


def test_missing_executable_is_reported_not_raised() -> None:
    def boom(*_args, **_kwargs):
        raise FileNotFoundError("kimi")

    result = KimiAgentController(popen_factory=boom, sleeper=lambda _s: None).run("切换到微信")
    assert not result.success
    assert result.error_code == "KIMI_NOT_AVAILABLE"


def test_build_kimi_controller_reads_the_kimi_section(tmp_path: Path) -> None:
    from handsfree_pc.config import load_settings
    from handsfree_pc.kimi_agent import build_kimi_controller

    (tmp_path / "preamble.txt").write_text("自定义前言", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        "kimi:\n  executable: kimi-test\n  model: kimi-code/k3\n  timeout_seconds: 42\n"
        "  preamble_file: preamble.txt\n  working_directory: work\n  resume_session: true\n",
        encoding="utf-8",
    )
    settings = load_settings(config)

    controller = build_kimi_controller(settings.kimi, diagnostics=object())

    assert controller.executable == "kimi-test"
    assert controller.model == "kimi-code/k3"
    assert controller.timeout_seconds == 42.0
    assert controller.preamble == "自定义前言"
    assert controller.working_directory == (tmp_path / "work").resolve()
    assert controller.resume_session is True
    assert controller.build_prompt("打开记事本") == "自定义前言\n用户指令：打开记事本"
