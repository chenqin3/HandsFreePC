from __future__ import annotations

import re
import secrets
import stat
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Protocol

from ..config import Settings
from ..intents import DeterministicIntentParser, relative_spoken_path
from ..models import Action, ActionType, ExecutionResult, Plan, RiskLevel, clone_plan
from ..path_binding import bind_plan_paths, guard_plan_paths
from ..paths import (
    AmbiguousPathError,
    PathCandidate,
    PathChildSelector,
    PathIdentityBinding,
    PathNotFoundError,
    PathResolutionError,
    PathResolver,
    PathSearchBudgetExceeded,
    PathSemanticSelectionError,
    bind_path_identity,
    verify_path_identity_binding,
)
from ..safety import SafetyPolicy
from ..windows.executor import WindowsExecutor
from ..workmap import WorkMapError, WorkMapIndex
from .workmap_selector import WorkMapSemanticSelector

_PLAIN_PATH_REQUEST_RE = re.compile(
    r"^\s*(?:请\s*)?(?:打开|进入|查看)\s*(?P<body>.+?)\s*$",
    re.IGNORECASE,
)
_PATH_CONTROL_CLAUSE_RE = re.compile(
    r"(?:然后|接着|随后|同时|并且|而且|以及|再|并|和)\s*(?:请|帮我|给我)?\s*"
    r"(?:打开|进入|查看|点击|选择|勾选|滚动|输入|填写|搜索|发送|提交|删除|关闭|"
    r"保存|打印|运行|执行|启动|切换|上传|下载|复制|移动|重命名|新建|创建|安装|"
    r"分享|回复|发布|最大化|最小化|退出|登录|付款|转账)|"
    r"\b(?:and\s+then|and|then|next)\s+"
    r"(?:open|enter|view|click|select|scroll|type|fill|search|send|submit|delete|"
    r"close|save|print|run|execute|launch|switch|upload|download|copy|move|rename|"
    r"create|install|share|reply|publish|maximize|minimize|exit|login|pay)\b",
    re.IGNORECASE,
)
_PATH_CLAUSE_PUNCTUATION_RE = re.compile(r"[，,。；;!?！？\r\n]")


class NativeRouteStatus(StrEnum):
    """Terminal outcome of one deterministic routing attempt."""

    MISS = "miss"
    BLOCKED = "blocked"
    CONFIRMATION_REQUIRED = "confirmation_required"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class NativeSkillResult:
    """A locally evidenced result; a miss is the only valid planner fallback seam."""

    status: NativeRouteStatus
    message: str
    plan: Plan | None = None
    execution_results: tuple[ExecutionResult, ...] = ()

    @property
    def matched(self) -> bool:
        return self.status != NativeRouteStatus.MISS

    @property
    def success(self) -> bool:
        return self.status == NativeRouteStatus.SUCCEEDED

    @property
    def needs_confirmation(self) -> bool:
        return self.status == NativeRouteStatus.CONFIRMATION_REQUIRED


class NativeExecutor(Protocol):
    def prepare_plan(self, plan: Plan) -> Plan: ...

    def execute_plan(self, plan: Plan) -> list[ExecutionResult]: ...


