---
name: file
description: Append messages to a local markdown file, by default data/outbox.md, and read the person's messages from another file. Use it to report locally, as an archive, or for tests.
input_env: [FILE_CHANNEL_INBOX]
---

# File

`send` appends each message to a markdown file, under a `## <time> · <subject>` heading, with a line that says who sent it.

## Sending a report (workers)

```bash
VAHTOVIK_SUBJECT='<subject>' <this skill's folder>/send <<'BODY'
<message body>
BODY
```

It prints where it wrote the message. The file is `FILE_CHANNEL_PATH` if your role or environment sets it, else `data/outbox.md` in the vahtovik folder.

Formatting rules:
- Write plain markdown. Headings inside the body start at `###`.
- Keep the subject under 100 characters.
- No length limit, but stay brief: what happened, why it matters, links.

## As a channel of vahtovik

In `vahtovik.yaml`, `env: {FILE_CHANNEL_PATH: ...}` names the file. With `input`, vahtovik reads the person's messages from the file `FILE_CHANNEL_INBOX`: the person writes each message, then a line `---`. Replies are appended to `FILE_CHANNEL_PATH`, each with a line that names the message it answers.
