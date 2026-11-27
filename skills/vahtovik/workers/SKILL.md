---
name: workers
description: Spawn, watch, merge, split, schedule and archive workers, and set a worker's own model and effort. Use when asked to start a worker on a task, to give a worker another model or effort, to make a worker hear of other workers, to join or separate workers' work, to give a worker a turn at a set time, or to retire a worker.
---

# Workers

A worker is one persistent agent of a role, working on one thing for a long time. Its name is `<id>-<key>`; commands take it as `<id>`, `<id>-<key>` or `<role>/<key>`. Its folder is `data/workers/<role>/<id>-<key>/`.

## Before you change workers

- Know the role (`ROLE.md`), the poller's rule for worker keys (`poller.md`) when a poller feeds the role, and the Summary in `context.md` of each worker involved. `uv run vahtovik worker show <worker>` gives the path.
- Show the person the plan: which workers, which artifacts, the new keys and titles. Act once they agree.
- `merge`, `split` and `archive` refuse a worker whose window is open, that is busy, or whose turn is unfinished. `uv run vahtovik worker show <worker>` shows why. A window closes by itself when its agent is done; if a person has it open, ask them to leave it.

## Spawn

```bash
uv run vahtovik worker spawn <role>/<key> --title "<title>" --message "<the task>" [--skill <skill> ...] [--model <model>] [--effort <effort>]
```

- Choose the role whose description fits the task, and a key that names it: lowercase words joined by hyphens. If a poller feeds the role, use its key rule, so monitoring puts later artifacts of that kind there.
- `--message` is the worker's task: write it so it stands on its own: what to do, why, what is known, and where to report if the role's rules do not say. The worker starts with it at the next run. Without `--message`, the worker waits for artifacts or a message.
- `--skill` gives this worker an extra skill of `knowledge/skills`, besides its role's.
- `--model` and `--effort` give this worker its own model or effort, over its role's (see Model and effort).
- The command prints the new worker's name. Its record says that the person spawned it, or the channel when you answer through one. `--watch` is only for workers that spawn workers: to make a worker watch the new one, use `worker watch` afterwards.

## Model and effort

```bash
uv run vahtovik worker set <worker> [--model <model>] [--effort <effort>]
```

- A worker works with its role's model and effort (`ROLE.md`, else `vahtovik.yaml`), unless it has its own from its spawn or from this command. `uv run vahtovik worker show <worker>` shows what it works with, and which of the two are its own.
- A model is an alias, such as `sonnet`, `opus` or `fable`, or a full model id; vahtovik does not check it, and a wrong one fails the worker's session at its start. An effort is `low`, `medium`, `high`, `xhigh` or `max`. The value `role` gives the worker its role's again.
- To change all workers of a role, change the role (`skills/vahtovik/roles/SKILL.md`), not each worker.
- The change counts from the next start of the worker's session. A turn that works now keeps the earlier ones, and so does a window that is still open: messages to the worker wait for its next turn, whose prompt names the new model and effort.
- A stronger model or a higher effort costs more, and uses more of the Claude plan's usage limits: say so before you set one. Workers can run this command too, for themselves and for other workers.

## Watch

```bash
uv run vahtovik worker watch <b> [<c> ...] --watcher <a>
uv run vahtovik worker unwatch <b> [<c> ...] --watcher <a>
```

- A watches B: vahtovik puts a notice into A's inbox each time B stops working: B ends a turn as `finished`, `scheduled` or `waiting`, needs a person (`maintenance`), or fails for good. Each notice gives A a turn, which costs money: say so before you set a watch.
- Any worker can watch any other one, and several can watch the same one. Workers set their own watches too. `uv run vahtovik worker show <worker>` shows whom it watches and who watches it.
- The command refuses a watch that would close a loop, such as A watches B while B watches A, and names the chain it found.
- A watch lasts until `unwatch`, or until one of the two workers is archived.
- The watch only tells A. Whether A should act on the notices is a matter of its role's rules, or of a message to it (`skills/vahtovik/tell/SKILL.md`).

## Merge

```bash
uv run vahtovik worker merge <b> [<c> ...] --into <a>
```

- B's artifacts, turns, costs, waiting messages and watches move to A, which keeps its role. If A already has an artifact, B's copy is dropped. A watch that would make A watch itself, or close a loop, is dropped too.
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

The worker gets no more turns, its folder goes to `data/archive/<role>/`, and `worker list` and `status` leave it out (`worker list --all` shows it). Its key is free: a later artifact for that key spawns a new worker. Its children go on. Its watches end: it watches nobody, and nobody watches it. No command undoes it.
