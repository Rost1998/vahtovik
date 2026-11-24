# Turn

Run: `$run_id` · Turn: `$turn_id` · Time: $now

$notes

## New artifacts

$artifacts

## Messages

$messages

## Your scheduled turn

$schedule

## Your task

1. If you have not yet in this session, read context.md. Read the new artifact files.
2. Process the new artifacts and do what the messages ask, by your role's rules in your CLAUDE.md. Research further with your tools when the rules ask for it. If context.md lists Open questions, check whether the new artifacts or messages answer them. In a scheduled turn (see the notes above), do the check it was scheduled for, even if there are no new artifacts. When a task is better done by another worker, spawn one or tell one.
3. Update context.md: rewrite the Summary section so it describes the current state, and add one Work log entry headed `### $now · run $run_id` saying what you did and found. If you learned something that holds for all workers of your role, save it to your role's memory; if an entry of it turned out wrong or stale, remove it.
4. Report as your role's rules say, with your skills' scripts. Note each report in your Work log, and whether it was delivered. If you need a person's answer to go on, put the questions in your report too.
5. End the turn with exactly one `vahtovik turn` command, with `--importance` and `--summary`: `finished`, `scheduled`, `waiting` or `background`, as your CLAUDE.md says under "Ending every turn". If the command prints an error, fix what it says and run it again. Then stop.
