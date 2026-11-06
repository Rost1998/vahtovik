---
name: tell
description: Pass the person's instructions or questions to the agents of group sessions.
---

# Telling group agents

```bash
uv run vahtovik tell <watch>/<key> [<watch>/<key> ...] --message "<message>"
uv run vahtovik tell <watch>/<key> <<'EOF'
<a longer message>
EOF
```

## What happens

- The group agent gets the message as the person's own words. vahtovik puts "Message from the person, sent through the vahtovik agent:" in front of it.
- The agent follows its watch's rules, as in any session with a person. It can explain, research, scan sources again, add artifacts, update `context.md`, and send reports when the message asks for them. It ends with `vahtovik turn`, as always.
- What vahtovik does depends on the group's window:
  - **window open**, during a turn or another session: vahtovik types the message into it. A busy agent gets the message when it is done with what it does now. vahtovik types the message as one line.
  - **no window**: vahtovik starts the session with the message.
  - **skipped**, and the command says why: a person is looking at the window, the group needs a person first (`maintenance`), or the group is busy.

## How to use it

- Ask the person before you tell anything: it starts or continues an AI session, which costs money.
- Write the message so it stands on its own:
  - what to do, and why;
  - the artifacts, dates and sources it is about;
  - whether to report the result.
- Send the same message to several groups at once when the same task applies to each.
- To follow the result:
  - `uv run vahtovik status --watch <watch>`;
  - `uv run vahtovik log <watch>/<key>`;
  - the group's `context.md`: its Work log and Summary;
  - the reports in the watch's channels.

  The window closes by itself once the agent is done.
- For a conversation rather than one message, the person runs `uv run vahtovik open <watch>/<key>` in their own terminal.
- A message works once. To change what a group agent always does, change the watch's rules (`process/*.md`, `report.md`) with `skills/vahtovik/create-watch/SKILL.md`.
