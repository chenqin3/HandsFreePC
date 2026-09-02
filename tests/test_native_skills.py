from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import handsfree_pc.desktop.native_skills as native_skills
from handsfree_pc.desktop.native_skills import NativeRouteStatus, NativeSkillRouter
from handsfree_pc.models import Action, ActionType, ExecutionResult, Plan
from handsfree_pc.paths import PathResolver
from handsfree_pc.windows.executor import WindowsExecutor


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


class NestedWorkMapIndex:
    def __init__(self, root) -> None:
        self.root = root

    @staticmethod
    def resolve_open_request(_text: str):
        return None

    def resolve_unique_name(self, query: str):
        return self.root if query in {"招聘数据库", "招聘数剧库"} else None


class SemanticWorkMapIndex:
    def __init__(self, candidates) -> None:
        self.candidates = dict(candidates)
        self.hint_calls = 0

    @staticmethod
    def resolve_open_request(_text: str):
        return None

    @staticmethod
    def resolve_unique_name(_query: str):
        return None

    def planner_hints(
        self,
        _query: str,
        *,
        limit: int,
        minimum_score: float,
        available_only: bool,
    ):
        self.hint_calls += 1
        assert minimum_score == 0.0
        assert available_only is True
        return tuple(
            {
                "target_id": candidate_id,
                "display_name": display_name,
                "project_name": display_name,
                "summary": f"{display_name} 的摘要",
                "score": 0.7,
                "has_local_root": True,
                "target_available": True,
            }
            for candidate_id, (display_name, _path) in list(self.candidates.items())[:limit]
        )

    def resolve_candidate_id(self, candidate_id: str):
        value = self.candidates.get(candidate_id)
        return value[1] if value is not None else None


class FakeWorkMapSelector:
    backend = "codex"

    def __init__(
        self,
        selected_id=None,
        *,
        error=None,
        on_select=None,
        selected_path_basename=None,
        path_selected_id=None,
        path_error=None,
        on_path_select=None,
    ) -> None:
        self.selected_id = selected_id
        self.error = error
        self.on_select = on_select
        self.calls = 0
        self.selected_path_basename = selected_path_basename
        self.path_selected_id = path_selected_id
        self.path_error = path_error
        self.on_path_select = on_path_select
        self.path_calls = 0
        self.path_requests = []

    def select(self, _text, _candidates, *, cancel_event=None):
        self.calls += 1
        if self.on_select is not None:
            self.on_select()
        if self.error is not None:
            raise self.error
        return self.selected_id

    def select_path_child(
        self,
        text,
        spoken_component,
        candidates,
        *,
        cancel_event=None,
    ):
        self.path_calls += 1
        self.path_requests.append((text, spoken_component, tuple(candidates), cancel_event))
        if self.on_path_select is not None:
            self.on_path_select()
        if self.path_error is not None:
            raise self.path_error
        if self.path_selected_id is not None:
            return self.path_selected_id
        desired_basename = self.selected_path_basename
        if isinstance(desired_basename, dict):
            desired_basename = desired_basename.get(spoken_component)
        for candidate in candidates:
            if candidate["basename"] == desired_basename:
                return candidate["candidate_id"]
        return None


def _enable_codex_workmap_selection(settings) -> None:
    settings.computer_control.enabled = True
    settings.computer_control.planner_backend = "codex_cli_best_effort"
    settings.computer_control.allow_screen_context_to_cloud = True
    settings.computer_control.allow_codex_cli_host_read = True
    settings.privacy.allow_cloud_planner = True


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


def test_verified_directory_context_supports_one_layer_per_utterance(settings, tmp_path) -> None:
    first = tmp_path / "年度招聘资料"
    second = first / "已处理数据"
    second.mkdir(parents=True)
    executor = RecordingExecutor()
    router = NativeSkillRouter(
        settings,
        executor=executor,
        workmap_index=FixedWorkMapIndex(tmp_path),
    )

    root_result = router.route("打开示例数据库")
    first_result = router.route("进入年度招骋资料文件夹")
    second_result = router.route("进入已处理数据文件夹")

    assert root_result.status == NativeRouteStatus.SUCCEEDED
    assert first_result.status == NativeRouteStatus.SUCCEEDED
    assert second_result.status == NativeRouteStatus.SUCCEEDED
    assert executor.executed[-1].source == "path_context"
    assert executor.executed[-1].actions[0].path == str(second.resolve())


