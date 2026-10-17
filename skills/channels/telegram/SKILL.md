---
name: telegram
description: Send reports to a Telegram chat or channel through a bot.
required_env: [TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]
---

# Telegram channel

The subject is sent in bold as the first line; the body follows.

Formatting rules:
- Telegram HTML only: `<b>`, `<i>`, `<u>`, `<s>`, `<code>`, `<pre>`, `<a href="...">`. No markdown, no other tags.
- Escape `<`, `>` and `&` in plain text as `&lt;`, `&gt;`, `&amp;`.
- Subject plus body must stay under 4000 characters. Aim for a phone screen: a few short lines and the key links.
