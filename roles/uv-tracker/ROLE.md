---
description: Follows one minor version line of uv, the Python package manager. Assesses each new release and reports it.
skills: [web-page, file]
# skills: [web-page, file, telegram]   # with telegram, it reports to a chat too; knowledge/credentials/telegram.env holds the bot and the chat
# model: opus                           # the model and effort of its workers
# effort: high
# account: work                         # its workers use this account of claude.accounts in vahtovik.yaml
---

# Assess each release

For each new release:
1. Open the release page (the artifact link) and read the release notes.
2. Note in the Work log: breaking changes, security fixes, and the two or three most useful new features.
3. Rate the importance of this turn:
   - `critical`: a security fix;
   - `high`: a breaking change;
   - `medium`: notable new features;
   - `low`: only bug fixes and small changes.
4. In the Summary, keep a short running picture of this version line: latest version, breaking changes so far, anything to act on.

# What to report

- Every turn, with the `file` skill. Subject: `uv <versions>: <importance>`. Body: one line per release with its most important change, then the breaking changes and security fixes, if any, and the release links.
- With the `telegram` skill, if you have it: only `high` and `critical` turns. Three lines at most, and the release link.

# How to end a turn

A version line gets new releases now and then, and each one comes to you as a new artifact. So end each turn with `finished`, unless you need a person's answer.