def test_contextual_path_ambiguity_falls_through_without_execution(settings, tmp_path) -> None:
    (tmp_path / "年度报告一").mkdir()
    (tmp_path / "年度报告二").mkdir()
    executor = RecordingExecutor()
    router = NativeSkillRouter(
        settings,
        executor=executor,
        workmap_index=FixedWorkMapIndex(tmp_path),
    )
    assert router.route("打开示例数据库").status == NativeRouteStatus.SUCCEEDED
    executed_before = len(executor.executed)

    result = router.route("进入年度报告")

    assert result.status == NativeRouteStatus.MISS
    assert len(executor.executed) == executed_before


def test_explicit_app_command_wins_over_same_named_folder_in_path_context(
    settings,
    tmp_path,
) -> None:
    (tmp_path / "Claude").mkdir()
    executor = RecordingExecutor()
    router = NativeSkillRouter(
        settings,
        executor=executor,
        workmap_index=FixedWorkMapIndex(tmp_path),
    )
    assert router.route("打开示例数据库").status == NativeRouteStatus.SUCCEEDED

    result = router.route("打开 Claude")

    assert result.status == NativeRouteStatus.SUCCEEDED
    assert executor.executed[-1].actions == [Action(ActionType.ACTIVATE_APP, app="claude")]


def test_workmap_root_and_deep_relative_tail_are_both_locally_verified(
    settings,
    tmp_path,
) -> None:
    root = tmp_path / "recruitment-database"
    target = root / "年度招聘资料" / "已处理数据"
    target.mkdir(parents=True)
    executor = RecordingExecutor()
    router = NativeSkillRouter(
        settings,
        executor=executor,
        workmap_index=NestedWorkMapIndex(root),
    )

    result = router.route("打开招聘数剧库下面的年度招骋资料文件夹里的已处理数据")

    assert result.status == NativeRouteStatus.SUCCEEDED
    assert executor.executed[-1].source == "workmap_path"
    assert executor.executed[-1].actions[0].path == str(target.resolve())


def test_semantic_child_selects_second_workmap_layer_and_continues_one_shot_tail(
    settings,
    tmp_path,
) -> None:
    root = tmp_path / "recruitment-root"
    (root / "年度报告甲").mkdir(parents=True)
    target = root / "年度报告乙" / "已处理数据"
    target.mkdir(parents=True)
    selector = FakeWorkMapSelector(selected_path_basename="年度报告乙")
    executor = RecordingExecutor()
    _enable_codex_workmap_selection(settings)
    router = NativeSkillRouter(
        settings,
        executor=executor,
        workmap_index=NestedWorkMapIndex(root),
        workmap_selector=selector,
    )

    result = router.route("打开招聘数据库下面的年度报告文件夹里的已处理数据")

    assert result.status == NativeRouteStatus.SUCCEEDED
    assert selector.path_calls == 1
    assert selector.path_requests[0][1] == "年度报告"
    assert executor.executed[-1].source == "workmap_path_semantic"
    assert executor.executed[-1].actions[0].path == str(target.resolve())


def test_semantic_child_selects_an_ambiguous_third_layer(settings, tmp_path) -> None:
    root = tmp_path / "recruitment-root"
    parent = root / "年度招聘资料"
    (parent / "数据成果甲").mkdir(parents=True)
    target = parent / "数据成果乙"
    target.mkdir()
    selector = FakeWorkMapSelector(selected_path_basename="数据成果乙")
    executor = RecordingExecutor()
    _enable_codex_workmap_selection(settings)
    router = NativeSkillRouter(
        settings,
        executor=executor,
        workmap_index=NestedWorkMapIndex(root),
        workmap_selector=selector,
    )

    result = router.route("打开招聘数据库下面的年度招聘资料文件夹里的数据成果")

    assert result.status == NativeRouteStatus.SUCCEEDED
    assert selector.path_calls == 1
    assert selector.path_requests[0][1] == "数据成果"
    assert executor.executed[-1].actions[0].path == str(target.resolve())


