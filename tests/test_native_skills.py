from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import handsfree_pc.desktop.native_skills as native_skills
from handsfree_pc.desktop.native_skills import NativeRouteStatus, NativeSkillRouter
from handsfree_pc.models import Action, ActionType, ExecutionResult, Plan


class RecordingExecutor:
    def __init__(self) -> None:
        self.prepared: list[Plan] = []
        self.executed: list[Plan] = []
        self.prepared_plan: Plan | None = None
        self.results: list[ExecutionResult] | None = None

    def prepare_plan(self, plan: Plan) -> Plan:
        self.prepared.append(plan)
        return self.prepared_plan or plan

    def execute_plan(self, plan: Plan) -> list[ExecutionResult]:
        self.executed.append(plan)
        if self.results is not None:
            return self.results
        return [
            ExecutionResult(True, "verified", action=action, evidence={"verified": True})
            for action in plan.actions
        ]


class FixedParser:
    def __init__(self, plan: Plan) -> None:
        self.plan = plan

    def parse(self, _text: str) -> Plan:
        return self.plan

    def covers_full_text(self, _text: str, _plan: Plan) -> bool:
        return True


class FixedWorkMapIndex:
    def __init__(self, target) -> None:
        self.target = target

    def resolve_open_request(self, text: str):
        return self.target if text == "打开示例数据库" else None


def test_route_miss_never_prepares_or_executes(settings) -> None:
    executor = RecordingExecutor()
    router = NativeSkillRouter(settings, executor=executor)

    result = router.route("把这个陌生软件里最好看的东西找出来")

    assert result.status == NativeRouteStatus.MISS
    assert result.matched is False
    assert result.message.startswith("NATIVE_ROUTE_MISS")
    assert executor.prepared == []
    assert executor.executed == []


def test_exact_workmap_alias_routes_before_generic_app_scope_and_is_verified(
    settings,
    tmp_path,
) -> None:
    target = tmp_path / "processed_data"
    target.mkdir()
    executor = RecordingExecutor()
    router = NativeSkillRouter(
        settings,
        executor=executor,
        workmap_index=FixedWorkMapIndex(target),
    )

    result = router.route("打开示例数据库")

    assert result.status == NativeRouteStatus.SUCCEEDED
    assert router.can_route("打开示例数据库") is True
    assert executor.executed[0].source == "workmap"
    assert executor.executed[0].actions == [
        Action(ActionType.OPEN_PATH, path=str(target.resolve()))
    ]


def test_workmap_never_partially_consumes_a_multi_clause_request(settings, tmp_path) -> None:
    target = tmp_path / "processed_data"
    target.mkdir()
    executor = RecordingExecutor()
    router = NativeSkillRouter(
        settings,
        executor=executor,
        workmap_index=FixedWorkMapIndex(target),
    )

    result = router.route("打开示例数据库，然后删除文件")

    assert result.status == NativeRouteStatus.MISS
    assert executor.executed == []


def test_partial_app_parse_falls_through_instead_of_reporting_partial_success(settings) -> None:
    executor = RecordingExecutor()
    router = NativeSkillRouter(settings, executor=executor)

    result = router.route("在 Claude 里点击左侧 Projects，然后滚动到底部")

    assert result.status == NativeRouteStatus.MISS
    assert executor.prepared == []
    assert executor.executed == []


def test_english_partial_app_parse_falls_through_to_generic_agent(settings) -> None:
    executor = RecordingExecutor()
    router = NativeSkillRouter(settings, executor=executor)

    result = router.route("Open Claude and click Code")

    assert result.status == NativeRouteStatus.MISS
    assert executor.prepared == []
    assert executor.executed == []


def test_partial_path_parse_falls_through_when_later_text_entry_is_requested(settings) -> None:
    executor = RecordingExecutor()
    router = NativeSkillRouter(settings, executor=executor)

    result = router.route("打开 D 盘，然后在搜索框输入年度报告")

    assert result.status == NativeRouteStatus.MISS
    assert executor.prepared == []
    assert executor.executed == []


def test_app_prefix_never_executes_when_later_request_is_not_consumed(settings) -> None:
    commands = [
        "打开 Codex，然后最大化窗口",
        "打开 Codex，然后保存当前文件",
        "打开 Codex，然后把当前聊天归档",
        "打开 Codex，然后退出账户",
        "打开 Codex，然后创建新对话",
    ]

    for command in commands:
        executor = RecordingExecutor()
        router = NativeSkillRouter(settings, executor=executor)

        result = router.route(command)

        assert result.status == NativeRouteStatus.MISS
        assert router.can_route(command) is False
        assert executor.prepared == []
        assert executor.executed == []


def test_safe_native_plan_is_prepared_checked_and_executed(settings) -> None:
    executor = RecordingExecutor()
    router = NativeSkillRouter(settings, executor=executor)

    result = router.route("切换到屏幕反馈")

    assert result.status == NativeRouteStatus.SUCCEEDED
    assert result.success is True
    assert len(executor.prepared) == 1
    assert len(executor.executed) == 1
    assert executor.executed[0].source == "deterministic"
    assert result.execution_results[0].evidence["verified"] is True


