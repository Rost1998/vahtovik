---
name: groups
description: Create, merge and split the groups of a watch, and schedule their turns.
---

# Groups

A group is a set of related artifacts of one watch, with one persistent agent session. Its folder is `data/watches/<watch>/groups/<key>/`. A group key is lowercase words joined by hyphens, such as `uv-0-9`.

## Before you change groups

- Know the watch: its grouping rule in `monitor.md`, and the Summary in `context.md` of each group involved.
- Show the person the plan: which groups, which artifacts, and the new keys and titles. Act once they agree.
- The commands refuse a group whose window is open, that is busy, or whose turn is unfinished. `uv run vahtovik status --watch <watch>` shows why. A window closes by itself when its agent is done; if a person has it open, ask them to leave it.

## Create

```bash
uv run vahtovik group create <watch>/<key> --title "<title>"
```

Choose a key that fits the watch's grouping rule, so monitoring puts later artifacts of that kind there. An empty group gets no turn until it gets artifacts (`skills/vahtovik/artifacts/SKILL.md`) or a scheduled turn (below).

## Merge

```bash
uv run vahtovik group merge <watch>/<b> [<watch>/<c> ...] --into <watch>/<a>
```

- B's artifacts, reports, turns and costs move to A. If A already has an artifact, B's copy is dropped.
- B's Summary, Open questions and Work log go into a new entry in A's Work log. B's session ends, and its folder goes to `data/watches/<watch>/archive/`.
- From now on, B's key leads to A: artifacts that monitoring gives the key B go to A.
- B's artifacts that waited for a turn now wait for A's next turn. Processed ones stay processed.
- No command undoes a merge, so make sure the person agrees.
- Afterwards, ask A's agent to read the merged record and rewrite its Summary (`skills/vahtovik/tell/SKILL.md`).

## Split

```bash
uv run vahtovik group split <watch>/<a> --artifact <id> [--artifact <id> ...] --into <watch>/<c> [--title "<title>"]
```

- Each artifact's id is the `id` field of its file in `artifacts/`, in A's folder.
- C may be new or existing. The artifacts become new in C, so C's next turn processes them. A note with A's Summary goes into C's Work log, and a note of the move goes into A's.
- Afterwards, ask A's agent to rewrite its Summary without those artifacts.
- Monitoring places later artifacts by the watch's grouping rule and the list of groups. If the rule would still put such artifacts in A, propose a change of `monitor.md` (`skills/vahtovik/create-watch/SKILL.md`).

## Schedule turns

A group gets a turn when it gets new artifacts. It can also get a turn at a set time, even without new artifacts:

```bash
uv run vahtovik group set <watch>/<key> --next 6h --reason "<what the turn should check>"   # one turn in 6 hours
uv run vahtovik group set <watch>/<key> --next off                                          # cancel it
uv run vahtovik group set <watch>/<key> --recheck 1d    # a standing check: each finished turn is followed by one a day later
uv run vahtovik group set <watch>/<key> --recheck off   # end the standing check
```

- The group's status is `scheduled` while it waits for that turn. `vahtovik status` shows the time left, and `-v` shows the reason and the standing check.
- The agent gets the reason in the turn's prompt. Write the reason so it says what to check.
- A group agent can also schedule its own next turn, with `vahtovik turn scheduled --after ...`.
- To change how often the whole watch is monitored, change `interval` in its `watch.yaml` (`skills/vahtovik/create-watch/SKILL.md`).
