"""The one place that builds and runs `claude` commands."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema

from . import group_status
from .config import Config, StageSettings
from .util import tail

log = logging.getLogger(__name__)

SESSION_MISSING = "No conversation found"


@dataclass
class SessionSpec:
    stage: str
    workdir: Path
    prompt: str
    settings: StageSettings
    schema: dict | None = None
    system_prompt: str | None = None
    mode: str = "throwaway"  # throwaway | new | resume
    session_id: str | None = None
    name: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    scope: str = "session"  # <watch>/<source or group>, for the journal

    @property
    def label(self) -> str:
        return f"{self.stage}-{self.scope.replace('/', '-')}"


@dataclass
class SessionResult:
    ok: bool
    output: dict | None = None
    error: str | None = None
    cost_usd: float = 0.0
    duration_s: float = 0.0
    session_id: str | None = None
    raw_path: Path | None = None
    session_missing: bool = False
    schema_error: bool = False


class Claude:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    @property
    def config_dir(self) -> Path:
        if self.cfg.claude.config_dir:
            return self.cfg.claude.config_dir
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")

    def trusts(self, folder: Path) -> bool | None:
        """Whether Claude Code trusts the folder or a folder above it, so interactive sessions there skip
        the "trust this folder" screen. None if Claude Code's config file cannot be read."""
        base = self.cfg.claude.config_dir or (Path(os.environ["CLAUDE_CONFIG_DIR"])
                                              if os.environ.get("CLAUDE_CONFIG_DIR") else None)
        try:
            projects = json.loads((base / ".claude.json" if base else Path.home() / ".claude.json").read_text())
        except (OSError, ValueError):
            return None
        projects = projects.get("projects") or {}
        folder = folder.resolve()
        return any((projects.get(str(p)) or {}).get("hasTrustDialogAccepted") for p in (folder, *folder.parents))

    def session_exists(self, session_id: str | None) -> bool:
        return bool(session_id) and any((self.config_dir / "projects").glob(f"*/{session_id}.jsonl"))

    def settings_json(self, spec: SessionSpec) -> str:
        """Keep the user's personal and parent-folder CLAUDE.md files out of vahtovik sessions
        (the working folder's own CLAUDE.md still loads), skip the bypassPermissions warning screen,
        and give group sessions the hooks that keep their status (group_status.py)."""
        excludes = [str(Path.home() / ".claude" / "CLAUDE.md"), str(Path.home() / ".claude" / "rules" / "**")]
        for parent in spec.workdir.resolve().parents:
            excludes += [str(parent / name) for name in ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md")]
        settings = {"claudeMdExcludes": excludes, "skipDangerousModePermissionPrompt": True}
        if spec.stage == "group":
            settings["hooks"] = group_status.hooks()
        return json.dumps(settings)

    def _common_args(self, spec: SessionSpec) -> list[str]:
        args = ["--model", spec.settings.model, "--permission-mode", "bypassPermissions",
                "--settings", self.settings_json(spec)]
        if spec.settings.effort:
            args += ["--effort", spec.settings.effort]
        if self.cfg.claude.auth == "api-key":
            # --bare skips CLAUDE.md discovery; --add-dir brings the working folder's CLAUDE.md back.
            args += ["--bare", "--add-dir", str(spec.workdir)]
        if spec.mode == "resume":
            args += ["--resume", spec.session_id]
        else:
            if spec.system_prompt:
                args += ["--append-system-prompt", spec.system_prompt]
            if spec.mode == "new":
                args += ["--session-id", spec.session_id]
                if spec.name:
                    args += ["--name", spec.name]
        return args

    def print_args(self, spec: SessionSpec) -> list[str]:
        """Arguments for print mode. The prompt goes on stdin."""
        args = [self.cfg.claude.binary, "-p", "--output-format", "json"]
        if spec.schema:
            args += ["--json-schema", json.dumps(spec.schema)]
        if spec.settings.max_budget_usd:
            args += ["--max-budget-usd", str(spec.settings.max_budget_usd)]
        if spec.mode == "throwaway":
            args.append("--no-session-persistence")
        return args + self._common_args(spec)

    def interactive_args(self, spec: SessionSpec) -> list[str]:
        """Arguments for the interactive mode of group session windows. The prompt is the last argument."""
        args = [self.cfg.claude.binary, *self._common_args(spec)]
        return args + [spec.prompt] if spec.prompt else args

    def env(self, spec: SessionSpec) -> dict[str, str]:
        env = dict(os.environ)
        env.update(spec.env)
        env["VAHTOVIK_HOME"] = str(self.cfg.root)
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"  # context.md is the memory
        vahtovik = shutil.which("vahtovik")
        bin_dir = str(Path(vahtovik).parent if vahtovik else Path(sys.executable).parent)
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
        if self.cfg.claude.config_dir:
            env["CLAUDE_CONFIG_DIR"] = str(self.cfg.claude.config_dir)
        if self.cfg.claude.auth == "subscription":
            env.pop("ANTHROPIC_API_KEY", None)  # make sure the subscription login is used
        else:
            env["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"] = "1"
        return env

    def run(self, spec: SessionSpec, raw_dir: Path) -> SessionResult:
        """Run a throwaway session in print mode; run it again once if the answer does not match the schema."""
        if self.cfg.claude.auth == "api-key" and not os.environ.get("ANTHROPIC_API_KEY"):
            return SessionResult(ok=False, error="claude.auth is api-key but ANTHROPIC_API_KEY is not set")
        result = self._invoke(spec, raw_dir, attempt=1)
        if result.ok or not result.schema_error:
            return result
        log.warning("%s: Retrying: the answer did not match the schema (%s)", spec.scope, result.error)
        second = self._invoke(spec, raw_dir, attempt=2)
        second.cost_usd += result.cost_usd
        second.duration_s += result.duration_s
        return second

    def _invoke(self, spec: SessionSpec, raw_dir: Path, attempt: int) -> SessionResult:
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / f"{spec.label}-{attempt}.json"
        started = time.monotonic()
        try:
            proc = subprocess.run(
                self.print_args(spec), input=spec.prompt, text=True, capture_output=True,
                cwd=spec.workdir, env=self.env(spec),
            )
        except OSError as exc:
            return SessionResult(ok=False, error=f"cannot run {self.cfg.claude.binary}: {exc}")
        duration = time.monotonic() - started
        raw_path.write_text(proc.stdout or proc.stderr)
        base = {"duration_s": duration, "raw_path": raw_path, "session_id": spec.session_id}
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            message = tail(proc.stderr or proc.stdout) or f"exit code {proc.returncode}"
            return SessionResult(ok=False, error=message, session_missing=SESSION_MISSING in message, **base)
        base["session_id"] = data.get("session_id") or spec.session_id
        base["cost_usd"] = float(data.get("total_cost_usd") or 0)
        if data.get("is_error"):
            message = tail(str(data.get("result") or data.get("subtype") or "error"))
            return SessionResult(ok=False, error=message, session_missing=SESSION_MISSING in message, **base)
        output = data.get("structured_output")
        if spec.schema:
            try:
                jsonschema.validate(output, spec.schema)
            except jsonschema.ValidationError as exc:
                return SessionResult(ok=False, error=exc.message, schema_error=True, output=output, **base)
        return SessionResult(ok=True, output=output, **base)
