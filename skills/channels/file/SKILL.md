---
name: file
description: Append reports to a local markdown file, and read the person's messages from another. Useful for testing and as a local archive.
required_env: [FILE_CHANNEL_PATH]
input_env: [FILE_CHANNEL_INBOX]
---

# File channel

Reports are appended to a markdown file, each under a `## <time> · <subject>` heading.

Formatting rules:
- Write plain markdown. Headings inside the body start at `###`.
- Keep the subject under 100 characters.
- No length limit, but stay brief: what happened, why it matters, links.

Input: with `input` in `vahtovik.yaml`, vahtovik reads the person's messages from the file `FILE_CHANNEL_INBOX`. The person writes each message, then a line `---`. Replies are appended to `FILE_CHANNEL_PATH`, each with a line that names the message it answers.
