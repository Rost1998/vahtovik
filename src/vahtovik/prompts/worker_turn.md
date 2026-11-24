# Turn

Worker: `$name` · Role: `$role` · Run: `$run_id` · Turn: `$turn_id` · Time: $now

$session_note

## New artifacts

$artifacts

## Messages

$messages

## Your role's rules (ROLE.md)

$role_rules

## Your skills

When a skill's description fits your task, read its `SKILL.md` and follow it.

$skills

## Your role's memory (MEMORY.md)

Notes that the workers of your role saved for all of them. They are notes, not rules: when they disagree with the rules above, the rules win.

$memory

## Your scheduled turn

$schedule

## Your task

1. If you have not yet in this session, read CLAUDE.md and context.md. Read the new artifact files.
2. Process the new artifacts and do what the messages ask, by your role's rules. Research further with your tools when the rules ask for it. If context.md lists Open questions, check whether the new artifacts or messages answer them. In a scheduled turn (see the note above), do the check it was scheduled for, even if there are no new artifacts. When a task is better done by another worker, spawn one or tell one (see CLAUDE.md, Other workers).
3. Update context.md: rewrite the Summary section so it describes the current state, and add one Work log entry headed `### $now · run $run_id` saying what you did and found. If you learned something that holds for all workers of your role, save it with `vahtovik memory add --text '<note>'`. If an entry of the memory turned out wrong or stale, remove it with `vahtovik memory remove <number> --reason '<why>'`.
4. Report as your role's rules say, with your skills' scripts. Note each report in your Work log, and whether it was delivered. If you need a person's answer to go on, put the questions in your report too.
5. End the turn with exactly one command:
   - `vahtovik turn finished --importance <low|medium|high|critical> --summary "<the current state in one or two sentences>"` when your work is really done. It cancels your scheduled turn; you get a turn again only for new artifacts or messages;
   - `vahtovik turn scheduled --importance <...> --summary "<...>" --after <6h, 1d, ...> --reason "<what the next turn should check>"`, or with `--at <HH:MM, or YYYY-MM-DD HH:MM>` instead of `--after`, when you need another turn later even without new artifacts, for example to see whether something announced has happened. Without `--after` and `--at`, it keeps the turn you have scheduled;
   - `vahtovik turn waiting --importance <...> --summary "<...>" --question "<question>" [--question "<question>" ...]` when you need a person's answer or action to go on. List every question that is still open, including earlier ones;
   - `vahtovik turn background --reason "<what you wait for>" [--check-after 30m]` when you started background work (a background command, a background agent or a scheduled prompt) and go on when it ends. Claude Code wakes you then; finish the steps above and end the turn with one of the commands above.

   Use `finished`, `scheduled` or `waiting` only when nothing you started still runs. If the command prints an error, fix what it says and run it again. Then stop.
