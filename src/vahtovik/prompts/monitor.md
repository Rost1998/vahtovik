# Monitoring task

Watch: `$watch` · Source: `$source_id` · Run: `$run_id` · Time: $now

Your working folder is this source's state folder: `$workdir`.
Pass it as the state folder to the source skill's scripts.

## Source skill `$skill_name`

Skill folder: `$skill_dir`

$skill_text

## Source parameters

```yaml
$params
```

## What to look for and how to group (monitor.md)

$monitor_rules

## Existing groups of this watch

Reuse one of these keys when an artifact belongs to an existing group.

$groups

## Artifacts already registered from this source (most recent)

Do not return these again.

$recent

## Your task

1. Collect new items from the source by following the source skill.
2. Decide which items are artifacts by the rules in monitor.md. Ignore the rest.
3. For each artifact give: a stable `id` (as the skill describes, usually the canonical URL or entry identifier), a short `title`, the `url`, a `summary` of a few sentences, optional `details`, and a `group_key` and `group_title` by the grouping rules. Group keys are short, lowercase, words joined by hyphens.
4. Return only new artifacts. If nothing is new, return an empty list. Put anything vahtovik should log, such as fetch problems, in `notes`.
