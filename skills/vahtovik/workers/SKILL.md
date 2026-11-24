---
name: workers
description: Spawn, merge, split, schedule and archive workers. Use when asked to start a worker on a task, to join or separate workers' work, to give a worker a turn at a set time, or to retire a worker.
---

# Workers

A worker is one persistent agent of a role, working on one thing for a long time. Its name is `<id>-<key>`; commands take it as `<id>`, `<id>-<key>` or `<role>/<key>`. Its folder is `data/workers/<role>/<id>-<key>/`.

## Before you change workers

- Know the role (`ROLE.md`), the poller's rule for worker keys (`poller.md`) when a poller feeds the role, and the Summary in `context.md` of each worker involved. `uv run vahtovik worker show <worker>` gives the path.
- Show the person the plan: which workers, which artifacts, the new keys and titles. Act once they agree.
- `merge`, `split` and `archive` refuse a worker whose window is open, that is busy, or whose turn is unfinished. `uv run vahtovik worker show <worker>` shows why. A window closes by itself when its agent is done; if a person has it open, ask them to leave it.

## Spawn

```bash
uv run vahtovik worker spawn <role>/<key> --title "<title>" --message "<the task>" [--skill <skill> ...]
```

- Choose the role whose description fits the task, and a key that names it: lowercase words joined by hyphens. If a poller feeds the role, use its key rule, so monitoring puts later artifacts of that kind there.
- `--message` is the worker's task: write it so it stands on its own: what to do, why, what is known, and where to report if the role's rules do not say. The worker starts with it at the next run. Without `--message`, the worker waits for artifacts or a message.
- `--skill` gives this worker an extra skill of `knowledge/skills`, besides its role's.
- The command prints the new worker's name. Its record says that the person spawned it, or the channel when you answer through one. `--report-back` is only for workers that spawn workers.

## Merge

```bash
uv run vahtovik worker merge <b> [<c> ...] --into <a>
```

- B's artifacts, turns, costs and waiting messages move to A, which keeps its role. If A already has an artifact, B's copy is dropped.
- B's Summary, Open questions and Work log go into a new entry in A's Work log. B is archived, its session ends, and its folder goes to `data/archive/<role>/`.
- From now on, B's role and key lead to A: artifacts that monitoring gives B's key go to A.
- B's artifacts that waited for a turn now wait for A's next turn. Processed ones stay processed.
- No command undoes a merge, so make sure the person agrees.
- Afterwards, ask A's agent to read the merged record and rewrite its Summary (`skills/vahtovik/tell/SKILL.md`).

## Split

```bash
uv run vahtovik worker split <a> --artifact <id> [--artifact <id> ...] --into <c> [--title "<title>"]
```

- Each artifact's id is the `id` field of its file in `artifacts/`, in A's folder.
- C may be an existing worker, or a new one given as `<role>/<key>`, of any role. The artifacts become new in C, so C's next turn processes them. A note with A's Summary goes into C's Work log, and a note of the move goes into A's.
- Afterwards, ask A's agent to rewrite its Summary without those artifacts.
- Monitoring places later artifacts by the poller's rule and the list of workers. If the rule would still give such artifacts to A, propose a change of `poller.md` (`skills/vahtovik/pollers/SKILL.md`).

## Schedule turns

A worker gets a turn when it gets new artifacts or messages. It can also get a turn at a set time, even without them:

```bash
uv run vahtovik worker schedule <worker> --after 6h --reason "<what the turn should check>"
uv run vahtovik worker schedule <worker> --at 08:00 --reason "..."             # the next 08:00, local time
uv run vahtovik worker schedule <worker> --at "2026-12-01 09:00" --reason "..."
uv run vahtovik worker schedule <worker> --off                                 # cancel it
```

- A worker has one scheduled turn at most; a new one replaces it. Its status is `scheduled` while it waits. `vahtovik status` shows the time left, and `worker show` shows the reason.
- The agent gets the reason in the turn's prompt. Write the reason so it says what to check.
- Workers schedule their own turns too, with `vahtovik turn scheduled`. When a worker ends a turn with `finished`, its work is really done, and that cancels its scheduled turn, also one that you set while it was working.

## Archive

```bash
uv run vahtovik worker archive <worker> --reason "<why>"
```

The worker gets no more turns, its folder goes to `data/archive/<role>/`, and `worker list` and `status` leave it out (`worker list --all` shows it). Its key is free: a later artifact for that key spawns a new worker. Its children go on; their report-back goes nowhere. No command undoes it.
