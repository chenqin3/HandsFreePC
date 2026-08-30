from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .config import PlannerSettings
from .models import Plan


class PlannerError(RuntimeError):
    pass


class PlannerUnavailable(PlannerError):
    pass


class Planner(ABC):
    @abstractmethod
    def plan(self, command: str, *, context: dict[str, Any] | None = None) -> Plan:
        raise NotImplementedError


def schema_path() -> Path:
    return Path(__file__).with_name("schemas") / "plan.schema.json"


def _schema_text() -> str:
    schema = json.loads(schema_path().read_text(encoding="utf-8"))
    # Claude Code's current structured-output validator rejects the otherwise
    # standard draft declaration.  The constraints themselves are shared with
    # Codex; only this validator hint is removed for the CLI argument.
    schema.pop("$schema", None)
    return json.dumps(schema, ensure_ascii=False, separators=(",", ":"))


def _sanitized_env() -> dict[str, str]:
    """Preserve normal runtime variables but do not leak API keys to planner children."""
    blocked_markers = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    return {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in blocked_markers)
    }


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _planner_prompt(command: str, context: dict[str, Any] | None) -> str:
    context_json = json.dumps(context or {}, ensure_ascii=False, sort_keys=True)
    return f"""You are the planning layer of HandsFreePC, a Windows voice controller.
Return only a JSON object matching the supplied schema.

Rules:
- You plan; you never execute tools, shell commands, scripts, coordinates, or arbitrary shortcuts.
- Use only the action types in the schema and at most 8 actions.
- Prefer open_path and activate_app before UI actions.
- For Codex or Claude prompt entry, use enter_dictation. Use start_native_voice only when the
  user explicitly asks for the application's own voice mode.
- Never invent a path, project, conversation, tab, or mode. Preserve user-spoken names.
- If the target is ambiguous or the request asks to delete, overwrite, install, pay, change a
  password, reveal secrets, or change security settings, return risk=blocked and no actions.
- Set risk=confirm for an executable file or native application voice mode. The local safety
  policy will independently recompute risk.

Current non-sensitive context: {context_json}
User command: {command}
"""


def _parse_plan_payload(payload: Any, *, source: str) -> Plan:
    try:
        if isinstance(payload, str):
            payload = payload.strip()
            if payload.startswith("```"):
                payload = payload.removeprefix("```json").removeprefix("```")
                payload = payload.removesuffix("```").strip()
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise ValueError(f"Planner returned {type(payload).__name__}, expected object")
        return Plan.from_dict(payload, source=source)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PlannerError(f"Planner output failed local validation: {exc}") from exc


def _parse_claude_envelope(raw_output: str) -> Plan:
    try:
        envelope = json.loads(raw_output)
    except (json.JSONDecodeError, TypeError) as exc:
        raise PlannerError("Claude planner returned invalid JSON") from exc
    if not isinstance(envelope, dict):
        raise PlannerError("Claude planner response envelope must be an object")
    payload = envelope.get("structured_output")
    if payload is None:
        payload = envelope.get("result", envelope)
    return _parse_plan_payload(payload, source="claude")


class CodexPlanner(Planner):
    def __init__(self, settings: PlannerSettings) -> None:
        self.settings = settings

    def plan(self, command: str, *, context: dict[str, Any] | None = None) -> Plan:
        executable = shutil.which(self.settings.codex_executable)
        if executable is None:
            raise PlannerUnavailable(
                f"Codex executable not found: {self.settings.codex_executable}"
            )
        with tempfile.TemporaryDirectory(prefix="handsfreepc-planner-") as temp_dir:
            output_path = Path(temp_dir) / "plan.json"
            args = [
                executable,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "-c",
                "shell_environment_policy.inherit=none",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--output-schema",
                str(schema_path()),
                "--output-last-message",
                str(output_path),
                "--color",
                "never",
                "-C",
                temp_dir,
            ]
            if self.settings.model:
                args.extend(["--model", self.settings.model])
            args.append("-")
            try:
                result = subprocess.run(
                    args,
                    input=_planner_prompt(command, context),
                    capture_output=True,
                    timeout=self.settings.timeout_seconds,
                    check=False,
                    cwd=temp_dir,
                    env=_sanitized_env(),
                    creationflags=_creation_flags(),
                    encoding="utf-8",
                    errors="replace",
                )
            except subprocess.TimeoutExpired as exc:
                raise PlannerError("Codex planner timed out") from exc
            except OSError as exc:
                raise PlannerError(f"Codex planner could not start: {type(exc).__name__}") from exc
            if result.returncode != 0:
                raise PlannerError(f"Codex planner failed with exit code {result.returncode}")
            if not output_path.exists():
                raise PlannerError("Codex planner did not create its structured output file")
            try:
                payload = output_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise PlannerError("Codex planner output could not be read") from exc
            return _parse_plan_payload(payload, source="codex")


class ClaudePlanner(Planner):
    def __init__(self, settings: PlannerSettings) -> None:
        self.settings = settings

    def plan(self, command: str, *, context: dict[str, Any] | None = None) -> Plan:
        executable = shutil.which(self.settings.claude_executable)
        if executable is None:
            raise PlannerUnavailable(
                f"Claude executable not found: {self.settings.claude_executable}"
            )
        args = [
            executable,
            "--safe-mode",
            "-p",
            "--permission-mode",
            "dontAsk",
            "--tools",
            "",
            "--output-format",
            "json",
            "--json-schema",
            _schema_text(),
            "--no-session-persistence",
        ]
        if self.settings.model:
            args.extend(["--model", self.settings.model])
        with tempfile.TemporaryDirectory(prefix="handsfreepc-planner-") as temp_dir:
            try:
                result = subprocess.run(
                    args,
                    input=_planner_prompt(command, context),
                    capture_output=True,
                    timeout=self.settings.timeout_seconds,
                    check=False,
                    cwd=temp_dir,
                    env=_sanitized_env(),
                    creationflags=_creation_flags(),
                    encoding="utf-8",
                    errors="replace",
                )
            except subprocess.TimeoutExpired as exc:
                raise PlannerError("Claude planner timed out") from exc
            except OSError as exc:
                raise PlannerError(f"Claude planner could not start: {type(exc).__name__}") from exc
        if result.returncode != 0:
            raise PlannerError(f"Claude planner failed with exit code {result.returncode}")
        return _parse_claude_envelope(result.stdout)


def build_planner(settings: PlannerSettings) -> Planner | None:
    if not settings.enabled or settings.backend == "none":
        return None
    if settings.backend == "codex":
        return CodexPlanner(settings)
    if settings.backend == "claude":
        return ClaudePlanner(settings)
    raise ValueError(f"Unsupported planner backend: {settings.backend}")
