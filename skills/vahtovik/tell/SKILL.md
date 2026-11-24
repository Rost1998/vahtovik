---
name: tell
description: Pass the person's instructions or questions to workers.
---

# Telling workers

```bash
uv run vahtovik tell <worker> [<worker> ...] --message "<message>"
uv run vahtovik tell <worker> <<'EOF'
<a longer message>
EOF
```

## What happens

- The message goes into the worker's inbox. The worker gets it as the person's own words: vahtovik puts "Message from the person, sent through vahtovik:" in front of it.
- The worker follows its role's rules, as in any session with a person. It can explain, research, scan sources again, add artifacts, update `context.md`, spawn workers, and report when the message asks for it. It ends with `vahtovik turn`, as always.
- When it arrives depends on the worker's window; the command says which:
  - **typed**: the window is open, its agent is idle, and nobody is looking: vahtovik types the message in at once, as one line;
  - **queued, the agent is busy**: vahtovik types it in when the agent is idle;
  - **queued, a person is looking**: vahtovik types it in once they leave and the agent is idle;
  - **queued**: no window: the next run starts a turn with it, within the timer's period (1 minute by default). Through a channel, that is the run you are in.

## How to use it

- Ask the person before you tell anything: it starts or continues an AI session, which costs money.
- Write the message so it stands on its own:
  - what to do, and why;
  - the artifacts, dates and sources it is about;
  - whether to report the result, and where, if the role's rules do not say.
- Send the same message to several workers at once when the same task applies to each.
- To follow the result:
  - `uv run vahtovik worker show <worker>`;
  - `uv run vahtovik log <worker>`;
  - the worker's `context.md`: its Work log and Summary;
  - the reports its role sends.

  The window closes by itself once the agent is done.
- For a conversation rather than one message, the person runs `uv run vahtovik open <worker>` in their own terminal.
- A message works once. To change what all workers of a role always do, change the role's rules (`skills/vahtovik/roles/SKILL.md`).