def test_prepared_target_is_safety_checked_before_execution(settings) -> None:
    executor = RecordingExecutor()
    executor.prepared_plan = Plan(
        "resolved installer",
        [Action(ActionType.OPEN_PATH, path=r"C:\\safe-looking-name.exe")],
    )
    router = NativeSkillRouter(settings, executor=executor)

    result = router.route("打开下载里的safe-looking-name")

    assert result.status == NativeRouteStatus.CONFIRMATION_REQUIRED
    assert result.needs_confirmation is True
    assert executor.executed == []


def test_safe_native_path_is_rebound_at_the_final_execution_boundary(
    settings, tmp_path, monkeypatch
) -> None:
    target = tmp_path / "fixture.exe"
    target.mkdir()
    plan = Plan("open fixture directory", [Action(ActionType.OPEN_PATH, path=str(target))])
    executor = RecordingExecutor()
    router = NativeSkillRouter(settings, parser=FixedParser(plan), executor=executor)
    entered = threading.Event()
    release = threading.Event()
    original_guard = native_skills.guard_plan_paths

    @contextmanager
    def blocking_guard(bound_plan: Plan, digest: str) -> Iterator[None]:
        entered.set()
        assert release.wait(timeout=2)
        with original_guard(bound_plan, digest):
            yield

    monkeypatch.setattr(native_skills, "guard_plan_paths", blocking_guard)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(router.route, "打开 fixture")
        assert entered.wait(timeout=2)
        target.rmdir()
        target.write_bytes(b"replacement executable")
        release.set()
        result = future.result(timeout=2)

    assert result.status == NativeRouteStatus.FAILED
    assert result.message.startswith("NATIVE_EXECUTION_FAILED")
    assert executor.executed == []


def test_unconsumed_blocked_keyword_falls_through_without_native_execution(settings) -> None:
    executor = RecordingExecutor()
    router = NativeSkillRouter(settings, executor=executor)

    result = router.route("打开 Codex 然后删除所有东西")

    assert result.status == NativeRouteStatus.MISS
    assert executor.prepared == []
    assert executor.executed == []


def test_negated_native_commands_never_execute(settings) -> None:
    commands = (
        "不要打开 Claude",
        "别打开 Codex",
        "我不想用 Claude",
        "不要打开 D盘的报告",
        "不要开启 Claude 的原生语音",
    )

    for command in commands:
        executor = RecordingExecutor()
        router = NativeSkillRouter(settings, executor=executor)

        result = router.route(command)

        assert result.status == NativeRouteStatus.MISS
        assert router.can_route(command) is False
        assert executor.prepared == []
        assert executor.executed == []


def test_unmapped_native_modes_never_prepare_or_click(settings) -> None:
    for mode in ("Settings", "Telemetry", "Account", "Upgrade", "Share"):
        executor = RecordingExecutor()
        router = NativeSkillRouter(settings, executor=executor)
        command = f"打开 Claude，开启一个 {mode}"

        result = router.route(command)

        assert result.status == NativeRouteStatus.MISS
        assert router.can_route(command) is False
        assert executor.prepared == []
        assert executor.executed == []


def test_explicitly_mapped_design_mode_remains_native(settings) -> None:
    executor = RecordingExecutor()
    router = NativeSkillRouter(settings, executor=executor)

    result = router.route("打开 Claude，开启一个 Design")

    assert result.status == NativeRouteStatus.SUCCEEDED
    assert len(executor.executed) == 1


def test_failed_action_is_not_reported_as_verified_completion(settings) -> None:
    executor = RecordingExecutor()
    failed_action = Action(ActionType.SET_FEEDBACK_MODE, feedback_mode=settings.app.feedback_mode)
    executor.results = [ExecutionResult(False, "postcondition missing", action=failed_action)]
    router = NativeSkillRouter(settings, executor=executor)

    result = router.route("切换到屏幕反馈")

    assert result.status == NativeRouteStatus.FAILED
    assert result.success is False
    assert result.message == "NATIVE_EXECUTION_FAILED: postcondition missing"


def test_short_execution_result_is_failure(settings) -> None:
    executor = RecordingExecutor()
    executor.results = []
    router = NativeSkillRouter(settings, executor=executor)

    result = router.route("打开 Claude 到 Chat 选项卡")

    assert result.status == NativeRouteStatus.FAILED
    assert result.execution_results == ()
    assert executor.executed


def test_unverified_native_side_effects_always_fall_through(settings) -> None:
    for command in ("发送提示", "打开codex并使用应用内语音"):
        executor = RecordingExecutor()
        router = NativeSkillRouter(settings, executor=executor)

        result = router.route(command, explicit_submission=True)

        assert result.status == NativeRouteStatus.MISS
        assert router.can_route(command) is False
        assert executor.prepared == []
        assert executor.executed == []


def test_preparation_failure_is_closed_without_execution(settings) -> None:
    class FailingExecutor(RecordingExecutor):
        def prepare_plan(self, plan: Plan) -> Plan:
            self.prepared.append(plan)
            raise ValueError("ambiguous target")

    executor = FailingExecutor()
    router = NativeSkillRouter(settings, executor=executor)

    result = router.route("打开下载里的文件")

    assert result.status == NativeRouteStatus.FAILED
    assert result.plan is not None
    assert result.plan.risk.value == "blocked"
    assert executor.executed == []
