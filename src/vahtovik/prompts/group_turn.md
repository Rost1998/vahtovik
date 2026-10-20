# Processing turn

Run: `$run_id` · Turn: `$turn_id` · Time: $now

$session_note

## New artifacts

$artifacts

## Processing rules

$process_rules

## Reporting rules (report.md)

$report_rules

## Channels you may report to, with their formatting rules

$channels

## Delivery results since your last turn

$deliveries

## Your task

1. If you have not yet in this session, read CLAUDE.md and context.md. Read the new artifact files.
2. Process the new artifacts by the processing rules. Research further with your tools when the rules ask for it. If context.md lists Open questions, check whether the new artifacts answer them.
3. Update context.md: rewrite the Summary section so it describes the current state, and add one Work log entry headed `### $now · run $run_id` saying what you did and found.
4. Decide what to report by the reporting rules. Send each report with `vahtovik report --channel <name> --subject "<subject>"` and the message body on stdin, formatted by that channel's rules. Each command prints the delivery id and whether it was sent. If you need a person's answer to go on, put the questions in the report too.
5. End the turn with exactly one command:
   - `vahtovik turn finished --importance <low|medium|high|critical> --summary "<the current state in one or two sentences>"` when you need nothing from a person;
   - `vahtovik turn waiting --importance <...> --summary "<...>" --question "<question>" [--question "<question>" ...]` when you need a person's answer or action to go on. List every question that is still open, including earlier ones.

   If the command prints an error, fix what it says and run it again. Then stop.
