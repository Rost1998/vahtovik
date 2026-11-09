# vahtovik skills

A skill is a folder with a `SKILL.md` file (front matter with `name` and `description`, then instructions for the agent) and optional scripts. To add a source type or a channel, add a folder; no Python changes are needed.

- `shared/`: the shared skill. vahtovik puts it in the system prompt of every group and monitoring session.
- `vahtovik/`: the skills of the vahtovik agent, which you talk to when you run `claude` in the project folder, or through a channel with `input`. The root `CLAUDE.md` loads `vahtovik/SKILL.md`, which names a sub-skill for each kind of task: `create-watch/`, `groups/`, `artifacts/`, `tell/` and `channel/`.
- `watchdog/`: the rules of the watchdog AI session, and the screens it may get past (with their keys) in its front matter.
- `sources/<name>/`: how to collect items from one kind of source.
- `channels/<name>/`: how to format and send reports to one kind of channel, and optionally how to read the person's messages from it.

## Source skill

```
sources/<name>/SKILL.md
sources/<name>/scripts/...     optional
sources/<name>/scan            optional executable, for sources with `scan: script`; only vahtovik runs it
```

- Front matter: `name`, `description`, and optionally:
  - `required_params: [...]`, which `vahtovik check` verifies against each source's `params` in `watch.yaml`;
  - `required_env: [...]`: secrets the scripts read from environment variables, such as `GITHUB_TOKEN`. `vahtovik check` verifies them.
- Secrets come from the watch's own `watches/<name>/.env`, or from the shared `.env`. A source's `env` in `watch.yaml` can fill a variable from a secret with another name.
- The body tells the agent how to collect new items and what to use as the stable artifact id.
- Scripts take the source parameters as arguments and a state folder (`--state-dir`) for snapshots or "last seen" markers, and print what is new, preferably as JSON.
- In monitoring, the state folder is the session's working folder. If the session fails, or the run stops before the new artifacts are saved, vahtovik puts the folder back as it was and monitors the source again, so no item is lost.

A source in `watch.yaml` has `scan: ai` (the default) or `scan: script`:
- `ai`: the monitoring session collects the items itself, by the skill's body.
- `script`: vahtovik runs the skill's `scan` first, and starts a monitoring session only when it finds new items. The session gets the items in its prompt and decides which are artifacts and where they go. Use it for sources whose new items a script can tell for certain, such as feeds or message lists, to read them often at little cost.

`scan` contract:
- it runs in the vahtovik project folder with a 5-minute timeout;
- `VAHTOVIK_STATE_DIR` is the source's state folder, `VAHTOVIK_PARAMS` holds the source's `params` as JSON; also `VAHTOVIK_WATCH`, `VAHTOVIK_SOURCE`, and the source's secrets as environment variables;
- it prints `{"items": [{"id": "...", ...}], "error": null}`: only the items it has not printed before for this state folder, each an object with an `id` and any other fields;
- exit code 0 and `error` null mean success. Anything else fails the monitoring, and vahtovik puts the state folder back.

## Channel skill

```
channels/<name>/SKILL.md       formatting rules for the agent
channels/<name>/send           executable; only vahtovik runs it
channels/<name>/receive        optional executable, for channels with `input`; only vahtovik runs it
```

- Front matter: `name`, `description`, and optionally `required_env: [...]`, which `vahtovik check` verifies, and `input_env: [...]`: the variables only `receive` needs, verified for channels with `input`.
- `send` contract:
  - message body on stdin;
  - subject in `VAHTOVIK_SUBJECT`; also `VAHTOVIK_WATCH`, `VAHTOVIK_GROUP`, `VAHTOVIK_CHANNEL`;
  - channel settings from `env` in `vahtovik.yaml`, and secrets from `.env`, as environment variables;
  - exit code 0 means delivered; the last line of stdout may hold a delivery identifier;
  - any other exit code is a failure, and stderr holds the reason;
  - `VAHTOVIK_DRY_RUN=1` means do not send; print what would be sent to stderr and exit 0;
  - `VAHTOVIK_PLAIN=1` means the body is plain text from vahtovik itself, such as a notice that a group waits for an answer: if the channel uses markup, escape the body;
  - `VAHTOVIK_REPLY_TO` is set when the message answers a message that `receive` read: its id. A channel that can reply to a message does so; others may ignore it. `VAHTOVIK_WATCH` and `VAHTOVIK_GROUP` are empty then.
- `send` runs in the vahtovik project folder with a 60-second timeout.
- `receive` contract:
  - `VAHTOVIK_STATE_DIR` is the channel's state folder, for "last read" markers; also `VAHTOVIK_CHANNEL`, the channel's `env` from `vahtovik.yaml`, and the secrets from `.env`;
  - it prints `{"messages": [{"id": "...", "text": "...", "from": "...", "time": "..."}], "error": null}`: only messages it has not printed before for this state folder, and only messages from people, never the ones vahtovik sent; `id` must be what `send` needs in `VAHTOVIK_REPLY_TO`;
  - exit code 0 and `error` null mean success. Anything else fails the read, and vahtovik puts the state folder back;
  - it runs in the vahtovik project folder with a 60-second timeout.
