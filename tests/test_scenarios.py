from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import handsfree_pc.cli as cli
import handsfree_pc.scenarios as scenarios
from handsfree_pc.computer_control import ComputerControlResult
from handsfree_pc.config import load_settings
from handsfree_pc.desktop.native_skills import NativeRouteStatus, NativeSkillResult


class FakeDriver:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, *_args, **_kwargs):
        self.calls += 1
        return object()


class FakeRouter:
    def route(self, *_args, **_kwargs):
        return NativeSkillResult(NativeRouteStatus.MISS, "miss")


class FakePlanner:
    def decide(self, *_args, **_kwargs):
        return object()


class FakeController:
    def __init__(
        self,
        results: list[ComputerControlResult] | None = None,
        *,
        execute_action_on_run: bool = False,
        on_run=None,
    ) -> None:
        self.results = list(results or [ComputerControlResult(True, "completed")])
        self.calls: list[str] = []
        self.driver = FakeDriver()
        self.native_router = FakeRouter()
        self.planner = FakePlanner()
        self.last_metrics = {"retry_count": 0, "action_count": 0}
        self.closed = False
        self.execute_action_on_run = execute_action_on_run
        self.on_run = on_run

    @property
    def session_id(self):
        return "scenario-test"

    def run(self, instruction, *, cancel_event=None):
        del cancel_event
        self.calls.append(instruction)
        if self.on_run is not None:
            self.on_run(instruction)
        if self.execute_action_on_run:
            self.driver.execute()
        if self.results:
            return self.results.pop(0)
        return ComputerControlResult(True, "completed")

    def execute(self, instruction, *, cancel_event=None):
        return self.run(instruction, cancel_event=cancel_event)

    def cancel(self):
        return False

    def close(self):
        self.closed = True


class FakeNative:
    def __init__(self, *, foreground_hwnd: int | None = 1) -> None:
        self.foreground_hwnd = foreground_hwnd
        self.windows = (
            [SimpleNamespace(hwnd=1, process_name="other.exe", class_name="OtherWindow")]
            if foreground_hwnd == 1
            else []
        )
        self.d_hwnd = 2
        self.activate_calls: list[int] = []

    def enumerate_windows(self):
        return list(self.windows)

    def get_foreground_window_info(self):
        if self.foreground_hwnd is None:
            return None
        return SimpleNamespace(hwnd=self.foreground_hwnd)

    def path_open_state(self, path):
        assert str(path) == "D:\\"
        return {
            "kind": "explorer_directory",
            "verified": self.foreground_hwnd == self.d_hwnd,
            "foreground_hwnd": self.foreground_hwnd,
            "matching_hwnds": (
                (self.d_hwnd,) if self.foreground_hwnd == self.d_hwnd else ()
            ),
        }

    def open_d_drive(self) -> None:
        self.foreground_hwnd = self.d_hwnd

    def add_explorer_window(self) -> None:
        self.windows.append(
            SimpleNamespace(
                hwnd=3,
                process_name="explorer.exe",
                class_name="CabinetWClass",
            )
        )

    def activate_window(self, hwnd):
        self.activate_calls.append(hwnd)
        self.foreground_hwnd = hwnd
        return SimpleNamespace(hwnd=hwnd)

    def assert_foreground(self, hwnd):
        assert self.foreground_hwnd == hwnd


def fake_settings():
    settings = load_settings(
        Path("C:/HandsFreePC/__scenario_test_missing__.yaml"),
        allow_missing=True,
    )
    settings.computer_control.enabled = True
    settings.computer_control.engine = "assistive_v1"
    settings.computer_control.failure_policy = "continue"
    settings.execution.dry_run = False
    return settings


def ticking_clock(*values: float):
    remaining = iter(values)
    return lambda: next(remaining)


