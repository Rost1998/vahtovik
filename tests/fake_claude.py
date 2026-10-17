"""Stand-in for the `claude` command in tests.

Pops one prepared response from $FAKE_CLAUDE_QUEUE, logs the call to $FAKE_CLAUDE_LOG,
performs the response's file actions in the working folder, and prints a result like `claude -p`.
"""

import json
import os
import re
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
           if k.startswith(("VAHTOVIK_", "CLAUDE_")) or k == "ANTHROPIC_API_KEY"}
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

    reports = []
    for action in response.get("actions", []):
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
            match = re.search(r"delivery (\d+) via (\S+)", proc.stdout + proc.stderr)
            if match and proc.returncode == 0:
                reports.append({"channel": match[2], "delivery_id": int(match[1])})

    if "exit" in response:
        print(response.get("stderr", "fake failure"), file=sys.stderr)
        return response["exit"]

    output = response.get("structured_output")
    if isinstance(output, dict) and output.get("reports") == "from-runs":
        output["reports"] = reports
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": response.get("is_error", False),
        "session_id": resume or new or str(uuid.uuid4()), "total_cost_usd": response.get("cost", 0.01),
        "result": response.get("result", ""), "structured_output": output,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
