# vahtovik skills

A skill is a folder with a `SKILL.md` file (front matter with `name` and `description`, then instructions for the agent) and optional scripts. To add a source type or a channel, add a folder; no Python changes are needed.

- `vahtovik/`: the main skill. vahtovik puts it in the system prompt of every session.
- `sources/<name>/`: how to collect items from one kind of source.
- `channels/<name>/`: how to format and send reports to one kind of channel.

## Source skill

```
sources/<name>/SKILL.md
sources/<name>/scripts/...     optional
```

- Front matter: `name`, `description`, and optionally `required_params: [...]`, which `vahtovik check` verifies against each source's `params` in `watch.yaml`.
- The body tells the agent how to collect new items and what to use as the stable artifact id.
- Scripts take the source parameters as arguments and a state folder (`--state-dir`) for snapshots or "last seen" markers, and print what is new, preferably as JSON.
- In monitoring, the state folder is the session's working folder. If the session fails, vahtovik restores the folder, so no item is lost.

## Channel skill

```
channels/<name>/SKILL.md       formatting rules for the agent
channels/<name>/send           executable; only vahtovik runs it
```

- Front matter: `name`, `description`, and optionally `required_env: [...]`, which `vahtovik check` verifies.
- `send` contract:
  - message body on stdin;
  - subject in `VAHTOVIK_SUBJECT`; also `VAHTOVIK_WATCH`, `VAHTOVIK_GROUP`, `VAHTOVIK_CHANNEL`;
  - channel settings from `env` in `vahtovik.yaml`, and secrets from `.env`, as environment variables;
  - exit code 0 means delivered; the last line of stdout may hold a delivery identifier;
  - any other exit code is a failure, and stderr holds the reason;
  - `VAHTOVIK_DRY_RUN=1` means do not send; print what would be sent to stderr and exit 0.
- `send` runs in the vahtovik project folder with a 60-second timeout.