class NativeSkillRouter:
    """Run allow-listed deterministic skills before any model-based planner.

    The router intentionally has no LLM or shell execution hook. A deterministic
    parser miss is returned to the caller, which may then choose a separately
    configured desktop agent. Once a deterministic plan matches, every target is
    prepared and safety-checked locally before the first UI action.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        parser: DeterministicIntentParser | None = None,
        executor: NativeExecutor | None = None,
        safety: SafetyPolicy | None = None,
        workmap_index: WorkMapIndex | None = None,
        workmap_selector: WorkMapSemanticSelector | None = None,
        path_resolver: PathResolver | None = None,
        path_context_timeout_seconds: float = 900.0,
        monotonic: object = time.monotonic,
    ) -> None:
        self.settings = settings
        self.parser = parser or DeterministicIntentParser()
        self.executor = executor or WindowsExecutor(settings)
        self.safety = safety or SafetyPolicy(settings.execution)
        executor_resolver = getattr(self.executor, "resolver", None)
        self.path_resolver = path_resolver or (
            executor_resolver
            if isinstance(executor_resolver, PathResolver)
            else PathResolver(
                aliases=settings.execution.path_aliases,
                search_roots=settings.execution.search_roots,
                threshold=settings.execution.ambiguity_threshold,
                max_results=settings.execution.max_search_results,
            )
        )
        if path_context_timeout_seconds <= 0:
            raise ValueError("path_context_timeout_seconds must be positive")
        self._path_context_timeout_seconds = float(path_context_timeout_seconds)
        self._monotonic = monotonic
        self._path_context_lock = threading.RLock()
        self._path_context: PathIdentityBinding | None = None
        self._path_context_updated_at = 0.0
        self.workmap_selector = workmap_selector
        self.workmap_index = workmap_index
        if (
            self.workmap_index is None
            and settings.workmap.enabled
            and settings.workmap.out_directory is not None
        ):
            try:
                self.workmap_index = WorkMapIndex.load(
                    settings.workmap.out_directory,
                    aliases=settings.workmap.aliases,
                )
            except WorkMapError:
                # A generated index can be temporarily absent while WorkMap is
                # rebuilding.  Do not make microphone startup depend on it;
                # the instruction can still fall through to the desktop loop.
                self.workmap_index = None

    def _current_path_context_binding(self) -> PathIdentityBinding | None:
        with self._path_context_lock:
            binding = self._path_context
            if binding is None:
                return None
            if (
                float(self._monotonic()) - self._path_context_updated_at
                > self._path_context_timeout_seconds
            ):
                self._path_context = None
                self._path_context_updated_at = 0.0
                return None
            if not verify_path_identity_binding(binding):
                self._path_context = None
                self._path_context_updated_at = 0.0
                return None
            if not binding.path.is_dir():
                self._path_context = None
                self._path_context_updated_at = 0.0
                return None
            return binding

    def _current_path_context(self) -> Path | None:
        binding = self._current_path_context_binding()
        return binding.path if binding is not None else None

    def _remember_path_context(self, plan: Plan) -> None:
        targets = [
            Path(action.path).resolve(strict=True)
            for action in plan.actions
            if action.type == ActionType.OPEN_PATH and action.path
        ]
        if len(targets) != 1:
            return
        target = targets[0]
        directory = target if target.is_dir() else target.parent
        binding = bind_path_identity(directory)
        with self._path_context_lock:
            self._path_context = binding
            self._path_context_updated_at = float(self._monotonic())

    def _clear_path_context_for_app_plan(self, plan: Plan) -> None:
        if not any(action.app for action in plan.actions):
            return
        self.clear_path_context()

    def clear_path_context(self) -> None:
        """Forget relative-path state when another controller takes over navigation."""

        with self._path_context_lock:
            self._path_context = None
            self._path_context_updated_at = 0.0

    def _workmap_plan(self, text: str) -> Plan | None:
        if self.workmap_index is None:
            return None
        target = self.workmap_index.resolve_open_request(text)
        if target is None:
            return None
        return Plan(
            "打开 WorkMap 中唯一匹配的本地资源",
            [Action(ActionType.OPEN_PATH, path=str(target))],
            source="workmap",
        )

    def _workmap_nested_plan(
        self,
        text: str,
        *,
        ambiguous_child_selector: PathChildSelector | None = None,
        semantic_bindings: list[PathIdentityBinding] | None = None,
        source: str = "workmap_path",
    ) -> tuple[bool, Plan | None]:
        """Resolve ``WorkMap root -> bounded relative tail`` without scanning a drive."""

        if self.workmap_index is None:
            return False, None
        query = relative_spoken_path(text)
        if query is None:
            return False, None
        parts = tuple(part for part in PureWindowsPath(query).parts if part not in {"", "."})
        if not parts:
            return False, None
        resolver = getattr(self.workmap_index, "resolve_unique_name", None)
        if not callable(resolver):
            return False, None
        root = resolver(parts[0])
        if root is None:
            return False, None
        if len(parts) == 1:
            target = Path(root).resolve(strict=True)
        else:
            try:
                root_binding = bind_path_identity(Path(root))
                guarded_selector = ambiguous_child_selector
                if ambiguous_child_selector is not None:

                    def guarded_selector(
                        component: str,
                        candidates: tuple[PathCandidate, ...],
                    ) -> PathCandidate | None:
                        if not verify_path_identity_binding(root_binding):
                            raise PathSemanticSelectionError(
                                "WorkMap root changed before child selection"
                            )
                        return ambiguous_child_selector(component, candidates)

                target = self.path_resolver.resolve_relative(
                    str(PureWindowsPath(*parts[1:])),
                    current_directory=root_binding.path,
                    ambiguous_child_selector=guarded_selector,
                    semantic_bindings=semantic_bindings,
                )
                if not verify_path_identity_binding(root_binding):
                    raise PathSemanticSelectionError(
                        "WorkMap root changed during child resolution"
                    )
            except PathResolutionError:
                # A real WorkMap root matched, but the relative tail was absent
                # or ambiguous. Do not try a stale navigation context instead.
                return True, None
        return True, Plan(
            "打开 WorkMap 根目录下唯一匹配的本地资源",
            [Action(ActionType.OPEN_PATH, path=str(target))],
            source=source,
        )

    def _contextual_path_plan(
        self,
        text: str,
        *,
        ambiguous_child_selector: PathChildSelector | None = None,
        semantic_bindings: list[PathIdentityBinding] | None = None,
        source: str = "path_context",
    ) -> Plan | None:
        query = relative_spoken_path(text)
        context_binding = self._current_path_context_binding()
        if query is None or context_binding is None:
            return None
        try:
            guarded_selector = ambiguous_child_selector
            if ambiguous_child_selector is not None:

                def guarded_selector(
                    component: str,
                    candidates: tuple[PathCandidate, ...],
                ) -> PathCandidate | None:
                    if not verify_path_identity_binding(context_binding):
                        raise PathSemanticSelectionError(
                            "Path context changed before child selection"
                        )
                    return ambiguous_child_selector(component, candidates)

            target = self.path_resolver.resolve_relative(
                query,
                current_directory=context_binding.path,
                ambiguous_child_selector=guarded_selector,
                semantic_bindings=semantic_bindings,
            )
            if not verify_path_identity_binding(context_binding):
                raise PathSemanticSelectionError(
                    "Path context changed during child resolution"
                )
        except PathResolutionError:
            return None
        return Plan(
            "打开当前目录下唯一匹配的本地资源",
            [Action(ActionType.OPEN_PATH, path=str(target))],
            source=source,
        )

    def _deterministic_path_plan(self, text: str) -> Plan | None:
        exact_workmap = self._workmap_plan(text)
        if exact_workmap is not None:
            return exact_workmap
        parsed = self.parser.parse(text)
        if parsed is not None and parsed.actions:
            if parsed.actions[0].type == ActionType.ACTIVATE_APP:
                return None
            if (
                tuple(action.type for action in parsed.actions) == (ActionType.OPEN_PATH,)
                and self._covers_full_request(text, parsed)
            ):
                return parsed
        workmap_matched, nested_workmap = self._workmap_nested_plan(text)
        if workmap_matched:
            return nested_workmap
        return self._contextual_path_plan(text)

    def _semantic_selector_allowed(self) -> bool:
        """Keep transcript-cloud consent explicit even for an injected selector."""

        if self.workmap_selector is None or not self.settings.computer_control.enabled:
            return False
        if not self.settings.privacy.allow_cloud_planner:
            return False
        if not self.settings.computer_control.allow_screen_context_to_cloud:
            return False
        planner_backend = self.settings.computer_control.planner_backend
        expected_backend = {
            "codex_cli_best_effort": "codex",
            "claude": "claude",
        }.get(planner_backend)
        if expected_backend is None or self.workmap_selector.backend != expected_backend:
            return False
        return not (
            expected_backend == "codex"
            and not self.settings.computer_control.allow_codex_cli_host_read
        )

    @staticmethod
    def _plain_semantic_path_request(text: str) -> bool:
        """Use a closed one-clause gate before any path child reaches a model."""

        match = _PLAIN_PATH_REQUEST_RE.fullmatch(text)
        if match is None:
            return False
        body = match.group("body")
        if not body or re.search(r"(?:不要|别|取消)(?:打开|进入|查看)", text):
            return False
        # Clause punctuation and conjunction+verb pairs are reserved control
        # syntax, never part of a cloud-disambiguated path component. Exact
        # local paths may still be handled deterministically without this gate.
        return not (
            _PATH_CLAUSE_PUNCTUATION_RE.search(body)
            or _PATH_CONTROL_CLAUSE_RE.search(body)
        )

    @staticmethod
    def _path_bindings_stable(bindings: tuple[PathIdentityBinding, ...]) -> bool:
        return all(verify_path_identity_binding(binding) for binding in bindings)

    def _semantic_path_selector(
        self,
        text: str,
        *,
        cancel_event: threading.Event | None,
    ) -> tuple[PathChildSelector | None, dict[str, int]]:
        """Build a per-instruction child chooser with path-free random ids."""

        state = {"calls": 0}
        if not self._semantic_selector_allowed():
            return None, state
        select_child = getattr(self.workmap_selector, "select_path_child", None)
        if not callable(select_child):
            return None, state

        def choose_child(
            spoken_component: str,
            candidates: tuple[PathCandidate, ...],
        ) -> PathCandidate | None:
            if not 2 <= len(candidates) <= 5:
                return None
            bindings: dict[str, PathCandidate] = {}
            hints: list[dict[str, object]] = []
            for candidate in candidates:
                try:
                    mode = candidate.path.lstat().st_mode
                except OSError:
                    return None
                if stat.S_ISDIR(mode):
                    candidate_type = "directory"
                elif stat.S_ISREG(mode):
                    candidate_type = "file"
                else:
                    return None
                while True:
                    candidate_id = f"pc-{secrets.token_hex(10)}"
                    if candidate_id not in bindings:
                        break
                bindings[candidate_id] = candidate
                hints.append(
                    {
                        "candidate_id": candidate_id,
                        "basename": candidate.path.name,
                        "type": candidate_type,
                        "score": candidate.score,
                    }
                )
            state["calls"] += 1
            try:
                selected_id = select_child(
                    text,
                    spoken_component,
                    tuple(hints),
                    cancel_event=cancel_event,
                )
            except Exception:
                return None
            if not isinstance(selected_id, str):
                return None
            return bindings.get(selected_id)

        return choose_child, state

    @staticmethod
    def _single_open_path(plan: Plan | None) -> bool:
        return bool(
            plan is not None
            and len(plan.actions) == 1
            and plan.actions[0].type == ActionType.OPEN_PATH
            and plan.actions[0].path
        )

    def _semantic_deep_path_plan(
        self,
        text: str,
        *,
        cancel_event: threading.Event | None,
    ) -> tuple[bool, Plan | None, tuple[PathIdentityBinding, ...]]:
        """Resolve only locally enumerated ambiguous children, never a free path."""

        parsed = self.parser.parse(text)
        if (
            parsed is not None
            and parsed.actions
            and parsed.actions[0].type == ActionType.ACTIVATE_APP
        ):
            return False, None, ()
        relative_query = relative_spoken_path(text)
        path_like_request = self._single_open_path(parsed) or relative_query is not None
        if path_like_request and not self._plain_semantic_path_request(text):
            return True, None, ()
        chooser, state = self._semantic_path_selector(text, cancel_event=cancel_event)
        semantic_bindings: list[PathIdentityBinding] = []
        if self._single_open_path(parsed):
            if not self._covers_full_request(text, parsed):
                return True, None, ()
            if chooser is None:
                return True, None, ()
            assert parsed is not None
            raw_path = parsed.actions[0].path
            assert raw_path is not None
            try:
                target = self.path_resolver.resolve(
                    raw_path,
                    ambiguous_child_selector=chooser,
                    semantic_bindings=semantic_bindings,
                )
            except PathResolutionError:
                return True, None, ()
            return (
                True,
                Plan(
                    "打开逐层本地验证的路径",
                    [Action(ActionType.OPEN_PATH, path=str(target))],
                    risk=parsed.risk,
                    source="path_semantic" if state["calls"] else parsed.source,
                ),
                tuple(semantic_bindings),
            )

        workmap_matched, workmap_plan = self._workmap_nested_plan(
            text,
            ambiguous_child_selector=chooser,
            semantic_bindings=semantic_bindings,
            source="workmap_path_semantic",
        )
        if workmap_matched:
            return True, workmap_plan, tuple(semantic_bindings) if workmap_plan else ()
        contextual_query = relative_query
        context_binding = self._current_path_context_binding()
        if contextual_query is None or context_binding is None:
            return False, None, ()
        guarded_chooser = chooser
        if chooser is not None:

            def guarded_chooser(
                component: str,
                candidates: tuple[PathCandidate, ...],
            ) -> PathCandidate | None:
                if not verify_path_identity_binding(context_binding):
                    raise PathSemanticSelectionError(
                        "Path context changed before semantic child selection"
                    )
                return chooser(component, candidates)

        try:
            target = self.path_resolver.resolve_relative(
                contextual_query,
                current_directory=context_binding.path,
                ambiguous_child_selector=guarded_chooser,
                semantic_bindings=semantic_bindings,
            )
            if not verify_path_identity_binding(context_binding):
                raise PathSemanticSelectionError(
                    "Path context changed during semantic child resolution"
                )
        except PathNotFoundError:
            # A filename with an explicit extension is a concrete local
            # identity, so a suffix mismatch must remain a local miss. A
            # pathless natural-language name may still be a WorkMap root.
            if PureWindowsPath(contextual_query).suffix:
                return True, None, ()
            return False, None, ()
        except (
            AmbiguousPathError,
            PathSearchBudgetExceeded,
            PathSemanticSelectionError,
        ):
            return True, None, ()
        except PathResolutionError:
            return True, None, ()
        return (
            True,
            Plan(
                "打开当前目录下唯一匹配的本地资源",
                [Action(ActionType.OPEN_PATH, path=str(target))],
                source="path_context_semantic" if state["calls"] else "path_context",
            ),
            tuple(semantic_bindings),
        )

    @staticmethod
    def _semantic_workmap_query(text: str) -> str | None:
        """Accept only one plain affirmative WorkMap name, never a spoken path."""

        query = WorkMapIndex._open_request_target(text)
        if query is None:
            return None
        relative_query = relative_spoken_path(text)
        if relative_query is None:
            return None
        parts = tuple(
            part for part in PureWindowsPath(relative_query).parts if part not in {"", "."}
        )
        if len(parts) != 1 or ":" in query or "/" in query or "\\" in query:
            return None
        return query

    def _semantic_workmap_plan(
        self,
        text: str,
        *,
        cancel_event: threading.Event | None,
    ) -> Plan | None:
        """Let a consented model choose only among locally rebound opaque ids."""

        if self.workmap_index is None or not self._semantic_selector_allowed():
            return None
        # An application/runtime command already understood locally must not
        # be detoured through WorkMap just because it begins with "open".
        if self.parser.parse(text) is not None:
            return None
        query = self._semantic_workmap_query(text)
        if query is None:
            return None
        hint_provider = getattr(self.workmap_index, "planner_hints", None)
        binder = getattr(self.workmap_index, "resolve_candidate_id", None)
        if not callable(hint_provider) or not callable(binder):
            return None
        try:
            # Semantic disambiguation needs a bounded shortlist even when the
            # spoken wording is not lexically close to a project title. The
            # local WorkMap ranker still chooses the only five hints the model
            # may see, and the model may return null.
            raw_hints = hint_provider(
                query,
                limit=5,
                minimum_score=0.0,
                available_only=True,
            )
        except (TypeError, ValueError, WorkMapError):
            return None
        if not isinstance(raw_hints, tuple | list):
            return None
        usable: list[Mapping[str, object]] = []
        seen_ids: set[str] = set()
        for hint in raw_hints:
            if not isinstance(hint, Mapping) or hint.get("target_available") is not True:
                continue
            candidate_id = hint.get("target_id")
            if not isinstance(candidate_id, str) or candidate_id in seen_ids:
                continue
            try:
                available_target = binder(candidate_id)
            except (OSError, RuntimeError, ValueError, WorkMapError):
                continue
            if available_target is None:
                continue
            seen_ids.add(candidate_id)
            usable.append(hint)
        if not 2 <= len(usable) <= 5:
            return None
        try:
            selected_id = self.workmap_selector.select(
                text,
                tuple(usable),
                cancel_event=cancel_event,
            )
        except Exception:
            # A cloud/CLI/protocol failure is always a planner miss. It must
            # never turn into a guessed path or a partially prepared action.
            return None
        if not isinstance(selected_id, str) or selected_id not in seen_ids:
            return None
        try:
            target = binder(selected_id)
        except (OSError, RuntimeError, ValueError, WorkMapError):
            return None
        if target is None:
            return None
        try:
            rebound = Path(target).resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        return Plan(
            "打开由 WorkMap 候选语义选择并重新绑定的本地资源",
            [Action(ActionType.OPEN_PATH, path=str(rebound))],
            source="workmap_semantic",
        )

    def can_route(self, text: str) -> bool:
        """Report a deterministic match without preparing or executing it."""

        if self._deterministic_path_plan(text) is not None:
            return True
        plan = self.parser.parse(text)
        return plan is not None and self._covers_full_request(text, plan)

    @staticmethod
    def execution_is_locally_verified(
        plan: Plan,
        results: tuple[ExecutionResult, ...],
    ) -> bool:
        """Require an explicit local postcondition for every dispatched action."""

        if len(results) != len(plan.actions):
            return False
        for action, result in zip(plan.actions, results, strict=True):
            if not result.success or result.action != action:
                return False
            evidence = result.evidence or {}
            if evidence.get("dry_run") is True:
                return False
            if not (
                evidence.get("postcondition_verified") is True or evidence.get("verified") is True
            ):
                return False
        return True

    def _covers_full_request(self, text: str, plan: Plan) -> bool:
        """Require exact parser coverage and a strongly verifiable action set."""

        action_types = {action.type for action in plan.actions}
        if action_types & {ActionType.START_NATIVE_VOICE, ActionType.SEND_PROMPT}:
            # These legacy native actions currently lack a strong, target-bound
            # local postcondition.  Let the generic desktop loop observe, act,
            # and verify them instead.  Runtime-internal controls are separate.
            return False
        for action in plan.actions:
            if action.type != ActionType.OPEN_MODE:
                continue
            profile = self.settings.apps.get((action.app or "").casefold())
            if profile is None:
                return False
            mapped_modes = {name.casefold() for name in profile.mode_names}
            if (action.mode or "").casefold() not in mapped_modes:
                return False
            if action.tab and action.tab.casefold() not in mapped_modes:
                return False
        coverage_check = getattr(self.parser, "covers_full_text", None)
        if not callable(coverage_check):
            return False
        try:
            return bool(coverage_check(text, plan))
        except (TypeError, ValueError):
            return False

    def route(
        self,
        text: str,
        *,
        explicit_submission: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> NativeSkillResult:
        plan = self._deterministic_path_plan(text)
        semantic_path_matched = False
        semantic_path_bindings: tuple[PathIdentityBinding, ...] = ()
        if plan is None or (plan.source == "deterministic" and self._single_open_path(plan)):
            (
                semantic_path_matched,
                semantic_path,
                semantic_path_bindings,
            ) = self._semantic_deep_path_plan(
                text,
                cancel_event=cancel_event,
            )
            if semantic_path is not None:
                plan = semantic_path
        if plan is None and not semantic_path_matched:
            plan = self._semantic_workmap_plan(text, cancel_event=cancel_event)
        if plan is None:
            plan = self.parser.parse(text)
            covered = plan is not None and self._covers_full_request(text, plan)
        else:
            covered = True
        if plan is None or not covered:
            return NativeSkillResult(
                status=NativeRouteStatus.MISS,
                message="NATIVE_ROUTE_MISS: 未命中确定性本地操作",
            )

        if semantic_path_bindings and not self._path_bindings_stable(
            semantic_path_bindings
        ):
            failed = replace(plan, risk=RiskLevel.BLOCKED, summary="语义路径身份已变化")
            return NativeSkillResult(
                status=NativeRouteStatus.FAILED,
                message="NATIVE_PREPARE_FAILED: selected path identity changed",
                plan=failed,
            )
        try:
            prepared = self.executor.prepare_plan(plan)
        except Exception as exc:
            failed = replace(plan, risk=RiskLevel.BLOCKED, summary="本地目标解析失败")
            return NativeSkillResult(
                status=NativeRouteStatus.FAILED,
                message=f"NATIVE_PREPARE_FAILED: {type(exc).__name__}: {exc}",
                plan=failed,
            )
        if semantic_path_bindings and not self._path_bindings_stable(
            semantic_path_bindings
        ):
            failed = replace(prepared, risk=RiskLevel.BLOCKED, summary="语义路径身份已变化")
            return NativeSkillResult(
                status=NativeRouteStatus.FAILED,
                message="NATIVE_PREPARE_FAILED: selected path identity changed during prepare",
                plan=failed,
            )

        evaluated = self.safety.evaluate(
            prepared,
            user_text=text,
            explicit_submission=explicit_submission,
        )
        safe_path_binding: str | None = None
        has_open_path = any(action.type == ActionType.OPEN_PATH for action in evaluated.actions)
        if evaluated.risk == RiskLevel.SAFE and has_open_path:
            # A deterministic route must not be a shortcut around the runtime's
            # path-identity boundary. Re-evaluate the exact target between two
            # bindings, then hold it stable through the executor call.
            try:
                if semantic_path_bindings and not self._path_bindings_stable(
                    semantic_path_bindings
                ):
                    raise RuntimeError("selected path identity changed before safety binding")
                before_classification = bind_plan_paths(evaluated)
                if semantic_path_bindings and not self._path_bindings_stable(
                    semantic_path_bindings
                ):
                    raise RuntimeError("selected path identity changed during safety binding")
                evaluated = self.safety.evaluate(
                    evaluated,
                    user_text=text,
                    explicit_submission=explicit_submission,
                )
                evaluated = clone_plan(evaluated)
                if evaluated.risk == RiskLevel.SAFE:
                    after_classification = bind_plan_paths(evaluated)
                    if semantic_path_bindings and not self._path_bindings_stable(
                        semantic_path_bindings
                    ):
                        raise RuntimeError(
                            "selected path identity changed during safety classification"
                        )
                    if after_classification != before_classification:
                        raise RuntimeError("path identity changed during safety classification")
                    safe_path_binding = after_classification
            except Exception as exc:
                return NativeSkillResult(
                    status=NativeRouteStatus.FAILED,
                    message=f"NATIVE_PATH_BINDING_FAILED: {type(exc).__name__}: {exc}",
                    plan=evaluated,
                )
        if semantic_path_bindings and not self._path_bindings_stable(
            semantic_path_bindings
        ):
            return NativeSkillResult(
                status=NativeRouteStatus.FAILED,
                message="NATIVE_PATH_BINDING_FAILED: selected path identity changed",
                plan=evaluated,
            )
        if evaluated.risk == RiskLevel.BLOCKED:
            return NativeSkillResult(
                status=NativeRouteStatus.BLOCKED,
                message=f"NATIVE_ROUTE_BLOCKED: {evaluated.summary}",
                plan=evaluated,
            )
        if evaluated.risk == RiskLevel.CONFIRM:
            if semantic_path_bindings:
                # Native confirmation state carries the final path digest but
                # not every intermediate semantic child identity. Until that
                # full binding can be retained, never confirm a different
                # same-named object later.
                return NativeSkillResult(
                    status=NativeRouteStatus.FAILED,
                    message=(
                        "NATIVE_PATH_BINDING_FAILED: semantic path confirmation "
                        "cannot retain intermediate identities"
                    ),
                    plan=evaluated,
                )
            return NativeSkillResult(
                status=NativeRouteStatus.CONFIRMATION_REQUIRED,
                message=f"NATIVE_CONFIRMATION_REQUIRED: {evaluated.summary}",
                plan=evaluated,
            )

        evaluated = clone_plan(evaluated)
        try:
            if has_open_path:
                if safe_path_binding is None:
                    raise RuntimeError("safe path execution is missing its identity binding")
                with guard_plan_paths(evaluated, safe_path_binding):
                    if semantic_path_bindings and not self._path_bindings_stable(
                        semantic_path_bindings
                    ):
                        raise RuntimeError("selected path identity changed before execution")
                    results = tuple(self.executor.execute_plan(evaluated))
                    if semantic_path_bindings and not self._path_bindings_stable(
                        semantic_path_bindings
                    ):
                        raise RuntimeError("selected path identity changed during execution")
            else:
                results = tuple(self.executor.execute_plan(evaluated))
        except Exception as exc:
            return NativeSkillResult(
                status=NativeRouteStatus.FAILED,
                message=f"NATIVE_EXECUTION_FAILED: {type(exc).__name__}: {exc}",
                plan=evaluated,
            )

        completed = self.execution_is_locally_verified(evaluated, results)
        if not completed:
            failure = next((result.message for result in results if not result.success), None)
            message = failure or "动作已请求，但缺少逐动作的本地后置条件证据"
            return NativeSkillResult(
                status=NativeRouteStatus.FAILED,
                message=f"NATIVE_EXECUTION_FAILED: {message}",
                plan=evaluated,
                execution_results=results,
            )
        if has_open_path:
            try:
                self._remember_path_context(evaluated)
            except (OSError, RuntimeError, ValueError):
                # The action remains verified, but an unstable target must not
                # seed a later relative command.
                with self._path_context_lock:
                    self._path_context = None
                    self._path_context_updated_at = 0.0
        else:
            self._clear_path_context_for_app_plan(evaluated)
        return NativeSkillResult(
            status=NativeRouteStatus.SUCCEEDED,
            message=f"NATIVE_EXECUTION_SUCCEEDED: {evaluated.summary}",
            plan=evaluated,
            execution_results=results,
        )