def test_catalog_contains_the_required_live_scenarios() -> None:
    assert [item.scenario for item in scenarios.SCENARIOS] == [
        "claude_foreground_zero_action",
        "explorer_from_no_window",
        "open_d_drive",
        "chrome_google_compound",
        "wechat_delayed_transition",
        "native_retryable_fallback",
        "wechat_open_conversation",
        "queue_continues_after_failure",
        "open_workmap_folder",
        "chrome_chatgpt_draft",
        "wechat_send_file_self",
    ]
    assert scenarios.SCENARIOS[0].max_elapsed_seconds == 1.0
    assert scenarios.SCENARIOS[1].max_elapsed_seconds == 3.0
    assert scenarios.SCENARIOS[2].max_elapsed_seconds == 4.0
    assert scenarios.SCENARIOS[3].max_elapsed_seconds == 6.0


def test_command_simulate_uses_the_shared_no_audio_text_injection_facade(
    monkeypatch,
    capsys,
) -> None:
    calls: list[tuple[str, bool, bool]] = []
    original_inject = scenarios.inject_runtime_text

    class FakeRuntime:
        def __init__(self, *_args, **_kwargs) -> None:
            self.state = SimpleNamespace(value="armed")
            self.pending_plan = None

        def handle_text(self, text, *, require_wake):
            calls.append((text, require_wake, False))
            return SimpleNamespace(
                handled=True,
                state=self.state,
                message="simulated",
                success=True,
                plan=None,
            )

        @staticmethod
        def stop() -> None:
            return None

    def observed_inject(runtime, text, **kwargs):
        calls.append((text, kwargs.get("require_wake", False), True))
        return original_inject(runtime, text, **kwargs)

    monkeypatch.setattr("handsfree_pc.runtime.VoiceRuntime", FakeRuntime)
    monkeypatch.setattr(cli, "load_settings", lambda *_args, **_kwargs: fake_settings())
    monkeypatch.setattr(cli, "_build_executor", lambda _settings: object())
    monkeypatch.setattr(scenarios, "inject_runtime_text", observed_inject)

    exit_code = cli.command_simulate(
        SimpleNamespace(
            config=None,
            text=["切换到 Claude"],
            file=None,
            independent=False,
            require_wake=False,
        )
    )

    assert exit_code == 0
    assert calls == [
        ("切换到 Claude", False, True),
        ("切换到 Claude", False, False),
    ]
    assert json.loads(capsys.readouterr().out)[0]["success"] is True


def test_live_scenario_calls_controller_with_real_text_and_reports_required_fields(
    monkeypatch,
) -> None:
    controller = FakeController([ComputerControlResult(True, "goal already satisfied")])
    monkeypatch.setattr(scenarios, "_check_precondition", lambda *_args, **_kwargs: None)

    report = scenarios.run_scenarios(
        fake_settings(),
        controller,
        scenario_names=["claude_foreground_zero_action"],
        monotonic=ticking_clock(10.0, 10.4),
    )

    assert controller.calls == ["切换到 Claude"]
    assert controller.closed is False
    assert report["all_success"] is True
    assert report["records"] == [
        {
            "scenario": "claude_foreground_zero_action",
            "result": "passed",
            "success": True,
            "elapsed_seconds": 0.4,
            "retry_count": 0,
            "failure_stage": None,
            "message": "goal already satisfied",
            "attempt": 1,
            "executed": True,
            "validation_boundary": (
                "VoiceRuntime.handle_session_text→CommandWorker→controller; "
                "no ASR audio"
            ),
            "pre_state_strategy": "declared_precondition",
        }
    ]


def test_elapsed_threshold_is_strict_and_cannot_be_reported_as_success(monkeypatch) -> None:
    controller = FakeController([ComputerControlResult(True, "completed")])
    monkeypatch.setattr(scenarios, "_check_precondition", lambda *_args, **_kwargs: None)

    report = scenarios.run_scenarios(
        fake_settings(),
        controller,
        scenario_names=["chrome_google_compound"],
        monotonic=ticking_clock(1.0, 7.0),
    )

    record = report["records"][0]
    assert record["result"] == "failed"
    assert record["success"] is False
    assert record["failure_stage"] == "elapsed_threshold"
    assert report["all_success"] is False


