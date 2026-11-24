---
name: knowledge
description: Add or change what vahtovik's workers know - a shared or specialized skill, its docs and scripts - and the credentials that skills need. Use when asked to teach workers something, to add a source type or a reporting channel, or to give them a token.
---

# The knowledge layer

`knowledge/` holds everything workers and monitoring sessions may know. `README.md` (The knowledge layer) explains it, and `knowledge/README.md` gives the contracts of the scripts that vahtovik runs. Look at the existing skills before you write a new one.

## 1. Agree on the plan

Settle with the person:
- what the skill teaches, and who needs it: every worker (shared) or only some (specialized);
- which scripts it needs, and which secrets they read;
- which roles, pollers or channels will use it.

Ask about anything unclear.

## 2. Shared or specialized

- `knowledge/shared/<skill>/`: every worker and every monitoring session gets it. Keep this small: each shared skill is listed in every session. `knowledge/shared/workers/` holds the rules of every worker: change it only when the person asks to change how all workers work.
- `knowledge/skills/<skill>/`: only the sessions that name it get it:
  - a role's `skills:` in its `ROLE.md`, for all its workers;
  - `vahtovik worker spawn --skill <skill>`, for one worker;
  - a poller source's `skill:`, for its monitoring session;
  - a channel's `skill:` in `vahtovik.yaml`, for vahtovik's own messages.

A skill's name is its folder name: lowercase words joined by hyphens. A specialized skill may not have a shared skill's name.

## 3. The skill

```
knowledge/skills/<skill>/SKILL.md
knowledge/skills/<skill>/<docs>.md       optional: reference the agent reads when it needs it
knowledge/skills/<skill>/scripts/...     optional
knowledge/skills/<skill>/scan            optional: for poller sources with `scan: script`
knowledge/skills/<skill>/send            optional: for reports, and for vahtovik's channels
knowledge/skills/<skill>/receive         optional: for vahtovik's channels with `input`
```

`SKILL.md`:

```
---
name: <skill>
description: <what it does and when to use it; agents choose a skill by this line>
credentials: [<name>]          # optional: files of knowledge/credentials that its scripts need
required_env: [...]            # optional: variables its scripts need; `vahtovik check` verifies them
required_params: [...]         # optional, for source skills: parameters every source of it needs
---
```

Then the instructions. Keep `SKILL.md` short and put long references into other files of the folder: the agent reads them only when it needs them.

- **Source skills** (used by pollers): the parameters, how to run the scripts, what they print, what to use as the stable artifact id, and what to do on errors. Scripts take the parameters as arguments and `--state-dir <folder>` for "already seen" markers, and print only new items, preferably as JSON with an `error` field. A `scan` script follows the contract in `knowledge/README.md`; give a skill one when a script can tell its new items for certain, so a monitoring session starts only when there are new items.
- **Reporting skills** (used by workers, and by vahtovik's channels): how a worker runs `send` (body on stdin, subject in `VAHTOVIK_SUBJECT`), and the formatting rules, such as markup and length limits. `send` and `receive` follow the contracts in `knowledge/README.md`. See `knowledge/skills/telegram/`.
- **Knowledge skills**: plain reference, such as how a system works, a checklist, or a glossary. No scripts needed.

Scripts use bash or the Python standard library, so they run with the system `python3`. Make them executable (`chmod +x`). They read secrets from environment variables, never from parameters.

A skill is generic. What one role or poller does with it goes into that role's or poller's rules.

## 4. Credentials

- One file per name: `knowledge/credentials/<name>.env`, with `KEY=value` lines. Git ignores these files; `example.env` shows the format.
- A skill names its files in `credentials:`. A role, a poller source or a channel names more with its own `credentials:`; later files win. So two sources of one skill can use different tokens: `credentials: [github-org-b]` on one of them.
- `knowledge/credentials/vahtovik.env` holds vahtovik's own secrets, such as `ANTHROPIC_API_KEY`.
- Never put secrets in yaml or markdown files, and never print them. If the person has not given a value, write the line with a placeholder and tell them which file and line to fill in.

## 5. Check

1. `uv run vahtovik check`, and fix every problem it reports.
2. A worker gets a new or changed skill at the start of its next turn: vahtovik links its skills again before every start, and the turn's prompt tells the worker which skill is new, gone or changed (its `SKILL.md`), so it reads the skill again before it uses it.

Never edit `data/`: it is vahtovik's runtime state.
