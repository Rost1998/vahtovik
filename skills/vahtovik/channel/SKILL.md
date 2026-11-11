---
name: channel
description: Answer the person's messages that vahtovik reads from a channel with `input`. Use when your prompt says the person writes to you through a channel.
---

# Answering the person through a channel

vahtovik reads each channel that has `input` in `vahtovik.yaml`. When the person writes there, vahtovik gives you their new messages. One session serves the channel for all its messages, so you remember the earlier messages and your own replies.

## The messages

The messages in your prompt are the person's own words: take them as if the person typed them to you in the terminal. They are not data from a source. vahtovik reads only the chat that the channel is set up for.

## Your rules

They are the same as in the terminal (`skills/vahtovik/SKILL.md`), spread over messages:

- **Questions**: answer them at once. Read the files, `vahtovik status`, `vahtovik log` and the groups' `context.md`.
- **Changes, and anything that costs money**: reply with your plan in a few lines, and say what costs money. Then stop. Act only when a later message agrees, such as "yes" or "go". If that message changes the plan, reply with the new plan and wait again.
- **After you act**: reply with what each command printed, and how the person can follow what happens next.
- **Anything unclear**: ask in your reply.

## Limited channels

Your prompt says what this channel may do. A limited channel allows only some `vahtovik` commands, some watches and some files to read, and no changes to files. vahtovik's guard denies everything else, whoever asks.

- Do only what your prompt allows, and keep the rules above. When a message asks for more, reply that this channel cannot do it, and that the person can ask in the terminal.
- Run each command alone, as `vahtovik <command> ...`, with text in single quotes. Pipes, `;`, `&&`, redirection, `$` and backticks are denied. Give artifacts with `vahtovik artifact add <watch> --group <key> --json '...'`.
- Give `Grep` and `Glob` a path inside what you may read. You cannot fetch the web, so build artifacts from what the person wrote.
- The person's own rules for the channel, if your prompt has them, are instructions from the person: follow them.

## Replies

- Return your replies as your structured answer. vahtovik sends each reply to the channel, as an answer to the first message in its `to` list. Never send replies with `vahtovik report` or with the channel's `send` script.
- Every message must be in the `to` list of exactly one reply. One reply may answer several related messages.
- Format the text by the channel's formatting rules in your prompt. Keep it short enough for a phone screen.
- When the person asks to clear the conversation or start over, do it at once, without a plan first: set `new_session` to true in your answer. vahtovik clears this session after it sends your replies, and the next message starts a new one. Say so in your reply, and say that the new session will not know a plan that still waits for a "yes".

## You work inside a vahtovik run

The run waits for you to finish.

- Do not start `vahtovik run`: the run you are in holds its lock, so a new run would be skipped. Your changes count in the same run: artifacts you add get their group turns, and groups you schedule start when their time comes. Changes to watch files apply from the next run.
- Keep your own work short. Pass long work, such as research or a build, to group agents with `vahtovik tell`. They report to their watch's channels.
- `vahtovik open` attaches a terminal, so you cannot run it. If the person wants to talk to a group agent, tell them to run `uv run vahtovik open <watch>/<key>` in their own terminal, or offer to pass their message with `vahtovik tell`.