def test_missing_precondition_is_explicit_skip_and_never_success() -> None:
    report = scenarios.run_scenarios(
        fake_settings(),
        FakeController(),
        scenario_names=["claude_foreground_zero_action"],
        native=None,
    )

    record = report["records"][0]
    assert record["result"] == "skipped"
    assert record["success"] is False
    assert record["failure_stage"] == "precondition"
    assert "foreground inspection" in record["message"]
    assert record["executed"] is False
    assert report["summary"][0]["executed_attempts"] == 0
    assert report["summary"][0]["skipped"] == 1


def test_native_fallback_rejects_success_without_a_generic_planner_call(monkeypatch) -> None:
    class FallbackController(FakeController):
        def run(self, instruction, *, cancel_event=None):
            del cancel_event
            self.calls.append(instruction)
            native_result = self.native_router.route(instruction)
            assert native_result.status == NativeRouteStatus.RETRYABLE_FAILURE
            self.driver.execute()
            return ComputerControlResult(True, "generic assistive fallback verified")

    controller = FallbackController()
    monkeypatch.setattr(scenarios, "_check_precondition", lambda *_args, **_kwargs: None)

    report = scenarios.run_scenarios(
        fake_settings(),
        controller,
        scenario_names=["native_retryable_fallback"],
        monotonic=ticking_clock(2.0, 3.0),
    )

    assert report["records"][0]["result"] == "failed"
    assert report["records"][0]["failure_stage"] == "generic_fallback"
    assert report["records"][0]["planner_decide_calls"] == 0
    assert "handsfreepc-scenario-" in controller.calls[0]


def test_native_failure_requires_generic_planner_before_success(monkeypatch) -> None:
    class FallbackController(FakeController):
        def run(self, instruction, *, cancel_event=None):
            del cancel_event
            self.calls.append(instruction)
            native_result = self.native_router.route(instruction)
            assert native_result.status == NativeRouteStatus.RETRYABLE_FAILURE
            self.planner.decide(instruction)
            self.driver.execute()
            return ComputerControlResult(True, "generic assistive fallback verified")

    controller = FallbackController()
    monkeypatch.setattr(scenarios, "_check_precondition", lambda *_args, **_kwargs: None)

    report = scenarios.run_scenarios(
        fake_settings(),
        controller,
        scenario_names=["native_retryable_fallback"],
        monotonic=ticking_clock(2.0, 3.0),
    )

    assert report["records"][0]["result"] == "passed"
    assert report["records"][0]["planner_decide_calls"] == 1


def test_queue_continuation_uses_runtime_ingress_callbacks_then_next_success(
    monkeypatch,
) -> None:
    controller = FakeController(
        [
            ComputerControlResult(False, "missing target", stage="goal_verification"),
            ComputerControlResult(True, "Claude foreground"),
        ],
        execute_action_on_run=True,
    )
    monkeypatch.setattr(scenarios, "_check_precondition", lambda *_args, **_kwargs: None)
    injected: list[tuple[str, bool]] = []
    original_inject = scenarios.inject_runtime_text

    def observed_inject(runtime, text, **kwargs):
        injected.append((text, kwargs.get("continuous_control") is True))
        return original_inject(runtime, text, **kwargs)

    monkeypatch.setattr(scenarios, "inject_runtime_text", observed_inject)

    report = scenarios.run_scenarios(
        fake_settings(),
        controller,
        scenario_names=["queue_continues_after_failure"],
        monotonic=ticking_clock(0.0, 0.5),
    )

    record = report["records"][0]
    assert record["result"] == "passed"
    assert record["success"] is True
    assert len(controller.calls) == 2
    assert controller.calls[1] == "切换到 Claude"
    assert controller.closed is False
    assert [item[1] for item in injected] == [True, True]
    assert "__handsfreepc_scenario_missing_" in injected[0][0]
    assert injected[1][0] == "切换到 Claude"
    assert record["validation_boundary"] == (
        "VoiceRuntime.handle_session_text→CommandWorker→controller; no ASR audio"
    )
    assert report["summary"][0]["validation_boundary"] == (
        "VoiceRuntime.handle_session_text→CommandWorker→controller; no ASR audio"
    )


