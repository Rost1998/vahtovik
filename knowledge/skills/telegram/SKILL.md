---
name: telegram
description: Send messages to a Telegram chat or channel through a bot, and read the person's messages from the chat. Use it to report to a chat.
credentials: [telegram]
required_env: [TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]
---

# Telegram

`send` sends the subject in bold as the first line, then the body. The bot and the chat come from the environment: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, by default from `knowledge/credentials/telegram.env`.

## Sending a report (workers)

```bash
VAHTOVIK_SUBJECT='<subject>' <this skill's folder>/send <<'BODY'
<message body>
BODY
```

Exit code 0 means delivered, and the last line is the message id. Anything else failed, and the error is on stderr: note it in your Work log.

Formatting rules:
- Telegram HTML only: `<b>`, `<i>`, `<u>`, `<s>`, `<code>`, `<pre>`, `<a href="...">`. No markdown, no other tags.
- Escape `<`, `>` and `&` in plain text as `&lt;`, `&gt;`, `&amp;`.
- Subject plus body must stay under 4000 characters. Aim for a phone screen: a few short lines and the key links.

## As a channel of vahtovik

In `vahtovik.yaml`, a channel's `credentials` and `env` can name another bot and chat. With `input`, vahtovik reads new text messages from the chat `TELEGRAM_CHAT_ID` with the bot's `getUpdates`, and replies to each one in the chat.
- Each message's sender is `@username`, or `id:<user id>` when the sender has no username: use these in the channel's `senders`.
- Messages from other chats are dropped. In a group chat, the bot sees all messages only when its privacy mode is off (BotFather, `/setprivacy`).
- Only one program may read a bot's updates, and the bot must have no webhook. Give each channel with `input` a bot of its own.
- `TELEGRAM_API_URL` (optional) points `send` and `receive` at another Bot API server, such as a local one.
