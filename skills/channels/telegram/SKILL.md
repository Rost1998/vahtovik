---
name: telegram
description: Send reports to a Telegram chat or channel through a bot, and read the person's messages from the chat.
required_env: [TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]
---

# Telegram channel

The subject is sent in bold as the first line; the body follows.

Formatting rules:
- Telegram HTML only: `<b>`, `<i>`, `<u>`, `<s>`, `<code>`, `<pre>`, `<a href="...">`. No markdown, no other tags.
- Escape `<`, `>` and `&` in plain text as `&lt;`, `&gt;`, `&amp;`.
- Subject plus body must stay under 4000 characters. Aim for a phone screen: a few short lines and the key links.

Input: with `input` in `vahtovik.yaml`, vahtovik reads new text messages from the chat `TELEGRAM_CHAT_ID` with the bot's `getUpdates`, and replies to each one in the chat.
- Each message's sender is `@username`, or `id:<user id>` when the sender has no username: use these in the channel's `senders`.
- Messages from other chats are dropped. In a group chat, the bot sees all messages only when its privacy mode is off (BotFather, `/setprivacy`).
- Only one program may read a bot's updates, and the bot must have no webhook. Give each vahtovik installation that reads its chat a bot of its own.
- `TELEGRAM_API_URL` (optional) points `send` and `receive` at another Bot API server, such as a local one.
