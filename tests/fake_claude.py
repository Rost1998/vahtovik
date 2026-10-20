"""Stand-in for the `claude` command in tests.

Pops one prepared response from $FAKE_CLAUDE_QUEUE, logs the call to $FAKE_CLAUDE_LOG,
runs the hooks from --settings like Claude Code does (logging their answers next to the log),
performs the response's actions in the working folder, and prints a result like `claude -p`.

When a Stop hook blocks, the response's "on_block" actions run once; later blocks get no reaction.
"""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    prompt = sys.stdin.read() if "-p" in args else ""

    def option(name):
        return args[args.index(name) + 1] if name in args else None

    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    env = {k: v for k, v in os.environ.items()
           if k.startswith(("VAHTOVIK_", "CLAUDE_")) or k.endswith("_TOKEN") or k == "ANTHROPIC_API_KEY"}
    with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
        log.write(json.dumps({"args": args, "cwd": os.getcwd(), "prompt": prompt, "env": env}) + "\n")

    resume, new = option("--resume"), option("--session-id")
    if resume and not list((config_dir / "projects").glob(f"*/{resume}.jsonl")):
        print(f"No conversation found with session ID: {resume}", file=sys.stderr)
        return 1

    queue_path = Path(os.environ["FAKE_CLAUDE_QUEUE"])
    queue = json.loads(queue_path.read_text()) if queue_path.exists() else []
    if not queue:
        print("fake claude: no prepared response", file=sys.stderr)
        return 1
    response = queue.pop(0)
    queue_path.write_text(json.dumps(queue))

    if new:
        folder = config_dir / "projects" / "fake"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{new}.jsonl").write_text("{}\n")

    settings = json.loads(option("--settings") or "{}")
    session_id = resume or new or str(uuid.uuid4())
    if prompt.strip():
        hook(settings, "UserPromptSubmit", {"session_id": session_id, "prompt": prompt})
    perform(response.get("actions", []))

    if "exit" in response:
        print(response.get("stderr", "fake failure"), file=sys.stderr)
        return response["exit"]

    for attempt in range(10):
        answers = hook(settings, "Stop", {"session_id": session_id, "stop_hook_active": attempt > 0})
        if not any(a.get("decision") == "block" for a in answers):
            break
        if attempt == 0:
            perform(response.get("on_block", []))

    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": response.get("is_error", False),
        "session_id": session_id, "total_cost_usd": response.get("cost", 0.01),
        "result": response.get("result", ""), "structured_output": response.get("structured_output"),
    }))
    return 0


def hook(settings: dict, event: str, payload: dict) -> list[dict]:
    """Run the hook commands for an event; return their JSON answers."""
    answers = []
    for entry in settings.get("hooks", {}).get(event, []):
        for item in entry["hooks"]:
            proc = subprocess.run(item["command"], shell=True, text=True, capture_output=True,
                                  input=json.dumps({**payload, "hook_event_name": event}))
            answer = json.loads(proc.stdout) if proc.stdout.strip() else {}
            with open(os.environ["FAKE_CLAUDE_LOG"] + ".hooks", "a") as log:
                log.write(json.dumps({"event": event, "code": proc.returncode, "answer": answer,
                                      "stderr": proc.stderr[-500:]}) + "\n")
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
            with open(os.environ["FAKE_CLAUDE_LOG"] + ".runs", "a") as log:
                log.write(json.dumps({"run": action["run"], "code": proc.returncode,
                                      "out": proc.stdout + proc.stderr}) + "\n")


if __name__ == "__main__":
    sys.exit(main())