def test_repeat_is_bounded_and_summary_exposes_95_percent_gate(monkeypatch) -> None:
    native = FakeNative()
    controller = FakeController(
        [ComputerControlResult(True, "completed")] * 2,
        execute_action_on_run=True,
        on_run=lambda _instruction: native.open_d_drive(),
    )
    monkeypatch.setattr(scenarios, "_check_precondition", lambda *_args, **_kwargs: None)

    report = scenarios.run_scenarios(
        fake_settings(),
        controller,
        scenario_names=["open_d_drive"],
        repeat=2,
        native=native,
        monotonic=ticking_clock(0.0, 0.1, 0.1, 0.2),
    )

    assert len(report["records"]) == 2
    summary = report["summary"][0]
    assert summary["requested_attempts"] == 2
    assert summary["executed_attempts"] == 2
    assert summary["already_satisfied"] == 0
    assert summary["skipped"] == 0
    assert summary["success_rate"] == 1.0
    assert summary["reached_requested_repeat"] is True
    assert summary["meets_95_percent"] is True
    assert native.activate_calls == [1, 1]
    assert all(item["state_restored"] is True for item in report["records"])


def test_explorer_repeat_marks_later_existing_window_already_satisfied(monkeypatch) -> None:
    native = FakeNative()
    controller = FakeController(
        [ComputerControlResult(True, "Explorer launched")],
        execute_action_on_run=True,
        on_run=lambda _instruction: native.add_explorer_window(),
    )
    monkeypatch.setattr(scenarios, "_check_precondition", lambda *_args, **_kwargs: None)

    report = scenarios.run_scenarios(
        fake_settings(),
        controller,
        scenario_names=["explorer_from_no_window"],
        repeat=2,
        native=native,
        monotonic=ticking_clock(0.0, 0.1),
    )

    assert controller.calls == ["打开资源管理器"]
    assert [item["result"] for item in report["records"]] == [
        "passed",
        "already_satisfied",
    ]
    assert [item["executed"] for item in report["records"]] == [True, False]
    summary = report["summary"][0]
    assert summary["executed_attempts"] == 1
    assert summary["already_satisfied"] == 1
    assert summary["success_rate"] == 1.0
    assert summary["reached_requested_repeat"] is False
    assert summary["meets_95_percent"] is False
    assert report["all_success"] is False


def test_open_drive_without_recoverable_foreground_cannot_fake_repeat_gate(
    monkeypatch,
) -> None:
    native = FakeNative(foreground_hwnd=None)
    controller = FakeController(
        [ComputerControlResult(True, "D drive opened")],
        execute_action_on_run=True,
        on_run=lambda _instruction: native.open_d_drive(),
    )
    monkeypatch.setattr(scenarios, "_check_precondition", lambda *_args, **_kwargs: None)

    report = scenarios.run_scenarios(
        fake_settings(),
        controller,
        scenario_names=["open_d_drive"],
        repeat=2,
        native=native,
        monotonic=ticking_clock(0.0, 0.1),
    )

    assert len(controller.calls) == 1
    assert report["records"][0]["state_restored"] is False
    assert report["records"][1]["result"] == "already_satisfied"
    summary = report["summary"][0]
    assert summary["executed_attempts"] == 1
    assert summary["already_satisfied"] == 1
    assert summary["reached_requested_repeat"] is False
    assert summary["meets_95_percent"] is False
    assert report["all_success"] is False


