"""The one place that builds and runs `claude` commands."""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema

from . import channel_guard, group_status
from .config import Config, StageSettings
from .util import tail

log = logging.getLogger(__name__)

SESSION_MISSING = "No conversation found"
USAGE_REQUEST = {"type": "control_request", "request_id": "vahtovik-usage",  # what /usage shows, see plan_usage()
                 "request": {"subtype": "get_usage", "skip_behaviors": True}}
USAGE_TIMEOUT = 60
CONTEXT_USAGE = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")  # a step's context


class UsageError(Exception):
    pass


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
    guard: str | None = None  # a limited input channel: its session runs with the guard (channel_guard.py)
    add_dirs: list[Path] = field(default_factory=list)  # for --add-dir: a watch folder, whose skills Claude Code loads
    account: str | None = None  # the watch's account of claude.accounts; None: the default login

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

    def profile(self, account: str | None = None) -> Path:
        """The Claude Code profile folder (CLAUDE_CONFIG_DIR) of an account of claude.accounts; None: the default
        login's. It holds the login and the conversation files."""
        if account:
            return self.cfg.claude.accounts[account]
        if self.cfg.claude.config_dir:
            return self.cfg.claude.config_dir
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")

    def profiles(self) -> list[Path]:
        """The profile folders of the default login and of every account."""
        return list(dict.fromkeys([self.profile(), *self.cfg.claude.accounts.values()]))

    def trusts(self, folder: Path, account: str | None = None) -> bool | None:
        """Whether Claude Code, with the account's profile, trusts the folder or a folder above it, so interactive
        sessions there skip the "trust this folder" screen. None if Claude Code's config file cannot be read."""
        own = account or self.cfg.claude.config_dir or os.environ.get("CLAUDE_CONFIG_DIR")
        try:  # without CLAUDE_CONFIG_DIR, Claude Code keeps the file next to ~/.claude, not in it
            projects = json.loads((self.profile(account) / ".claude.json" if own
                                   else Path.home() / ".claude.json").read_text())
        except (OSError, ValueError):
            return None
        projects = projects.get("projects") or {}
        folder = folder.resolve()
        return any((projects.get(str(p)) or {}).get("hasTrustDialogAccepted") for p in (folder, *folder.parents))

    def session_file(self, session_id: str | None, profiles: list[Path] | None = None) -> Path | None:
        """The file where Claude Code keeps the session's conversation, in any profile unless profiles are given."""
        if not session_id:
            return None
        return next((path for folder in profiles or self.profiles()
                     for path in (folder / "projects").glob(f"*/{session_id}.jsonl")), None)

    def session_exists(self, session_id: str | None, account: str | None = None) -> bool:
        """Whether the account's profile has the session, so a session with that account can resume it."""
        return self.session_file(session_id, [self.profile(account)]) is not None

    def context_tokens(self, session_id: str | None) -> int | None:
        """How many tokens the session's context held at its last step: the input of the last assistant entry
        in its conversation file, cached or not. Claude Code writes a step there before its tool runs, so a
        command that the session runs sees the step that ran it. None if the file has no such entry."""
        path = self.session_file(session_id)
        if path is None:
            return None
        tokens = None
        try:
            with open(path, errors="replace") as handle:
                for raw in handle:
                    if '"usage"' not in raw:
                        continue
                    try:
                        item = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if item.get("type") != "assistant" or item.get("isSidechain"):
                        continue
                    usage = (item.get("message") or {}).get("usage") or {}
                    total = sum(int(usage.get(k) or 0) for k in CONTEXT_USAGE)
                    tokens = total or tokens  # a synthetic entry has no usage
        except OSError:
            return None
        return tokens

    def settings_json(self, spec: SessionSpec) -> str:
        """Keep the user's personal and parent-folder CLAUDE.md files out of vahtovik sessions
        (the working folder's own CLAUDE.md still loads), skip the bypassPermissions warning screen,
        turn off prompt suggestions (see env()), give group sessions the hooks that keep their status
        (group_status.py), and limited input channels their guard (channel_guard.py)."""
        excludes = []
        for folder in dict.fromkeys([Path.home() / ".claude", self.profile(spec.account)]):  # the personal ones
            excludes += [str(folder / "CLAUDE.md"), str(folder / "rules" / "**")]
        for parent in spec.workdir.resolve().parents:
            excludes += [str(parent / name) for name in ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md")]
        settings = {"claudeMdExcludes": excludes, "skipDangerousModePermissionPrompt": True,
                    "promptSuggestionEnabled": False}
        if spec.stage == "group":
            settings["hooks"] = group_status.hooks()
        if spec.guard:
            settings["hooks"] = channel_guard.hooks(spec.guard)
        return json.dumps(settings)

    def _common_args(self, spec: SessionSpec) -> list[str]:
        args = ["--model", spec.settings.model, "--settings", self.settings_json(spec)]
        if spec.guard:  # only the guard's tools, no MCP servers, and Claude Code denies what nothing allowed
            args += ["--permission-mode", "dontAsk", "--tools", ",".join(channel_guard.TOOLS), "--strict-mcp-config"]
        else:
            args += ["--permission-mode", "bypassPermissions"]
        if spec.settings.effort:
            args += ["--effort", spec.settings.effort]
        if self.cfg.claude.auth == "api-key":
            # --bare skips CLAUDE.md discovery; --add-dir brings the working folder's CLAUDE.md back.
            args += ["--bare", "--add-dir", str(spec.workdir)]
        for folder in spec.add_dirs:  # Claude Code loads the skills in its .claude/skills/
            args += ["--add-dir", str(folder)]
        # --add-dir takes several values, so the options below must follow it: interactive_args puts the prompt
        # last, and right after an --add-dir value Claude Code would read the prompt as one more folder.
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
        """Arguments for the interactive mode of group session windows. The prompt is the last argument;
        windows.launch() passes a prompt too long for one argument through a file."""
        args = [self.cfg.claude.binary, *self._common_args(spec)]
        return args + [spec.prompt] if spec.prompt else args

    def env(self, spec: SessionSpec) -> dict[str, str]:
        env = self._base_env(spec.env, spec.account)
        env["VAHTOVIK_HOME"] = str(self.cfg.root)
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"  # context.md is the memory
        # A suggestion shows as grey text in the input box, and the watchdog's screen shows it as typed text.
        env["CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION"] = "false"
        vahtovik = shutil.which("vahtovik")
        bin_dir = str(Path(vahtovik).parent if vahtovik else Path(sys.executable).parent)
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
        if self.cfg.claude.auth != "subscription":
            env["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"] = "1"
        return env

    def _base_env(self, extra: dict[str, str] | None = None, account: str | None = None) -> dict[str, str]:
        """The environment of every `claude` that vahtovik runs: its profile and its login."""
        env = {**os.environ, **(extra or {})}
        if account or self.cfg.claude.config_dir:
            env["CLAUDE_CONFIG_DIR"] = str(self.profile(account))
        if self.cfg.claude.auth == "subscription":
            env.pop("ANTHROPIC_API_KEY", None)  # make sure the subscription login is used
        if account:  # Claude Code would use these before the login in the account's profile
            env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
        return env

    def plan_usage(self, account: str | None = None) -> dict | None:
        """How full the Claude plan's usage limits are: the `rate_limits` that Claude Code's /usage shows, or None
        when plan limits do not apply, as with an API key. Claude Code answers this control request without an AI
        session, with the login of the account's profile, which it refreshes itself. Raises UsageError when it
        gives no answer."""
        args = [self.cfg.claude.binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--no-session-persistence", "--strict-mcp-config"]
        try:
            proc = subprocess.run(args, input=json.dumps(USAGE_REQUEST) + "\n", text=True, capture_output=True,
                                  cwd=self.cfg.root, env=self._base_env(account=account), timeout=USAGE_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise UsageError(f"{self.cfg.claude.binary} gave no answer in {USAGE_TIMEOUT} s") from None
        except OSError as exc:
            raise UsageError(f"cannot run {self.cfg.claude.binary}: {exc}") from None
        for line in proc.stdout.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, dict) or item.get("type") != "control_response":
                continue
            response = item.get("response") or {}
            if response.get("subtype") != "success":
                raise UsageError(tail(str(response.get("error") or response)))
            answer = response.get("response") or {}
            if not answer.get("rate_limits_available"):
                return None
            if not isinstance(answer.get("rate_limits"), dict):
                raise UsageError("Claude Code could not get the usage from claude.ai")
            return answer["rate_limits"]
        raise UsageError(tail(proc.stderr or proc.stdout) or f"exit code {proc.returncode}")

    def login(self, account: str | None = None) -> dict | None:
        """What `claude auth status` says about the login of the account's profile, such as loggedIn and email;
        None when it gives no answer."""
        try:
            proc = subprocess.run([self.cfg.claude.binary, "auth", "status", "--json"], text=True,
                                  capture_output=True, cwd=self.cfg.root, env=self._base_env(account=account),
                                  timeout=USAGE_TIMEOUT)
            answer = json.loads(proc.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
        return answer if isinstance(answer, dict) else None

    def start_command(self, account: str | None = None) -> str:
        """The command that starts Claude Code with the account's profile, for a person to log in with."""
        if account or self.cfg.claude.config_dir:
            return f"CLAUDE_CONFIG_DIR={shlex.quote(str(self.profile(account)))} {self.cfg.claude.binary}"
        return self.cfg.claude.binary

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