def test_absolute_deep_path_uses_fresh_opaque_ids_at_each_ambiguous_layer(
    settings,
    tmp_path,
) -> None:
    base = tmp_path / "deep-root"
    (base / "部门资料甲").mkdir(parents=True)
    chosen_first = base / "部门资料乙"
    (chosen_first / "年度报告甲").mkdir(parents=True)
    target = chosen_first / "年度报告乙" / "最终文件.txt"
    target.parent.mkdir()
    target.write_text("verified", encoding="utf-8")
    selector = FakeWorkMapSelector(
        selected_path_basename={
            "部门资料": "部门资料乙",
            "年度报告": "年度报告乙",
        }
    )
    executor = RecordingExecutor()
    _enable_codex_workmap_selection(settings)
    router = NativeSkillRouter(
        settings,
        executor=executor,
        workmap_selector=selector,
        path_resolver=PathResolver(threshold=0.70),
    )

    result = router.route(f"打开 {base}\\部门资料\\年度报告\\最终文件.txt")

    assert result.status == NativeRouteStatus.SUCCEEDED
    assert selector.path_calls == 2
    assert [request[1] for request in selector.path_requests] == ["部门资料", "年度报告"]
    id_rounds = [
        {candidate["candidate_id"] for candidate in request[2]}
        for request in selector.path_requests
    ]
    assert id_rounds[0].isdisjoint(id_rounds[1])
    assert all(
        candidate_id.startswith("pc-") and len(candidate_id) == 23
        for candidate_ids in id_rounds
        for candidate_id in candidate_ids
    )
    assert all(
        set(candidate) == {"candidate_id", "basename", "type", "score"}
        for request in selector.path_requests
        for candidate in request[2]
    )
    assert executor.executed[-1].source == "path_semantic"
    assert executor.executed[-1].actions[0].path == str(target.resolve())


def test_semantic_child_supports_one_verified_context_layer_per_utterance(
    settings,
    tmp_path,
) -> None:
    (tmp_path / "年度报告甲").mkdir()
    target = tmp_path / "年度报告乙"
    target.mkdir()
    selector = FakeWorkMapSelector(selected_path_basename="年度报告乙")
    executor = RecordingExecutor()
    _enable_codex_workmap_selection(settings)
    router = NativeSkillRouter(
        settings,
        executor=executor,
        workmap_index=FixedWorkMapIndex(tmp_path),
        workmap_selector=selector,
    )
    assert router.route("打开示例数据库").status == NativeRouteStatus.SUCCEEDED

    result = router.route("进入年度报告")

    assert result.status == NativeRouteStatus.SUCCEEDED
    assert selector.path_calls == 1
    assert result.plan is not None and result.plan.source == "path_context_semantic"
    assert executor.executed[-1].actions[0].path == str(target.resolve())


def test_semantic_child_forged_null_exception_and_stale_choices_never_execute(
    settings,
    tmp_path,
) -> None:
    _enable_codex_workmap_selection(settings)
    variants = ("forged", "null", "exception", "stale")
    for variant in variants:
        root = tmp_path / variant
        (root / "年度报告甲").mkdir(parents=True)
        stale_target = root / "年度报告乙"
        stale_target.mkdir()
        if variant == "forged":
            selector = FakeWorkMapSelector(path_selected_id="pc-99999999999999999999")
        elif variant == "exception":
            selector = FakeWorkMapSelector(path_error=RuntimeError("cli failed"))
        elif variant == "stale":
            selector = FakeWorkMapSelector(
                selected_path_basename="年度报告乙",
                on_path_select=stale_target.rmdir,
            )
        else:
            selector = FakeWorkMapSelector()
        executor = RecordingExecutor()
        router = NativeSkillRouter(
            settings,
            executor=executor,
            workmap_index=FixedWorkMapIndex(root),
            workmap_selector=selector,
        )
        assert router.route("打开示例数据库").status == NativeRouteStatus.SUCCEEDED
        executed_before = len(executor.executed)

        result = router.route("进入年度报告")

        assert result.status == NativeRouteStatus.MISS
        assert selector.path_calls == 1
        assert len(executor.executed) == executed_before


