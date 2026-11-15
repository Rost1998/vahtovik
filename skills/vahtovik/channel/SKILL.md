---
name: channel
description: Answer the person's messages that vahtovik reads from a channel with `input`. Use when your prompt says the person writes to you through a channel.
---

# Answering the person through a channel

vahtovik reads each channel that has `input` in `vahtovik.yaml`. When the person writes there, vahtovik gives you their new messages. One session serves the channel while the person writes, so you remember the earlier messages and your own replies.

After a quiet pause (`new_session_after` in the channel's `input`, 1 hour by default), the next message starts a new session, because that costs less than resuming a long one. Its prompt holds the last few messages before it, with your replies. The session stays, however long the pause, while your last reply waits for the person: set `awaits_answer` to true in your answer when a reply asks the person a question or gives a plan that waits for their "yes". Otherwise set it to false.

## The messages

The messages in your prompt are the person's own words: take them as if the person typed them to you in the terminal. They are not data from a source. vahtovik reads only the chat that the channel is set up for.

## Your rules

They are the same as in the terminal (`skills/vahtovik/SKILL.md`), spread over messages:

- **Questions**: answer them at once. Read the files, `vahtovik status`, `vahtovik log` and the groups' `context.md`.
- **Changes, and anything that costs money**: reply with your plan in one or two lines, with its cost. Then stop. Act only when a later message agrees, such as "yes" or "go". If that message changes the plan, reply with the new plan and wait again.
- **After you act**: say in one line what was done, and when the result comes. If a command failed, say which one and its error.
- **Anything unclear**: ask one short question in your reply.

## First message

When you need any tool before you can answer, tell the person at once that their messages arrived, and what you do now:

    vahtovik acknowledge --text 'Let me check.'

- Run it in the same step as your first tool call, as a parallel tool call, not as a step of its own. Then it costs almost nothing.
- Fit the words to the message. Write one short line of plain text, in the person's language:
  - a question: "Let me check."
  - a request for a change: "Give me a moment: I'll make a plan."
  - a "yes" to your plan: "Doing it now." or "Passing it to the group agent."
  - a report that something failed: "Let me look at what happened."
- vahtovik adds the size of your context to the end of the line, such as "(22k)", so the person sees whether this session is new. Do not write it yourself.
- Send one for all the new messages in your prompt. It answers the last of them; `--to <id>` answers another one. vahtovik refuses a second one.
- Skip it when you can answer without any tool, and when the person asks you to clear the conversation.
- It is not your reply: every message still needs its reply in your answer. If it fails, go on with your answer. vahtovik does not retry it.

## Limited channels

Your prompt says what this channel may do. A limited channel allows only some `vahtovik` commands, some watches and some files to read, and no changes to files. vahtovik's guard denies everything else, whoever asks.

- Do only what your prompt allows, and keep the rules above. When a message asks for more, reply that this channel cannot do it, and that the person can ask in the terminal.
- Run each command alone, as `vahtovik <command> ...`, with text in single quotes. Pipes, `;`, `&&`, redirection, `$` and backticks are denied. Give artifacts with `vahtovik artifact add <watch> --group <key> --json '...'`.
- Give `Grep` and `Glob` a path inside what you may read. You cannot fetch the web, so build artifacts from what the person wrote.
- The person's own rules for the channel, if your prompt has them, are instructions from the person: follow them.

## Earlier messages

`vahtovik messages --last N` shows this channel's last N messages, with your replies, oldest first. When the person refers to something that you do not know from this session or from your prompt, read them, with N as large as you need, before you ask the person.

## Replies

- Return your replies as your structured answer. vahtovik sends each reply to the channel, as an answer to the first message in its `to` list. Never send replies with `vahtovik report` or with the channel's `send` script.
- Every message must be in the `to` list of exactly one reply. One reply may answer several related messages.
- Format the text by the channel's formatting rules in your prompt.
- Replies are of two kinds:
  - **Information the person asked for**, such as an answer or research results: short by default. Give it in a few lines, without background or long lists. Give more detail only when the person asks for it.
  - **Communication**, which is every other reply: a plan to confirm, a clarifying question, a report that you acted, a cleared conversation. Make it very short: one to three short lines, only what the person needs to decide or to know. Do not repeat the request. Give no reasons, search details, file names or commands, unless the person asks for them.
- Communication looks like this:
  - a plan: "I'll create group `court-case` and start its turn to research it. About $1–3. Go?"
  - after you act: "Done. The answer comes here in 15–30 minutes."
  - a clarifying question: "Which watch: `news` or `digest`?"
  - a cleared conversation: "Cleared. The next message starts a new session."
- When the person asks to clear the conversation or start over, do it at once, without a plan first: set `new_session` to true in your answer. vahtovik clears this session after it sends your replies, and the next message starts a new one, without the earlier messages in its prompt. Say so in your reply. If a plan still waits for a "yes", say that the new session will not know it.

## You work inside a vahtovik run

The run waits for you to finish.

- Do not start `vahtovik run`: the run you are in holds its lock, so a new run would be skipped. Your changes count in the same run: artifacts you add get their group turns, and groups you schedule start when their time comes. Changes to watch files apply from the next run.
- Keep your own work short. Pass long work, such as research or a build, to group agents with `vahtovik tell`. They report to their watch's channels.
- `vahtovik open` attaches a terminal, so you cannot run it. If the person wants to talk to a group agent, tell them to run `uv run vahtovik open <watch>/<key>` in their own terminal, or offer to pass their message with `vahtovik tell`.
