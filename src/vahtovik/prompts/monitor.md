# Monitoring task

Poller: `$poller` · Source: `$source_id` · Run: `$run_id` · Time: $now

Your working folder is this source's state folder: `$workdir`.
Pass it as the state folder to the source skill's scripts.

## Source skill `$skill_name`

Skill folder: `$skill_dir`

$skill_text

## Source parameters

```yaml
$params
```

## What to look for, and which worker gets it (poller.md)

$poller_rules

## Your skills

When a skill's description fits your task, read its `SKILL.md` and follow it.

$skills

## The roles of this poller's workers

Each artifact goes to a worker of one of these roles.

$roles

## The memory of these roles (MEMORY.md)

Notes that the workers of these roles saved. They are notes, not rules: when they disagree with the rules above, the rules win.

$memory

## Existing workers of these roles

Reuse one of these keys when an artifact belongs to an existing worker. A new key spawns a new worker.

$workers

## Artifacts already registered from this source (most recent)

Do not return these again.

$recent

## Your task

$collect

Then:

1. Decide which items are artifacts by the poller's rules. Ignore the rest.
2. For each artifact give: a stable `id` (as the skill describes, usually the canonical URL or entry identifier), a short `title`, the `url`, a `summary` of a few sentences, optional `details`, and the worker that gets it, by the poller's rules: its `role`, its `worker` key and its `worker_title`. Worker keys are short, lowercase, words joined by hyphens, and unique within a role.
3. Return only new artifacts. If nothing is new, return an empty list. Put anything vahtovik should log, such as fetch problems, in `notes`.