def test_semantic_child_identity_replacement_during_prepare_fails_closed(
    settings,
    tmp_path,
) -> None:
    root = tmp_path / "recruitment-root"
    (root / "年度报告甲").mkdir(parents=True)
    selected = root / "年度报告乙"
    selected.mkdir()

    class SwappingPrepareExecutor(RecordingExecutor):
        def prepare_plan(self, plan: Plan) -> Plan:
            selected.rmdir()
            selected.mkdir()
            return super().prepare_plan(plan)

    selector = FakeWorkMapSelector(selected_path_basename="年度报告乙")
    executor = SwappingPrepareExecutor()
    _enable_codex_workmap_selection(settings)
    router = NativeSkillRouter(
        settings,
        executor=executor,
        workmap_index=NestedWorkMapIndex(root),
        workmap_selector=selector,
        path_resolver=PathResolver(threshold=0.70),
    )

    result = router.route("打开招聘数据库下面的年度报告")

    assert result.status == NativeRouteStatus.FAILED
    assert "identity changed during prepare" in result.message
    assert executor.executed == []


def test_replaced_verified_path_context_is_cleared_before_cloud_selection(
    settings,
    tmp_path,
) -> None:
    original = tmp_path / "root"
    original.mkdir()
    selector = FakeWorkMapSelector(selected_path_basename="年度报告乙")
    executor = RecordingExecutor()
    _enable_codex_workmap_selection(settings)
    router = NativeSkillRouter(
        settings,
        executor=executor,
        workmap_index=FixedWorkMapIndex(original),
        workmap_selector=selector,
        path_resolver=PathResolver(threshold=0.70),
    )
    assert router.route("打开示例数据库").status == NativeRouteStatus.SUCCEEDED
    original.rename(tmp_path / "old-root")
    original.mkdir()
    (original / "年度报告甲").mkdir()
    (original / "年度报告乙").mkdir()

    result = router.route("进入年度报告")

    assert result.status == NativeRouteStatus.MISS
    assert selector.path_calls == 0
    assert len(executor.executed) == 1


def test_semantic_child_rejects_unconsumed_conjunction_action_clause(
    settings,
    tmp_path,
) -> None:
    base = tmp_path / "deep-root"
    (base / "2026年完整年度招聘数据库分析报告甲").mkdir(parents=True)
    (base / "2026年完整年度招聘数据库分析报告乙").mkdir()
    selector = FakeWorkMapSelector(
        selected_path_basename="2026年完整年度招聘数据库分析报告乙"
    )
    resolver = PathResolver(threshold=0.45)
    executor = WindowsExecutor(settings, resolver=resolver)
    _enable_codex_workmap_selection(settings)
    router = NativeSkillRouter(
        settings,
        executor=executor,
        workmap_selector=selector,
        path_resolver=resolver,
    )

    result = router.route(f"打开 {base}\\2026年完整年度招聘数据库分析报告并打印")

    assert result.status == NativeRouteStatus.FAILED
    assert selector.path_calls == 0


