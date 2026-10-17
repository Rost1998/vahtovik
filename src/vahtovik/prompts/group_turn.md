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
2. Process the new artifacts by the processing rules. Research further with your tools when the rules ask for it.
3. Update context.md: rewrite the Summary section so it describes the current state, and add one Work log entry headed `### $now · run $run_id` saying what you did and found.
4. Decide what to report by the reporting rules. Send each report with `vahtovik report --channel <name> --subject "<subject>"` and the message body on stdin, formatted by that channel's rules. Each command prints the delivery id and whether it was sent.
5. Answer with the structured result: the current state in one or two sentences, the importance, the reports you sent (channel and delivery id), and each allowed channel you did not report to, with the reason.
