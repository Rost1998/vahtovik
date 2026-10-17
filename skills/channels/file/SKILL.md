---
name: file
description: Append reports to a local markdown file. Useful for testing and as a local archive.
required_env: [FILE_CHANNEL_PATH]
---

# File channel

Reports are appended to a markdown file, each under a `## <time> · <subject>` heading.

Formatting rules:
- Write plain markdown. Headings inside the body start at `###`.
- Keep the subject under 100 characters.
- No length limit, but stay brief: what happened, why it matters, links.