def test_semantic_child_never_runs_without_consent_or_for_budget_suffix_and_clear_match(
    settings,
    tmp_path,
) -> None:
    no_consent_root = tmp_path / "no-consent"
    (no_consent_root / "年度报告甲").mkdir(parents=True)
    (no_consent_root / "年度报告乙").mkdir()
    no_consent_selector = FakeWorkMapSelector(selected_path_basename="年度报告乙")
    no_consent_router = NativeSkillRouter(
        settings,
        executor=RecordingExecutor(),
        workmap_index=FixedWorkMapIndex(no_consent_root),
        workmap_selector=no_consent_selector,
    )
    assert no_consent_router.route("打开示例数据库").status == NativeRouteStatus.SUCCEEDED
    assert no_consent_router.route("进入年度报告").status == NativeRouteStatus.MISS
    assert no_consent_selector.path_calls == 0

    _enable_codex_workmap_selection(settings)
    budget_root = tmp_path / "budget"
    for suffix in "甲乙丙":
        (budget_root / f"年度报告{suffix}").mkdir(parents=True)
    budget_selector = FakeWorkMapSelector(selected_path_basename="年度报告乙")
    budget_router = NativeSkillRouter(
        settings,
        executor=RecordingExecutor(),
        workmap_index=FixedWorkMapIndex(budget_root),
        workmap_selector=budget_selector,
        path_resolver=PathResolver(threshold=0.70, max_entries=1),
    )
    assert budget_router.route("打开示例数据库").status == NativeRouteStatus.SUCCEEDED
    assert budget_router.route("进入年度报告").status == NativeRouteStatus.MISS
    assert budget_selector.path_calls == 0

    suffix_root = tmp_path / "suffix"
    suffix_root.mkdir()
    (suffix_root / "report.xlsx").write_text("x", encoding="utf-8")
    (suffix_root / "report.pptx").write_text("x", encoding="utf-8")
    suffix_selector = FakeWorkMapSelector(selected_path_basename="report.xlsx")
    suffix_router = NativeSkillRouter(
        settings,
        executor=RecordingExecutor(),
        workmap_index=FixedWorkMapIndex(suffix_root),
        workmap_selector=suffix_selector,
        path_resolver=PathResolver(threshold=0.70),
    )
    assert suffix_router.route("打开示例数据库").status == NativeRouteStatus.SUCCEEDED
    assert suffix_router.route("打开 report.docx").status == NativeRouteStatus.MISS
    assert suffix_selector.path_calls == 0

    clear_root = tmp_path / "clear"
    clear_target = clear_root / "年度招聘资料"
    clear_target.mkdir(parents=True)
    clear_selector = FakeWorkMapSelector(selected_path_basename="年度招聘资料")
    clear_executor = RecordingExecutor()
    clear_router = NativeSkillRouter(
        settings,
        executor=clear_executor,
        workmap_index=FixedWorkMapIndex(clear_root),
        workmap_selector=clear_selector,
        path_resolver=PathResolver(threshold=0.70),
    )
    assert clear_router.route("打开示例数据库").status == NativeRouteStatus.SUCCEEDED
    clear_result = clear_router.route("进入年度招骋资料")
    assert clear_result.status == NativeRouteStatus.SUCCEEDED
    assert clear_selector.path_calls == 0
    assert clear_executor.executed[-1].actions[0].path == str(clear_target.resolve())


def test_semantic_workmap_choice_is_rebound_locally_before_opening(settings, tmp_path) -> None:
    first = tmp_path / "recruitment"
    second = tmp_path / "research"
    first.mkdir()
    second.mkdir()
    selected_id = "wm-11111111111111111111"
    index = SemanticWorkMapIndex(
        {
            selected_id: ("招聘资料库", first),
            "wm-22222222222222222222": ("研究资料库", second),
        }
    )
    selector = FakeWorkMapSelector(selected_id)
    executor = RecordingExecutor()
    _enable_codex_workmap_selection(settings)
    router = NativeSkillRouter(
        settings,
        executor=executor,
        workmap_index=index,
        workmap_selector=selector,
    )

    result = router.route("打开我平时招人的那个资料库")

    assert result.status == NativeRouteStatus.SUCCEEDED
    assert selector.calls == 1
    assert result.plan is not None and result.plan.source == "workmap_semantic"
    assert executor.executed[0].actions[0].path == str(first.resolve())


