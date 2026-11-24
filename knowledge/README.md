# vahtovik knowledge

Everything that vahtovik's workers and monitoring sessions may know. A skill is a folder with a `SKILL.md` file (front matter with `name` and `description`, then instructions for the agent) and optional docs and scripts. Adding a skill needs no Python changes.

- `shared/<skill>/`: every worker and every monitoring session has these. `shared/workers/` holds the rules of every worker; vahtovik also puts it in their system prompt.
- `skills/<skill>/`: specialized skills. A role's `skills:`, `vahtovik worker spawn --skill`, a poller source's `skill:`, and a channel's `skill:` in `vahtovik.yaml` name them.
- `credentials/<name>.env`: secrets, as `KEY=value` lines, never in git (`example.env` shows the format). `vahtovik.env` holds vahtovik's own, such as `ANTHROPIC_API_KEY`.

vahtovik links each session's skills into its folder, `.claude/skills/<skill>`, where Claude Code finds them, and lists their names, descriptions and paths in the session's `CLAUDE.md` or prompt. vahtovik's own skills, which no worker gets, are in `skills/` in the project root: the vahtovik agent's and the watchdog's.

## SKILL.md

```
---
name: <the folder name>
description: <what it does and when to use it; agents choose a skill by this line>
credentials: [<name>, ...]     # optional: files of credentials/ that its scripts need
required_env: [...]            # optional: variables its scripts need; `vahtovik check` verifies them
required_params: [...]         # optional, for source skills: the params every poller source of it needs
input_env: [...]               # optional, for channel skills: the variables only `receive` needs
---
```

`vahtovik check` reports a skill without `SKILL.md` or a description, a `name` other than the folder name, a specialized skill with a shared skill's name, and, for the skills that roles, sources and channels use, missing credential files and variables.

## Credentials

A session gets the variables of the credential files of its skills, and then of the files its role, its poller source or its channel names with its own `credentials:`; later files win. So two sources of one skill can use different tokens, and two channels of the `telegram` skill can use different bots. A missing file gives nothing, and `vahtovik check` reports it.

## Source skills

```
skills/<name>/SKILL.md
skills/<name>/scripts/...     optional
skills/<name>/scan            optional executable, for sources with `scan: script`; only vahtovik runs it
```

- The body tells the monitoring agent how to collect new items and what to use as the stable artifact id.
- Scripts take the source parameters as arguments and a state folder (`--state-dir`) for snapshots or "last seen" markers, and print what is new, preferably as JSON.
- In monitoring, the state folder is the session's working folder. If the session fails, or the run stops before the new artifacts are saved, vahtovik puts the folder back as it was and monitors the source again, so no item is lost.
- Workers may use the same skill to scan again, with a state folder inside their own folder.

A source in `poller.yaml` has `scan: ai` (the default) or `scan: script`:
- `ai`: the monitoring session collects the items itself, by the skill's body.
- `script`: vahtovik runs the skill's `scan` first, and starts a monitoring session only when it finds new items. The session gets the items in its prompt and decides which are artifacts and which worker gets them. Use it for sources whose new items a script can tell for certain, such as feeds or message lists, to read them often at little cost.

`scan` contract:
- it runs in the vahtovik project folder with a 5-minute timeout;
- `VAHTOVIK_STATE_DIR` is the source's state folder, `VAHTOVIK_PARAMS` holds the source's `params` as JSON; also `VAHTOVIK_POLLER`, `VAHTOVIK_SOURCE`, and the source's credentials as environment variables;
- it prints `{"items": [{"id": "...", ...}], "error": null}`: only the items it has not printed before for this state folder, each an object with an `id` and any other fields;
- exit code 0 and `error` null mean success. Anything else fails the monitoring, and vahtovik puts the state folder back.

## Reporting and channel skills

```
skills/<name>/SKILL.md       how to send, and the formatting rules
skills/<name>/send           executable: workers run it to report; vahtovik runs it for its channels
skills/<name>/receive        optional executable, for vahtovik's channels with `input`; only vahtovik runs it
```

`send` contract:
- message body on stdin; subject in `VAHTOVIK_SUBJECT`;
- the settings and secrets it needs as environment variables: from the skill's credentials, and for a channel of vahtovik also the channel's `credentials` and `env` in `vahtovik.yaml`;
- exit code 0 means delivered; the last line of stdout may hold a delivery identifier;
- any other exit code is a failure, and stderr holds the reason;
- `VAHTOVIK_DRY_RUN=1` means do not send; print what would be sent to stderr and exit 0;
- `VAHTOVIK_PLAIN=1` means the body is plain text, such as a notice from vahtovik or a channel agent's first short line: if the skill uses markup, escape the body;
- `VAHTOVIK_REPLY_TO` is set when the message answers a message that `receive` read: its id. A skill that can reply to a message does so; others may ignore it;
- `VAHTOVIK_WORKER_NAME` is set when a worker runs it (the worker's name), and empty for vahtovik's own messages;
- vahtovik runs it in the project folder with a 60-second timeout; a worker runs it wherever it is.

`receive` contract:
- `VAHTOVIK_STATE_DIR` is the channel's state folder, for "last read" markers; also `VAHTOVIK_CHANNEL`, and the channel's credentials and `env`;
- it prints `{"messages": [{"id": "...", "text": "...", "from": "...", "time": "..."}], "error": null}`: only messages it has not printed before for this state folder, and only messages from people, never the ones vahtovik sent; `id` must be what `send` needs in `VAHTOVIK_REPLY_TO`; `from` must be a stable id of the sender that nobody else can take, such as a username, never a display name: a channel's `senders` in `vahtovik.yaml` matches it;
- exit code 0 and `error` null mean success. Anything else fails the read, and vahtovik puts the state folder back;
- it runs in the vahtovik project folder with a 60-second timeout.