def test_chrome_repeat_uses_a_fresh_google_url_for_every_executed_attempt(
    monkeypatch,
) -> None:
    controller = FakeController(
        [ComputerControlResult(True, "Google loaded")] * 2,
        execute_action_on_run=True,
    )
    monkeypatch.setattr(scenarios, "_check_precondition", lambda *_args, **_kwargs: None)

    report = scenarios.run_scenarios(
        fake_settings(),
        controller,
        scenario_names=["chrome_google_compound"],
        repeat=2,
        monotonic=ticking_clock(0.0, 0.1, 0.1, 0.2),
    )

    assert len(set(controller.calls)) == 2
    assert all("handsfreepc-scenario-" in item for item in controller.calls)
    assert report["summary"][0]["executed_attempts"] == 2
    assert report["summary"][0]["meets_95_percent"] is True


def test_verified_unsatisfied_prestate_rejects_zero_action_success(monkeypatch) -> None:
    controller = FakeController([ComputerControlResult(True, "claimed complete")])
    monkeypatch.setattr(scenarios, "_check_precondition", lambda *_args, **_kwargs: None)

    report = scenarios.run_scenarios(
        fake_settings(),
        controller,
        scenario_names=["chrome_google_compound"],
        monotonic=ticking_clock(0.0, 0.1),
    )

    record = report["records"][0]
    assert record["result"] == "failed"
    assert record["success"] is False
    assert record["executed"] is True
    assert record["failure_stage"] == "execution_evidence"
    assert report["summary"][0]["passed"] == 0
    assert report["summary"][0]["meets_95_percent"] is False


def test_report_file_contains_the_same_json_payload(tmp_path) -> None:
    report = scenarios.unavailable_report(
        scenario_names=["open_d_drive"],
        repeat=1,
        reason="D drive absent",
    )
    destination = tmp_path / "reports" / "assistive.json"

    scenarios.write_report(report, destination)

    assert json.loads(destination.read_text(encoding="utf-8")) == report


def test_cli_parser_supports_selection_repeat_report_and_list(tmp_path) -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "scenarios",
            "--scenario",
            "open_d_drive",
            "--repeat",
            "20",
            "--report",
            str(tmp_path / "report.json"),
        ]
    )
    listed = parser.parse_args(["scenarios", "--list"])

    assert args.func is cli.command_scenarios
    assert args.scenario == ["open_d_drive"]
    assert args.repeat == 20
    assert args.report == str(tmp_path / "report.json")
    assert listed.list is True


def test_command_returns_nonzero_for_skipped_configuration_and_writes_report(
    tmp_path, monkeypatch, capsys
) -> None:
    settings = fake_settings()
    settings.execution.dry_run = True
    destination = tmp_path / "skipped.json"
    monkeypatch.setattr(cli, "load_settings", lambda *_args, **_kwargs: settings)
    monkeypatch.setattr(scenarios.platform, "system", lambda: "Windows")
    args = SimpleNamespace(
        config=None,
        scenario=["open_d_drive"],
        repeat=1,
        report=str(destination),
        list=False,
    )

    assert cli.command_scenarios(args) == 2

    printed = json.loads(capsys.readouterr().out)
    saved = json.loads(destination.read_text(encoding="utf-8"))
    assert printed == saved
    assert printed["records"][0]["result"] == "skipped"


def test_native_failure_handled_by_a_deterministic_assistive_skill_counts_as_fallback(
    monkeypatch,
) -> None:
    class SkillFallbackController(FakeController):
        def run(self, instruction, *, cancel_event=None):
            del cancel_event
            self.calls.append(instruction)
            native_result = self.native_router.route(instruction)
            assert native_result.status == NativeRouteStatus.RETRYABLE_FAILURE
            self.driver.execute()
            self.last_metrics = {"retry_count": 0, "action_count": 2, "native_fallbacks": 1}
            return ComputerControlResult(True, "deterministic browser skill verified")

    controller = SkillFallbackController()
    monkeypatch.setattr(scenarios, "_check_precondition", lambda *_args, **_kwargs: None)

    report = scenarios.run_scenarios(
        fake_settings(),
        controller,
        scenario_names=["native_retryable_fallback"],
        monotonic=ticking_clock(2.0, 3.0),
    )

    record = report["records"][0]
    assert record["result"] == "passed"
    assert record["planner_decide_calls"] == 0
    assert record["native_fallbacks"] == 1
