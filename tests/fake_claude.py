"""Stand-in for the `claude` command in tests.

Logs each start to $FAKE_CLAUDE_LOG and pops one prepared response from $FAKE_CLAUDE_QUEUE per prompt.
It runs the hooks from --settings like Claude Code does (their answers go to $FAKE_CLAUDE_LOG.hooks),
and performs the response's actions in the working folder (their results go to $FAKE_CLAUDE_LOG.runs).

- With -p, the prompt comes on stdin, and the result is printed like `claude -p`.
- Without -p (interactive, in a tmux window), the prompt is the last argument. Then each line typed into the
  window is the next prompt (logged to $FAKE_CLAUDE_LOG.typed); end of input exits.

Response keys: "actions"; "on_block" (actions run when a Stop hook blocks the first time); "exit" (exit code,
right after the actions); "screen" (interactive: show this text and wait for a line of keys, as a startup
screen does, before the prompt); "hang" (interactive: stop responding after the prompt hook).
"""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

VALUE_OPTIONS = {"--model", "--permission-mode", "--settings", "--effort", "--resume", "--session-id", "--name",
                 "--append-system-prompt", "--add-dir", "--output-format", "--json-schema", "--max-budget-usd"}


def main() -> int:
    args = sys.argv[1:]
    options, positional = {}, []
    items = iter(args)
    for arg in items:
        if arg in VALUE_OPTIONS:
            options[arg] = next(items)
        elif not arg.startswith("-"):
            positional.append(arg)
    printing = "-p" in args
    prompt = sys.stdin.read() if printing else (positional[-1] if positional else "")

    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    env = {k: v for k, v in os.environ.items()
           if k.startswith(("VAHTOVIK_", "CLAUDE_")) or k.endswith("_TOKEN") or k == "ANTHROPIC_API_KEY"}
    _log("", {"args": args, "cwd": os.getcwd(), "prompt": prompt, "env": env})

    resume, new = options.get("--resume"), options.get("--session-id")
    if resume and not list((config_dir / "projects").glob(f"*/{resume}.jsonl")):
        print(f"No conversation found with session ID: {resume}", file=sys.stderr)
        return 1
    if new:
        folder = config_dir / "projects" / "fake"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{new}.jsonl").write_text("{}\n")
    session = Session(json.loads(options.get("--settings") or "{}"), resume or new or str(uuid.uuid4()), not printing)

    if printing:
        response = _pop()
        if response is None:
            print("fake claude: no prepared response", file=sys.stderr)
            return 1
        code = session.turn(prompt, response)
        if code is not None:
            return code
        print(json.dumps({
            "type": "result", "subtype": "success", "is_error": response.get("is_error", False),
            "session_id": session.id, "total_cost_usd": response.get("cost", 0.01),
            "result": response.get("result", ""), "structured_output": response.get("structured_output"),
        }))
        return 0

    print(f"fake claude, session {session.id[:8]}", flush=True)
    while True:
        if prompt:
            response = _pop()
            if response is None:
                print("fake claude: no prepared response", flush=True)
            else:
                if "screen" in response:
                    print(response["screen"], flush=True)
                    _log(".typed", {"keys": sys.stdin.readline()})
                code = session.turn(prompt, response)
                if code is not None:
                    return code
        print("> ", end="", flush=True)  # idle, waiting for the person
        line = sys.stdin.readline()
        if not line:
            return 0
        _log(".typed", {"line": line.rstrip("\n")})
        prompt = line.rstrip("\n")


class Session:
    def __init__(self, settings: dict, session_id: str, interactive: bool):
        self.settings, self.id, self.interactive = settings, session_id, interactive

    def turn(self, prompt: str, response: dict) -> int | None:
        """Handle one prompt. Returns an exit code if the response ends the program."""
        if prompt.strip():
            self.hook("UserPromptSubmit", {"prompt": prompt})
        if response.get("hang"):
            while True:
                time.sleep(1)
        perform(response.get("actions", []))
        if "exit" in response:
            print(response.get("stderr", "fake failure"), file=sys.stderr, flush=True)
            return response["exit"]
        for attempt in range(10):
            answers = self.hook("Stop", {"stop_hook_active": attempt > 0})
            if not any(a.get("decision") == "block" for a in answers):
                break
            if attempt == 0:
                perform(response.get("on_block", []))
        if self.interactive:
            print(response.get("result", "done"), flush=True)
        return None

    def hook(self, event: str, payload: dict) -> list[dict]:
        """Run the hook commands for an event; return their JSON answers."""
        answers = []
        for entry in self.settings.get("hooks", {}).get(event, []):
            for item in entry["hooks"]:
                proc = subprocess.run(item["command"], shell=True, text=True, capture_output=True,
                                      input=json.dumps({**payload, "session_id": self.id, "hook_event_name": event}))
                answer = json.loads(proc.stdout) if proc.stdout.strip() else {}
                _log(".hooks", {"event": event, "code": proc.returncode, "answer": answer,
                                "stderr": proc.stderr[-500:]})
                answers.append(answer)
        return answers


def perform(actions: list[dict]) -> None:
    for action in actions:
        if "append" in action:
            with open(action["append"], "a") as handle:
                handle.write(action["text"])
        elif "replace" in action:
            path = Path(action["replace"])
            path.write_text(path.read_text().replace(action["old"], action["new"], 1))
        elif "write" in action:
            path = Path(action["write"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(action["json"]) if "json" in action else action["text"])
        elif "run" in action:
            proc = subprocess.run(action["run"], input=action.get("stdin", ""), text=True, capture_output=True)
            _log(".runs", {"run": action["run"], "code": proc.returncode, "out": proc.stdout + proc.stderr})


def _pop() -> dict | None:
    queue_path = Path(os.environ["FAKE_CLAUDE_QUEUE"])
    queue = json.loads(queue_path.read_text()) if queue_path.exists() else []
    if not queue:
        return None
    response = queue.pop(0)
    queue_path.write_text(json.dumps(queue))
    return response


def _log(suffix: str, record: dict) -> None:
    with open(os.environ["FAKE_CLAUDE_LOG"] + suffix, "a") as handle:
        handle.write(json.dumps(record) + "\n")


if __name__ == "__main__":
    sys.exit(main())
