from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..config import AppProfile, Settings
from ..models import Action, ActionType, ExecutionResult, Plan
from ..paths import PathResolver
from .native import (
    AmbiguousWindowError,
    NativeWindows,
    WindowInfo,
    WindowNotFoundError,
    parse_hotkey,
)
from .uia import ElementNotFoundError, UIABackend, UIAError


class WindowsExecutionError(RuntimeError):
    pass


@dataclass(slots=True)
class _DictationTarget:
    app: str
    hwnd: int | None
    dry_run: bool
    entry_identity: tuple[str, str] | None = None


class WindowsExecutor:
    """Execute the finite HandsFreePC action vocabulary against verified windows."""

    def __init__(
        self,
        settings: Settings,
        *,
        resolver: PathResolver | None = None,
        native: NativeWindows | None = None,
        uia: UIABackend | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.resolver = resolver or PathResolver(
            aliases=settings.execution.path_aliases,
            search_roots=settings.execution.search_roots,
            threshold=settings.execution.ambiguity_threshold,
            max_results=settings.execution.max_search_results,
        )
        self._native = native
        self._uia = uia
        self._sleep = sleeper
        self._dictation_target: _DictationTarget | None = None

    @property
    def dry_run(self) -> bool:
        return self.settings.execution.dry_run

    def _native_backend(self) -> NativeWindows:
        if self._native is None:
            self._native = NativeWindows()
        return self._native

    def _uia_backend(self) -> UIABackend:
        if self._uia is None:
            self._uia = UIABackend(
                threshold=self.settings.execution.ambiguity_threshold,
                foreground_guard=self._native_backend().assert_foreground,
            )
        return self._uia

    def execute(self, action: Action) -> ExecutionResult:
        try:
            action.validate()
            if not self.dry_run and action.type not in {
                ActionType.SET_FEEDBACK_MODE,
                ActionType.PAUSE,
                ActionType.RESUME,
                ActionType.WAIT,
            }:
                self._native_backend().assert_interactive_desktop()
            if action.type not in {
                ActionType.ENTER_DICTATION,
                ActionType.TYPE_TEXT,
                ActionType.SEND_PROMPT,
            }:
                self._dictation_target = None
            handler = {
                ActionType.OPEN_PATH: self._open_path,
                ActionType.ACTIVATE_APP: self._activate_app_action,
                ActionType.OPEN_CONVERSATION: self._open_conversation,
                ActionType.OPEN_MODE: self._open_mode,
                ActionType.ENTER_DICTATION: self._enter_dictation,
                ActionType.START_NATIVE_VOICE: self._start_native_voice,
                ActionType.TYPE_TEXT: self._type_text,
                ActionType.SEND_PROMPT: self._send_prompt,
                ActionType.WAIT: self._wait,
                ActionType.SET_FEEDBACK_MODE: self._runtime_action,
                ActionType.PAUSE: self._runtime_action,
                ActionType.RESUME: self._runtime_action,
            }[action.type]
            return handler(action)
        except Exception as exc:
            if action.type in {ActionType.TYPE_TEXT, ActionType.SEND_PROMPT}:
                self._dictation_target = None
            failure_evidence = {
                "error_type": type(exc).__name__,
                "dry_run": self.dry_run,
            }
            extra_evidence = getattr(exc, "evidence", None)
            if isinstance(extra_evidence, dict):
                failure_evidence.update(extra_evidence)
            return ExecutionResult(
                success=False,
                message=str(exc) or type(exc).__name__,
                action=action,
                evidence=failure_evidence,
            )

    def execute_plan(self, plan: Plan) -> list[ExecutionResult]:
        results: list[ExecutionResult] = []
        for action in plan.actions:
            result = self.execute(action)
            results.append(result)
            if not result.success:
                break
        return results

    def prepare_plan(self, plan: Plan) -> Plan:
        """Resolve read-only targets before the runtime performs its final risk check.

        A fuzzy query such as ``安装程序`` can resolve to ``installer.exe``.  The
        final path must therefore be visible to the safety policy before any
        confirmation decision is made.
        """

        prepared: list[Action] = []
        for action in plan.actions:
            if action.type == ActionType.OPEN_PATH and action.path:
                prepared.append(replace(action, path=str(self.resolver.resolve(action.path))))
            else:
                prepared.append(action)
        return replace(plan, actions=prepared)

    @staticmethod
    def _success(action: Action, message: str, evidence: dict[str, Any]) -> ExecutionResult:
        return ExecutionResult(success=True, message=message, action=action, evidence=evidence)

    def _profile(self, app: str | None) -> AppProfile:
        normalized = (app or "").strip().casefold()
        if normalized == "current":
            raise WindowsExecutionError("The current window does not have an application profile")
        try:
            return self.settings.apps[normalized]
        except KeyError as exc:
            raise WindowsExecutionError(f"Application is not configured: {app!r}") from exc

    def _open_path(self, action: Action) -> ExecutionResult:
        assert action.path is not None
        target = self.resolver.resolve(action.path)
        evidence: dict[str, Any] = {
            "dry_run": self.dry_run,
            "resolved_path": str(target),
            "exists": target.exists(),
            "is_directory": target.is_dir(),
        }
        if self.dry_run:
            evidence["would_open_via"] = "Windows shell path API"
            return self._success(action, f"Dry run: would open {target}", evidence)
        native = self._native_backend()
        before = native.path_open_state(target)
        if before.get("verified") is True:
            raise WindowsExecutionError(
                "The requested path is already open in the foreground; no new action can be proven"
            )
        evidence["opened_via"] = native.open_path(target)
        evidence.update(native.wait_for_path_open(target, before=before))
        return self._success(action, f"Opened {target}", evidence)

    def _dry_activation_evidence(self, app: str, profile: AppProfile) -> dict[str, Any]:
        return {
            "dry_run": True,
            "app": app,
            "title_patterns": list(profile.title_patterns),
            "process_names": list(profile.process_names),
            "would_try_activation_hotkey_if_no_visible_window": bool(
                profile.activation_hotkey
            ),
            "would_launch": str(profile.executable) if profile.executable else None,
            "foreground_verified": False,
        }

    def _activate(self, app: str | None) -> tuple[int | None, dict[str, Any]]:
        normalized = (app or "").strip().casefold()
        if normalized == "current":
            raise WindowsExecutionError("Dictation requires an explicitly configured application")

        profile = self._profile(normalized)
        if self.dry_run:
            if profile.activation_hotkey:
                parse_hotkey(profile.activation_hotkey)
            return None, self._dry_activation_evidence(normalized, profile)

        native = self._native_backend()
        windows = native.find_windows(
            title_patterns=profile.title_patterns,
            process_names=profile.process_names,
        )
        activation_hotkey_attempted = False
        activation_hotkey_restored = False
        if not windows and profile.activation_hotkey:
            # Validate the entire allow-listed shortcut before emitting any OS
            # input. The shortcut is global because there is no visible target
            # window to bind input to yet.
            parse_hotkey(profile.activation_hotkey)
            native.send_hotkey(profile.activation_hotkey)
            activation_hotkey_attempted = True
            windows = native.wait_for_windows(
                title_patterns=profile.title_patterns,
                process_names=profile.process_names,
            )
            activation_hotkey_restored = bool(windows)
        launched = False
        if not windows and profile.executable is not None:
            executable = Path(profile.executable)
            if not executable.exists():
                raise WindowNotFoundError(f"Configured executable does not exist: {executable}")
            native.open_path(executable)
            launched = True
            windows = native.wait_for_windows(
                title_patterns=profile.title_patterns,
                process_names=profile.process_names,
            )
        if not windows:
            raise WindowNotFoundError(f"No configured window found for {normalized}")

        foreground = native.get_foreground_window_info()
        foreground_match = next(
            (item for item in windows if foreground is not None and item.hwnd == foreground.hwnd),
            None,
        )
        if len(windows) > 1 and foreground_match is None:
            raise AmbiguousWindowError(windows)
        target = foreground_match or windows[0]
        activated = native.activate_window(target.hwnd)
        native.assert_foreground(target.hwnd)
        if isinstance(activated, WindowInfo):
            target = activated
        evidence = target.to_evidence()
        evidence.update(
            {
                "app": normalized,
                "launched": launched,
                "activation_hotkey_attempted": activation_hotkey_attempted,
                "activation_hotkey_restored": activation_hotkey_restored,
                "foreground_verified": True,
                "candidate_count": len(windows),
            }
        )
        return target.hwnd, evidence

    def _activate_app_action(self, action: Action) -> ExecutionResult:
        hwnd, evidence = self._activate(action.app)
        evidence["hwnd"] = hwnd
        evidence["postcondition_verified"] = bool(
            not self.dry_run and evidence.get("foreground_verified") is True
        )
        return self._success(action, f"Activated {action.app}", evidence)

    def _verified_hotkey(self, hwnd: int, hotkey: str) -> tuple[int, ...]:
        native = self._native_backend()
        native.assert_foreground(hwnd)
        keys = native.send_hotkey(hotkey)
        native.assert_foreground(hwnd)
        return keys

    def _verified_text(self, hwnd: int, text: str) -> int:
        native = self._native_backend()
        native.assert_foreground(hwnd)
        units = native.send_text(text)
        native.assert_foreground(hwnd)
        return units

    def _open_conversation(self, action: Action) -> ExecutionResult:
        assert action.app is not None and action.conversation is not None
        profile = self._profile(action.app)
        configured_search_keys = (
            parse_hotkey(profile.search_hotkey) if profile.search_hotkey else None
        )
        hwnd, activation = self._activate(action.app)
        query = " / ".join(item for item in (action.project, action.conversation) if item)
        evidence: dict[str, Any] = {
            "dry_run": self.dry_run,
            "activation": activation,
            "project": action.project,
            "conversation": action.conversation,
        }
        if self.dry_run:
            evidence["method"] = "configured_search_hotkey" if profile.search_hotkey else "uia"
            if configured_search_keys is not None:
                evidence["hotkey_key_count"] = len(configured_search_keys)
            return self._success(action, f"Dry run: would open conversation {query}", evidence)
        assert hwnd is not None
        if profile.search_hotkey:
            keys = self._verified_hotkey(hwnd, profile.search_hotkey)
            units = self._verified_text(hwnd, query)
            self._verified_hotkey(hwnd, "enter")
            selected = self._uia_backend().verify_named_selected(
                hwnd,
                action.conversation,
                control_types=("ListItem", "TreeItem", "TabItem", "Button"),
            )
            self._native_backend().assert_foreground(hwnd)
            evidence.update(
                {
                    "method": "configured_search_hotkey",
                    "hotkey_key_count": len(keys),
                    "query_utf16_units": units,
                    "selected_conversation": selected,
                    "foreground_verified": True,
                }
            )
        else:
            clicks: list[dict[str, object]] = []
            if action.project:
                clicks.append(
                    self._uia_backend().click_named_exact(
                        hwnd,
                        action.project,
                        control_types=("Button", "ListItem", "TreeItem", "TabItem"),
                    )
                )
                self._native_backend().assert_foreground(hwnd)
            clicks.append(
                self._uia_backend().click_named_exact(
                    hwnd,
                    action.conversation,
                    control_types=("Button", "ListItem", "TreeItem", "TabItem"),
                )
            )
            self._native_backend().assert_foreground(hwnd)
            selected = self._uia_backend().verify_named_selected(
                hwnd,
                action.conversation,
                control_types=("ListItem", "TreeItem", "TabItem", "Button"),
            )
            evidence.update(
                {
                    "method": "uia",
                    "matches": clicks,
                    "selected_conversation": selected,
                    "foreground_verified": True,
                }
            )
        evidence["postcondition_verified"] = bool(
            evidence.get("foreground_verified") is True
            and isinstance(evidence.get("selected_conversation"), dict)
            and evidence["selected_conversation"].get("selection_verified") is True
            and evidence["selected_conversation"].get("exact") is True
        )
        if not evidence["postcondition_verified"]:
            raise WindowsExecutionError("The exact conversation did not become selected")
        return self._success(action, f"Opened conversation {query}", evidence)

    def _open_mode(self, action: Action) -> ExecutionResult:
        assert action.app is not None and action.mode is not None
        profile = self._profile(action.app)
        requested_names = [action.mode, *([action.tab] if action.tab else [])]
        for canonical_name in requested_names:
            assert canonical_name is not None
            if canonical_name.casefold() not in profile.mode_names:
                raise WindowsExecutionError(
                    f"Mode {canonical_name!r} is not in the configured exact-label allowlist"
                )
        hwnd, activation = self._activate(action.app)
        evidence: dict[str, Any] = {
            "dry_run": self.dry_run,
            "activation": activation,
            "tab": action.tab,
            "mode": action.mode,
        }
        if self.dry_run:
            evidence["method"] = "uia"
            return self._success(action, f"Dry run: would open mode {action.mode}", evidence)
        assert hwnd is not None

        def retry_uia(operation: Callable[[], dict[str, object]]) -> dict[str, object]:
            # Exact mode selection is idempotent, so this retry stays local to OPEN_MODE.
            for attempt in range(3):
                try:
                    return operation()
                except ElementNotFoundError:
                    # A missing exact label must continue through the configured
                    # fallbacks instead of retrying the same label.
                    raise
                except UIAError:
                    if attempt == 2:
                        raise
                    self._sleep((0.15, 0.35)[attempt])
                    self._native_backend().assert_foreground(hwnd)
            raise AssertionError("unreachable")

        def click_mode(canonical_name: str) -> tuple[str, dict[str, object]]:
            labels = profile.mode_names[canonical_name.casefold()]
            last_not_found: ElementNotFoundError | None = None
            for label in labels:
                try:
                    match = retry_uia(
                        lambda label=label: self._uia_backend().click_named_exact(
                            hwnd,
                            label,
                            control_types=("TabItem", "Button", "ListItem", "TreeItem"),
                        )
                    )
                except ElementNotFoundError as exc:
                    last_not_found = exc
                    continue
                return label, match
            raise WindowsExecutionError(
                f"No exact configured UI label matched mode {canonical_name!r}"
            ) from last_not_found

        matches: list[dict[str, object]] = []
        if action.tab:
            _tab_label, tab_match = click_mode(action.tab)
            matches.append(tab_match)
            self._native_backend().assert_foreground(hwnd)
        mode_label, mode_match = click_mode(action.mode)
        matches.append(mode_match)
        self._native_backend().assert_foreground(hwnd)
        try:
            selected = retry_uia(
                lambda: self._uia_backend().verify_named_selected(
                    hwnd,
                    mode_label,
                    control_types=("Button", "ListItem", "TreeItem", "TabItem"),
                )
            )
        except UIAError:
            selected = None
        mode_verified = bool(
            isinstance(selected, dict)
            and selected.get("selection_verified") is True
            and selected.get("exact") is True
        ) or bool(
            mode_match.get("exact") is True
            and mode_match.get("postcondition_verified") is True
            and mode_match.get("postcondition") in {"selected", "already_selected"}
        )
        evidence.update(
            {
                "method": "uia",
                "matches": matches,
                "selected_mode": selected,
                "resolved_mode_label": mode_label,
                "foreground_verified": True,
                "postcondition_verified": mode_verified,
            }
        )
        if not mode_verified:
            raise WindowsExecutionError("The exact configured mode did not become selected")
        return self._success(action, f"Opened mode {action.mode}", evidence)

    def _enter_dictation(self, action: Action) -> ExecutionResult:
        assert action.app is not None
        hwnd, activation = self._activate(action.app)
        evidence: dict[str, Any] = {
            "dry_run": self.dry_run,
            "activation": activation,
            "dictation_backend": "handsfreepc_stt",
            "uses_application_native_voice": False,
        }
        text_entry: dict[str, object] | None = None
        if not self.dry_run:
            assert hwnd is not None
            text_entry = self._uia_backend().focus_text_entry(hwnd)
            evidence["text_entry"] = text_entry
            self._native_backend().assert_foreground(hwnd)
            evidence["foreground_verified"] = True
            evidence["postcondition_verified"] = bool(text_entry.get("focus_verified") is True)
        self._dictation_target = _DictationTarget(
            app=action.app.casefold(),
            hwnd=hwnd,
            dry_run=self.dry_run,
            entry_identity=self._entry_identity(text_entry) if text_entry else None,
        )
        return self._success(action, f"HandsFreePC dictation ready for {action.app}", evidence)

    def _start_native_voice(self, action: Action) -> ExecutionResult:
        assert action.app is not None
        profile = self._profile(action.app)
        voice_button_names = [item for item in profile.voice_button_names if item.strip()]
        configured_voice_keys = (
            parse_hotkey(profile.native_voice_hotkey) if profile.native_voice_hotkey else None
        )
        if configured_voice_keys is None and not voice_button_names:
            raise WindowsExecutionError(
                f"No configured native voice hotkey or named voice button for {action.app}"
            )
        if self.dry_run:
            _hwnd, activation = self._activate(action.app)
            evidence: dict[str, Any] = {"dry_run": True, "activation": activation}
            evidence["method"] = (
                "configured_hotkey" if configured_voice_keys is not None else "named_uia_button"
            )
            if configured_voice_keys is not None:
                evidence["hotkey_key_count"] = len(configured_voice_keys)
            return self._success(
                action,
                f"Dry run: would start native voice in {action.app}",
                evidence,
            )
        raise WindowsExecutionError(
            "Native voice activation is disabled because no application-specific active-state "
            "postcondition is configured"
        )

    def _require_dictation_target(self) -> _DictationTarget:
        if self._dictation_target is None:
            raise WindowsExecutionError("Text entry requires a preceding enter_dictation action")
        return self._dictation_target

    @staticmethod
    def _entry_identity(evidence: dict[str, object] | None) -> tuple[str, str] | None:
        if not evidence:
            return None
        runtime_id = evidence.get("runtime_id")
        if runtime_id is not None:
            return "runtime_id", repr(runtime_id)
        automation_id = evidence.get("automation_id")
        if automation_id:
            return "automation_id", str(automation_id)
        name = evidence.get("name")
        control_type = evidence.get("control_type")
        if name and control_type:
            return "semantic", f"{control_type}\0{name}"
        return None

    def _verify_dictation_entry(self, target: _DictationTarget) -> dict[str, object]:
        assert target.hwnd is not None
        evidence = self._uia_backend().verify_focused_text_entry(target.hwnd)
        current_identity = self._entry_identity(evidence)
        if target.entry_identity is None or current_identity != target.entry_identity:
            raise WindowsExecutionError(
                "Refusing input because keyboard focus moved to a different text entry"
            )
        return evidence

    def _type_text(self, action: Action) -> ExecutionResult:
        assert action.text is not None
        target = self._require_dictation_target()
        evidence: dict[str, Any] = {
            "dry_run": self.dry_run,
            "app": target.app,
            "character_count": len(action.text),
            "uses_clipboard": False,
        }
        if self.dry_run:
            evidence["would_use"] = "SendInput Unicode"
            return self._success(action, "Dry run: would type dictated text", evidence)
        if target.hwnd is None:
            raise WindowsExecutionError("Dictation target has no verified window")
        evidence["text_entry"] = self._verify_dictation_entry(target)
        evidence["utf16_units"] = self._verified_text(target.hwnd, action.text)
        text_result = self._uia_backend().verify_focused_text_contains(
            target.hwnd,
            action.text,
        )
        if self._entry_identity(text_result) != target.entry_identity:
            raise WindowsExecutionError(
                "Text appeared in a different UI element than the verified dictation target"
            )
        evidence["text_postcondition"] = text_result
        evidence["foreground_verified"] = True
        evidence["postcondition_verified"] = bool(text_result.get("input_text_verified") is True)
        return self._success(action, "Typed dictated text", evidence)

    def _send_prompt(self, action: Action) -> ExecutionResult:
        target = self._require_dictation_target()
        evidence: dict[str, Any] = {"dry_run": self.dry_run, "app": target.app}
        if self.dry_run:
            evidence["would_send"] = "enter"
            return self._success(action, "Dry run: would submit prompt", evidence)
        raise WindowsExecutionError(
            "Prompt submission is disabled because no application-specific sent-message "
            "postcondition is configured"
        )

    def _wait(self, action: Action) -> ExecutionResult:
        assert action.seconds is not None
        if not self.dry_run:
            self._sleep(action.seconds)
        return self._success(
            action,
            f"{'Dry run: would wait' if self.dry_run else 'Waited'} {action.seconds:g} seconds",
            {
                "dry_run": self.dry_run,
                "seconds": action.seconds,
                "postcondition_verified": not self.dry_run,
            },
        )

    def _runtime_action(self, action: Action) -> ExecutionResult:
        return self._success(
            action,
            f"Runtime action acknowledged: {action.type.value}",
            {"dry_run": self.dry_run, "handled_by": "runtime"},
        )