def test_assistive_semantic_workmap_candidate_identity_change_is_blocked(
    settings,
    tmp_path,
) -> None:
    first = tmp_path / "first"
    replacement = tmp_path / "replacement"
    second = tmp_path / "second"
    first.mkdir()
    replacement.mkdir()
    second.mkdir()
    selected_id = "wm-11111111111111111111"
    index = SemanticWorkMapIndex(
        {
            selected_id: ("第一资料库", first),
            "wm-22222222222222222222": ("第二资料库", second),
        }
    )
    selector = FakeWorkMapSelector(
        selected_id,
        on_select=lambda: index.candidates.__setitem__(
            selected_id,
            ("替换资料库", replacement),
        ),
    )
    settings.computer_control.engine = "assistive_v1"
    _enable_codex_workmap_selection(settings)
    executor = RecordingExecutor()
    router = NativeSkillRouter(
        settings,
        executor=executor,
        workmap_index=index,
        workmap_selector=selector,
    )

    result = router.route("打开我之前说的资料库")

    assert result.status == NativeRouteStatus.BLOCKED
    assert "identity changed" in result.message
    assert executor.executed == []


def test_assistive_semantic_workmap_candidate_disappearance_is_blocked(
    settings,
    tmp_path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    selected_id = "wm-11111111111111111111"
    index = SemanticWorkMapIndex(
        {
            selected_id: ("第一资料库", first),
            "wm-22222222222222222222": ("第二资料库", second),
        }
    )
    selector = FakeWorkMapSelector(
        selected_id,
        on_select=lambda: index.candidates.pop(selected_id),
    )
    settings.computer_control.engine = "assistive_v1"
    _enable_codex_workmap_selection(settings)
    executor = RecordingExecutor()
    router = NativeSkillRouter(
        settings,
        executor=executor,
        workmap_index=index,
        workmap_selector=selector,
    )

    result = router.route("打开我之前说的资料库")

    assert result.status == NativeRouteStatus.BLOCKED
    assert "disappeared" in result.message
    assert executor.executed == []


def test_semantic_workmap_forged_id_null_error_and_stale_choice_all_miss(
    settings,
    tmp_path,
) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    selected_id = "wm-11111111111111111111"

    for selector_factory in (
        lambda _index: FakeWorkMapSelector("wm-99999999999999999999"),
        lambda _index: FakeWorkMapSelector(None),
        lambda _index: FakeWorkMapSelector(error=RuntimeError("cli failed")),
        lambda index: FakeWorkMapSelector(
            selected_id,
            on_select=lambda: index.candidates.pop(selected_id),
        ),
    ):
        index = SemanticWorkMapIndex(
            {
                selected_id: ("第一资料库", first),
                "wm-22222222222222222222": ("第二资料库", second),
            }
        )
        selector = selector_factory(index)
        executor = RecordingExecutor()
        _enable_codex_workmap_selection(settings)
        router = NativeSkillRouter(
            settings,
            executor=executor,
            workmap_index=index,
            workmap_selector=selector,
        )

        result = router.route("打开我之前说的资料库")

        assert result.status == NativeRouteStatus.MISS
        assert executor.executed == []


def test_semantic_workmap_selector_never_runs_without_all_cloud_consents(
    settings,
    tmp_path,
) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    index = SemanticWorkMapIndex(
        {
            "wm-11111111111111111111": ("第一资料库", first),
            "wm-22222222222222222222": ("第二资料库", second),
        }
    )
    selector = FakeWorkMapSelector("wm-11111111111111111111")
    router = NativeSkillRouter(
        settings,
        executor=RecordingExecutor(),
        workmap_index=index,
        workmap_selector=selector,
    )

    assert router.can_route("打开我平时使用的资料库") is False
    result = router.route("打开我平时使用的资料库")

    assert result.status == NativeRouteStatus.MISS
    assert selector.calls == 0


def test_unique_deterministic_workmap_match_never_calls_semantic_selector(
    settings,
    tmp_path,
) -> None:
    target = tmp_path / "recruitment"
    target.mkdir()
    selector = FakeWorkMapSelector("wm-11111111111111111111")
    executor = RecordingExecutor()
    _enable_codex_workmap_selection(settings)
    router = NativeSkillRouter(
        settings,
        executor=executor,
        workmap_index=NestedWorkMapIndex(target),
        workmap_selector=selector,
    )

    result = router.route("打开招聘数剧库")

    assert result.status == NativeRouteStatus.SUCCEEDED
    assert selector.calls == 0
    assert executor.executed[0].source == "workmap_path"


def test_semantic_workmap_selector_never_replaces_a_spoken_relative_tail(
    settings,
    tmp_path,
) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    index = SemanticWorkMapIndex(
        {
            "wm-11111111111111111111": ("第一资料库", first),
            "wm-22222222222222222222": ("第二资料库", second),
        }
    )
    selector = FakeWorkMapSelector("wm-11111111111111111111")
    _enable_codex_workmap_selection(settings)
    router = NativeSkillRouter(
        settings,
        executor=RecordingExecutor(),
        workmap_index=index,
        workmap_selector=selector,
    )

    result = router.route("打开资料库下面的不存在文件夹")

    assert result.status == NativeRouteStatus.MISS
    assert selector.calls == 0


def test_semantic_workmap_selector_never_preempts_a_local_app_parse(settings, tmp_path) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    index = SemanticWorkMapIndex(
        {
            "wm-11111111111111111111": ("Claude 资料", first),
            "wm-22222222222222222222": ("其他资料", second),
        }
    )
    selector = FakeWorkMapSelector("wm-11111111111111111111")
    executor = RecordingExecutor()
    _enable_codex_workmap_selection(settings)
    router = NativeSkillRouter(
        settings,
        executor=executor,
        workmap_index=index,
        workmap_selector=selector,
    )

    result = router.route("打开 Claude")

    assert result.status == NativeRouteStatus.SUCCEEDED
    assert selector.calls == 0
    assert executor.executed[0].actions == [Action(ActionType.ACTIVATE_APP, app="claude")]


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


def test_assistive_path_identity_change_during_binding_is_blocked(
    settings,
    tmp_path,
    monkeypatch,
) -> None:
    settings.computer_control.engine = "assistive_v1"
    target = tmp_path / "bound-directory"
    target.mkdir()
    plan = Plan("open bound directory", [Action(ActionType.OPEN_PATH, path=str(target))])
    executor = RecordingExecutor()
    router = NativeSkillRouter(settings, parser=FixedParser(plan), executor=executor)

    def changed_identity(_plan: Plan) -> str:
        raise RuntimeError("path identity changed during safety binding")

    monkeypatch.setattr(native_skills, "bind_plan_paths", changed_identity)

    result = router.route("打开 bound-directory")

    assert result.status == NativeRouteStatus.BLOCKED
    assert result.message.startswith("NATIVE_PATH_BINDING_FAILED")
    assert executor.executed == []


def test_assistive_path_identity_change_during_execution_is_blocked(
    settings,
    tmp_path,
) -> None:
    class IdentityChangingExecutor(RecordingExecutor):
        def execute_plan(self, plan: Plan) -> list[ExecutionResult]:
            raise RuntimeError("selected path identity changed during execution")

    settings.computer_control.engine = "assistive_v1"
    target = tmp_path / "execution-directory"
    target.mkdir()
    plan = Plan("open execution directory", [Action(ActionType.OPEN_PATH, path=str(target))])
    executor = IdentityChangingExecutor()
    router = NativeSkillRouter(settings, parser=FixedParser(plan), executor=executor)

    result = router.route("打开 execution-directory")

    assert result.status == NativeRouteStatus.BLOCKED
    assert result.message.startswith("NATIVE_EXECUTION_FAILED")


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


def test_assistive_wrapped_secure_desktop_execution_result_is_blocked(settings) -> None:
    settings.computer_control.engine = "assistive_v1"
    executor = RecordingExecutor()
    failed_action = Action(ActionType.SET_FEEDBACK_MODE, feedback_mode=settings.app.feedback_mode)
    executor.results = [
        ExecutionResult(
            False,
            "secure desktop",
            action=failed_action,
            evidence={"error_type": "DesktopUnavailableError"},
        )
    ]
    router = NativeSkillRouter(settings, executor=executor)

    result = router.route("切换到屏幕反馈")

    assert result.status == NativeRouteStatus.BLOCKED
    assert result.message.startswith(
        "NATIVE_EXECUTION_BLOCKED: DesktopUnavailableError"
    )


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
