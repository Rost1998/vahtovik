---
name: roles
description: Create or change a role - what its workers do, their working rules, skills, model, effort and Claude account. Use when asked to set up a new kind of worker, or to change how workers of a role process, report or end their turns.
---

# Roles

A role says what a worker is and how it works. Every worker has one role, and all workers of a role follow the same rules. Look at `roles/uv-tracker/` before you write one.

## 1. Agree on the plan

Settle with the person:
- what one worker of the role works on, and for how long: one version line, one case, one research task;
- how it processes its artifacts and messages, and which decisions belong to a person;
- what it reports, when, where, and with which skill;
- when it needs another turn without new artifacts: `scheduled`, and when its work is really done: `finished`;
- which skills and credentials it needs (`skills/vahtovik/knowledge/SKILL.md`).

Ask about anything unclear.

## 2. `roles/<role>/ROLE.md`

The folder name is the role's name: lowercase words joined by hyphens.

```
---
description: <one line: what a worker of this role does; pollers and other workers choose roles by it>
skills: [web-page, telegram]   # specialized skills of knowledge/skills; the shared ones come by themselves
credentials: [telegram-ops]    # optional: more files of knowledge/credentials, over its skills' own
model: opus                    # optional: the model and effort of its workers; vahtovik.yaml's by default
effort: high                   # low, medium, high, xhigh or max
account: work                  # optional: a Claude account of claude.accounts in vahtovik.yaml
---
```

Then the working rules, as plain instructions to the worker:
- **Processing**: what to do with new artifacts and messages; what to note in the Work log and the Summary; how to rate importance (`low`, `medium`, `high`, `critical`). If some decisions belong to a person, say which: the worker then asks in its report and ends the turn as `waiting`. If processing starts long work, such as a build, the rules may tell it to run the work in the background: it ends the turn as `background`, and Claude Code wakes it when the work ends.
- **Reporting**: for each place, when to report (for example only `high` and `critical` turns), with which skill, the subject, and what the body holds. vahtovik does not report for workers; without these rules, a worker reports nowhere.
- **Ending turns**: when the work is really done (`finished`), and when the worker should come back later by itself (`scheduled --after` or `--at`), for example to see whether something announced has happened. `finished` cancels any scheduled turn, and the worker then gets a turn only for new artifacts or messages. Nothing repeats by itself: for a check that must come again and again, the rules say to end every such turn with `scheduled` again, and when to stop.
- **Spawning**, if the role should: when to spawn workers of which role, and whether to watch them (`--watch`) and wait for their results. How workers wait for each other is in the rules of every worker (`knowledge/shared/workers/SKILL.md`, "Working with other workers"): do not repeat it in a role.

**Keep the rules short.** vahtovik writes them into each worker's `CLAUDE.md`, so they stay in the session's context all the time. Each rule says in a few lines what to do and when. Put the long parts in skills: detailed steps, checklists, examples and scripts. In the rule, refer to the skill by name and say when to use it, for example: "To rate an item's urgency, use the skill `triage`."

Workers write only inside their folder. If the role needs them to act outside it, for example to change a file elsewhere or run a command, say so plainly in the rules. Do not repeat what vahtovik already tells them: sessions, `context.md`, `vahtovik turn`, spawning.

A Claude account is a Claude Code profile folder with its own login, so that roles spread over the usage limits of several plans. To add one, write it in `vahtovik.yaml` (`claude: {accounts: {work: {config_dir: ~/.claude-work}}}`). Then the person runs `CLAUDE_CONFIG_DIR=~/.claude-work claude` in the project folder, logs in with that account, and accepts "trust this folder". Never log in for them, and never read the profile's login files. `vahtovik check` shows who each account is logged in as.

## 3. Check

1. `uv run vahtovik check`, and fix every problem it reports.
2. A role gets its workers from pollers (`skills/vahtovik/pollers/SKILL.md`), from `vahtovik worker spawn` (`skills/vahtovik/workers/SKILL.md`), and from other workers.

## Changing a role later

Read its `ROLE.md`, its `MEMORY.md` and `uv run vahtovik worker list --role <role>` first, and tell the person what you will change.

- New rules and skills apply from each worker's next turn: vahtovik writes the rules into the worker's `CLAUDE.md` and links the skills again before every start, and the turn's prompt tells the worker what changed. A worker in an open window gets them at its next turn.
- `account:` moves the role's workers to another Claude account. Claude Code keeps conversations per account, so each worker then starts a new session and rebuilds its picture from `context.md`. Tell the person so before the change.
- A role cannot be renamed or removed while it has workers: its name is in their records and folders, and workers without their role get no turns (`vahtovik check` reports them). Make a new role, and merge or spawn workers into it; archive the workers of a role before you remove it.
- `MEMORY.md` holds notes, not rules. Change it with `vahtovik memory add|remove --role <role>`, or let the person edit it by hand. Each worker's next turn names the entries that came, went or were edited, and the worker reads those.
