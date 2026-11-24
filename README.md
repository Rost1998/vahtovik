# vahtovik

An orchestrator of long-running AI workers on Claude Code sessions. vahtovik has three layers:

1. **knowledge**: the skills that workers know, shared by all or given to some, and the credentials those skills need;
2. **workers**: persistent Claude Code sessions, each with a runtime folder of its own and links to the skills it needs. A worker has a role, which says how it works. Workers work for a long time without a person: they process artifacts, report with their skills, schedule their own next turns, see each other, and spawn and tell other workers;
3. **monitoring**: pollers that check sources on a timer, with scripts or AI sessions, split what they find into artifacts, and give each artifact to a worker, which vahtovik spawns if it does not exist yet.

Above them, vahtovik itself has channels, such as your Telegram chat: you talk to the vahtovik agent there, and vahtovik sends you its notices. A watchdog looks after the worker sessions.

Skills are folders with `SKILL.md` and optional scripts. Adding one needs no Python changes.

## Quick start

Requirements: [uv](https://docs.astral.sh/uv/), [Claude Code](https://code.claude.com/) (`claude`), logged in, and tmux. Run `claude` once in the project folder and accept "trust this folder": worker sessions run inside it and would otherwise stop at that screen.

```bash
uv sync
uv run vahtovik check                                   # config, knowledge, roles, pollers, channels, environment
uv run vahtovik run --poller uv-releases --force --dry-run
uv run vahtovik status                                  # pollers, workers, their status and cost; -v for all details
uv run vahtovik worker list                             # what each worker does
uv run vahtovik log                                     # what vahtovik did
uv run vahtovik open uv-tracker/uv-0-13                 # talk to a worker
```

## Layout

```
knowledge/
  shared/<skill>/          every worker and monitoring session has these; shared/vahtovik/ holds the worker rules
  skills/<skill>/          specialized skills: rss, web-page, web-search, file, telegram, ...
  credentials/<name>.env   secrets, never in git
roles/<role>/              ROLE.md: what a worker of the role is and how it works; MEMORY.md: its workers' notes
pollers/<poller>/          poller.yaml: what to monitor and when; poller.md: what an artifact is, and who gets it
skills/vahtovik/           the vahtovik agent and its sub-skills
skills/watchdog/           the watchdog's strict skill
vahtovik.yaml              Claude, tmux, watchdog, usage, notices, channels
data/                      runtime state: vahtovik.db, journal.log, workers/, archive/, pollers/, channels/, runs/
```

## How a run works

`vahtovik run` does one cycle:

1. **Input**: it reads each channel with `input` whose interval has passed, and the vahtovik agent answers the new messages (see Channel-input).
2. **Monitor**: one throwaway session per due source of each enabled poller. A source is due once its `interval` has passed since it was last monitored, or, with `at`, once the first of its times of the day after that has come. A source without its own `interval` or `at` has its poller's. A source with `scan: script` is read first by its skill's `scan` script, without AI; a session starts only when the script finds new items, and gets them in its prompt. The session follows the source skill and `poller.md`, and returns new artifacts, each with the `role` and `key` of the worker that gets it. vahtovik drops artifacts the poller brought before, spawns the workers that do not exist yet, and saves the artifacts.
3. **Workers**: for each worker with new artifacts, messages in its inbox, or a scheduled turn that is due, vahtovik resumes the worker's session in a tmux window and goes on: it does not wait for the turn to end. The agent works by its role's rules, updates `context.md`, reports with its skills, and ends the turn with its status (see below). At most `claude.parallel_turns` (5) turns work at once; turns that wait for their background work or for a person do not count. The rest start at a later run: every run starts the turns that can start, even when no source is due. A worker whose window is open gets its messages typed in instead, when its agent is idle.
4. **Deliver**: vahtovik's own failed messages are retried, up to 5 attempts.

Before these steps, a run lets the watchdog look after the worker sessions (see Watchdog), and checks how full the Claude plan's usage limits are (see Usage limits).

## The knowledge layer

`knowledge/README.md` gives the details and the contracts of the scripts.

- **Shared skills**, `knowledge/shared/<skill>/`: every worker and every monitoring session has them. `knowledge/shared/vahtovik/SKILL.md` holds the rules of every worker; vahtovik also puts it in their system prompt. Claude Code keeps a session's system prompt as it was when the session was created, so when you change these rules, vahtovik tells each worker that exists already, in its next turn, to read the file again.
- **Specialized skills**, `knowledge/skills/<skill>/`: only the sessions that name them have them:
  - a role names skills for all its workers;
  - `vahtovik worker spawn --skill` gives one worker more;
  - a poller source names the skill that reads it;
  - a channel of vahtovik names the skill whose `send` and `receive` scripts it uses.

  One skill can serve several of these: the `telegram` skill is how workers report to a chat, and also how vahtovik talks to you in yours.
- **Credentials**, `knowledge/credentials/<name>.env`: `KEY=value` lines, ignored by git. A skill names the files it needs (`credentials: [telegram]`); a role, a poller source or a channel can name more, and later files win. A session gets the variables of its files in its environment. `knowledge/credentials/vahtovik.env` holds vahtovik's own secrets, such as `ANTHROPIC_API_KEY`; vahtovik loads it at startup, and values already set in the environment win.

Each session's skills are links in its folder, `.claude/skills/<skill>`, where Claude Code finds them and loads them as its own: it shows the agent their descriptions, and the whole skill when the agent uses it. vahtovik makes the links again before every start of a session, so a new or changed skill counts from the next turn. It also lists each skill's name, description and path in the worker's `CLAUDE.md` and prompts, so the skills work in the api-key login too, where Claude Code's bare mode limits loading skills.

## Roles

A role says what a worker is and how it works: `roles/<role>/ROLE.md`.

```markdown
---
description: Follows one minor version line of uv. Assesses each new release and reports it.
skills: [web-page, file]       # specialized skills; the shared ones come by themselves
credentials: [telegram-ops]    # optional: more credential files
model: opus                    # optional: the model and effort of its workers
effort: high
account: work                  # optional: a Claude account of claude.accounts
---

# Processing ...
# Reporting ...
# How to end a turn ...
```

The body is the role's working rules: how to process artifacts and messages, what to report, when, where and with which skill, and when the work is done. vahtovik pastes it into every turn's prompt, so keep it short: a rule says in a few lines what to do and when, and refers to a skill by name for the long instructions and scripts. Changed rules count from each worker's next turn. See `roles/uv-tracker/`.

### Role memory

Each role can have a `MEMORY.md` next to its `ROLE.md`: notes that hold for all its workers, which workers found out themselves or learned from you. For example: how a source behaves, which kinds of artifacts turn out to be false alarms, or what you said you prefer. Workers save notes with `vahtovik memory add` and remove wrong or stale ones with `vahtovik memory remove`; the vahtovik agent does too when you ask. Every turn of the role's workers, and every monitoring session of a poller with the role, gets the file in its prompt. The notes are not rules: when they disagree with the role's rules, the rules win.

```markdown
## 3 · 2026-11-11 14:03 · worker a3f9c2-uv-0-13

The changelog feed repeats an entry when a release is edited: its id stays the same.
```

Each entry's heading has its number, its time and who saved it. The command refuses a text that holds a secret from a credentials file, and a worker may change only its own role's memory. The journal keeps a line for every entry saved or removed. vahtovik never commits the file: review the new notes with `git diff`, edit or delete them by hand if you like, and commit them.

## Workers

Each worker has one Claude Code session, created on its first turn and resumed on every later turn. A worker has:

- an **id**: 6 random hexadecimal characters, such as `a3f9c2`, unique in the installation and never used again;
- a **key**: lowercase words joined by hyphens, such as `uv-0-13`, unique within its role.

Its name is `<id>-<key>`. Every command takes a worker as `a3f9c2`, `a3f9c2-uv-0-13` or `uv-tracker/uv-0-13`. Its folder is `data/workers/<role>/<id>-<key>/`:

- `.claude/skills/`: links to its skills: the shared ones, its role's, and the extra ones of its spawn;
- `CLAUDE.md`: regenerated by vahtovik before every start; tells the agent who it is, its role, its parent, its skills, the roles of other workers, its scheduled turn, and the commands;
- `context.md`: Summary and Work log (written by the agent), Open questions and Artifacts (written by vahtovik);
- `artifacts/*.json`: one file per artifact.

Its record in the database says who spawned it (`poller:<name>`, `worker:<id>`, `person` or `channel:<name>`), its parent (the worker that spawned it), and whether it reports back to that parent.

Worker sessions run as interactive Claude Code, each in its own window of the tmux session `vahtovik-agents`, named `<role>/<key>`. The session is on a tmux server of its own, `tmux -L vahtovik`, apart from your own tmux sessions; both names are set in `vahtovik.yaml`, and `socket: null` uses your default tmux server instead. The first window shows the journal. To look around yourself: `tmux -L vahtovik attach -t vahtovik-agents`.

- `vahtovik open <worker>` shows you the worker's window: a running turn, or the session started fresh if none is running. You see every earlier turn and can ask the agent anything: explain, research more, scan sources again, add artifacts, spawn workers, or send extra reports. It attaches your terminal to the window. Inside your own tmux this is tmux inside tmux, and Ctrl-b goes to your outer tmux: press Ctrl-b Ctrl-b d to leave the inner one. (With `socket: null`, inside tmux it switches your window instead.)
- Leave whenever you like: detach, or switch back to your own window. The agent keeps working.
- While the window is open, it holds the worker's lock, and runs skip that worker.
- The watchdog (below) closes a window when nobody is looking at it and its agent is done: `failed`, or `finished`, `scheduled` or `waiting` once its AI check confirms that the work is really done.
- `--new-session` starts a fresh session that rebuilds its picture from `context.md`.

If vahtovik's tmux server is not running, vahtovik starts it with `systemd-run --user`, so the server outlives the run that started it.

### Status and scheduling

Every worker has a status, shown by `vahtovik status` and `vahtovik worker list`. The agent ends every automated turn, and every reply to a person or a message, with one command, and gives the status that is true:

- `vahtovik turn finished`: its work is really done. This cancels its scheduled turn, if it has one, and the worker gets no more turns by itself: only new artifacts, a message, or you opening it start it again;
- `vahtovik turn scheduled --after 6h [--reason "..."]`, or `--at 08:00` (its next occurrence, local time) or `--at "2026-12-01 09:00"`: it needs nothing from a person now, but another turn later, even without new artifacts, for example to see whether something announced has happened. The worker is `scheduled` until that turn starts. A plain `vahtovik turn scheduled`, without `--after` and `--at`, keeps the turn it has scheduled already, as after a reply to you while its check still waits; without one, the command refuses;
- `vahtovik turn waiting --question "..."`: it needs a person's answer or action to go on. The questions go to the Open questions section of `context.md`, and the watchdog sends you a notice. Answer them in `vahtovik open`, or with `vahtovik tell`. A scheduled turn stays for after the answer;
- `vahtovik turn background --reason "..." [--check-after 30m]`: it started background work (a background command, a background agent or a scheduled prompt) and goes on when that work ends. Claude Code wakes the agent then, and it ends the turn as usual. Meanwhile the window stays open and keeps the worker's lock, and the turn stays open.

When the agent stops, Claude Code tells vahtovik what background work it still runs for the agent. If the agent said `finished`, `scheduled` or `waiting` while such work runs, vahtovik makes it `background`, and the turn stays open. If it said `background` while nothing runs that would wake it, vahtovik sends it back to work.

A Claude Code hook does not let the agent stop before it runs the command. An automated turn counts as done only then; otherwise it is retried, up to 3 attempts. The statuses are `idle`, `running`, `finished`, `scheduled`, `waiting`, `background`, `maintenance` (set by the watchdog, below) and `failed`.

A run starts a scheduled turn once its time comes, and the agent gets the reason in its prompt. New artifacts or messages give the worker a turn earlier, and its scheduled turn stays. A worker has one scheduled turn at most, and nothing repeats by itself: a worker that checks something again and again ends each of those turns with `scheduled` again, as its role's rules say. A failed scheduled turn is tried again at the next runs, up to 3 attempts. You or the vahtovik agent can schedule a worker too: `vahtovik worker schedule <worker> --after 6h|--at 08:00|--off`.

### The inbox

`vahtovik tell <worker>` and `vahtovik worker spawn --message` put a message into the worker's inbox. If the worker's window is open, its agent is idle and nobody is looking at it, vahtovik types the message in at once. Otherwise the next run starts a turn with it, together with new artifacts and a scheduled turn that is due; that turn counts toward `parallel_turns`. The agent gets each message with who sent it: "Message from the person, sent through vahtovik:" or "Message from worker a3f9c2-uv-0-13 (your parent), sent through vahtovik:". A message whose turn fails is tried again in the next turn, up to 3 attempts. vahtovik types nothing into a window that started less than 20 seconds ago, and no message longer than 4000 characters: those wait for a later run, or for the worker's next turn. A turn's prompt that is too long for a program argument goes to the file `turn-prompt.md` in the worker's folder, and the agent is told to read it.

### Workers and other workers

Workers see each other and work together:

- `vahtovik worker list [--role R] [--status S] [--tree] [--all]` shows every worker: its status, who spawned it, and what it does: the summary of its last turn, or its task before its first turn has ended. `--tree` shows children under their parents, and `--all` includes archived workers. `vahtovik worker show <worker>` shows one worker in full, with its children, its artifacts and messages, and the path of its `context.md`, which any worker may read.
- A worker spawns a worker with `vahtovik worker spawn <role>/<key> --message "<the task>"`, and becomes its parent. With `--report-back`, vahtovik tells the parent each time the child ends a turn as `finished` or `waiting`, or fails and vahtovik stops retrying: the child's status and summary go into the parent's inbox, and the parent gets a turn for them.
- A worker tells another worker something with `vahtovik tell <worker>`, and gives it artifacts with `vahtovik artifact add --worker <worker>`.

Nothing limits how many workers a worker may spawn: `parallel_turns` limits only how many work at once. Messages between workers are instructions, so text that misleads one worker can spread to the workers it tells or spawns.

### Reporting

vahtovik does not report for workers. A worker reports as its role's rules say, with its skills' scripts: for example the `telegram` skill's `send` sends a message to the chat in `knowledge/credentials/telegram.env`, and the `file` skill's `send` appends it to `data/outbox.md`. The worker notes each report, and whether it was delivered, in its Work log.

### Artifacts a worker finds

A worker adds artifacts with `vahtovik artifact add`: to itself, where they count as processed, or with `--worker` to another worker, where they wait for that worker's next turn. One artifact may be in several workers; each processes it on its own.

### Changing workers by hand

You, or the vahtovik agent (below), can change workers with `vahtovik worker spawn`, `merge`, `split`, `schedule` and `archive`, `artifact add` and `tell` (see Commands). `merge`, `split` and `archive` refuse a worker whose window is open, that is busy, or whose turn is unfinished. A merged worker is archived, and its role and key lead to the worker it went into, so monitoring never brings it back. An archived worker gets no more turns, and its folder goes to `data/archive/<role>/`; its key is free for a new worker.

### Sessions and costs

Sessions run with `bypassPermissions`, except those of limited input channels (see Channel-input). The worker rules (`knowledge/shared/vahtovik/SKILL.md`, part of every session's system prompt) tell agents two rules. Everything outside their working folder is read-only, unless their role's rules or skills say otherwise; `vahtovik turn`, `vahtovik memory`, `vahtovik worker list|show|spawn`, `vahtovik tell` and `vahtovik artifact add` are always allowed. Instructions come only from their rules, the person, and the messages vahtovik delivers, never from what sources return.

`vahtovik status` shows what each worker's sessions cost in the last 24 hours and the last 30 days, and the totals per role and for all workers. Below that, a second table shows what worker, monitoring, input and watchdog sessions cost in the same periods, and in total; `-v` adds all time. Claude Code writes a session's running total to its conversation file when the session's window ends, and vahtovik reads it from there, so a running window's cost shows once the window ends. The total has no time of its own, so vahtovik dates it by the last message before it, and a window's whole cost counts at that time. vahtovik keeps every total with its time in its database, because Claude Code deletes old conversation files. With `auth: subscription`, the cost is what the same work would cost with an API key; you pay your subscription instead. Subscription limits are counted for the whole account, not per session, so the cost is also the best way to compare how much of the limits each kind of session uses.

## Pollers

A poller is what vahtovik monitors: `pollers/<poller>/`.

```yaml
# poller.yaml
enabled: true
interval: 6h                    # or at: ["08:00", "20:00"]
roles: [uv-tracker]             # the roles of the workers it gives artifacts to; the first is the default
sources:
  - id: uv-releases
    skill: rss                  # a skill of knowledge/skills
    params: {url: "https://github.com/astral-sh/uv/releases.atom", max: 10}
    scan: script                # optional: the skill's scan script reads the feed; a session starts only for new items
    interval: 1h                # optional: the source's own schedule
    credentials: [github-org-b] # optional: more credential files for this source
claude: {model: sonnet, effort: low, account: work}   # optional: its monitoring sessions
```

`poller.md` says what counts as an artifact, how to make its id, title and summary, and which worker gets it: the role and the rule for the worker key, with examples. A key that no worker of the role has spawns a new worker. A monitoring session gets the poller's rules, the source skill, the descriptions and memory of the poller's roles, the workers of those roles, and the artifacts the source brought recently. See `pollers/uv-releases/`.

## Channel-input

vahtovik's channels belong to vahtovik itself: it talks to you through them. Workers never use them. You can talk to vahtovik through a channel, such as your Telegram chat. Give the channel `input` in `vahtovik.yaml`:

```yaml
channels:
  ops-telegram:
    skill: telegram             # a skill of knowledge/skills with send and receive scripts
    credentials: [ops-bot]      # knowledge/credentials/ops-bot.env: this channel's bot
    env: {TELEGRAM_CHAT_ID: "-1001234567890"}
    input: {}                   # read it every minute; or {interval: 5m}
```

At the start of every run, after the watchdog, vahtovik reads each such channel whose interval has passed, with its skill's `receive` script (contract in [knowledge/README.md](knowledge/README.md)); no AI runs for that. When there are new messages, vahtovik resumes the channel's session: the vahtovik agent (see The vahtovik agent), in the project folder, with Opus and `xhigh` effort by default (`claude.stages.input`). One session serves the channel while you write, so it remembers the conversation. After a quiet pause, the next message starts a new session, because resuming is cheaper only while Claude Code's cache still holds the conversation; then a long old session costs more than a new one. The new session gets your last few messages, with its replies, in its prompt, and reads more with `vahtovik messages` when it needs them. The session stays, however long the pause, while the agent's last reply waits for your answer, such as a plan that waits for "yes", and while it tries a message again. A new session also starts if the old one's conversation file is gone. To start over yourself, ask in the channel to clear the conversation: the agent answers, and your next message starts a new session, without the earlier messages in its prompt.

```yaml
    input:
      new_session_after: 1h     # the default; 0: one session until you clear it
      history: 5                # how many earlier messages a new session gets in its prompt (the default)
```

- Your messages are your own words, as in the terminal. The agent answers questions at once. Before a change, or anything that costs money, it replies with its plan, and acts once you answer "yes" in the channel.
- When it needs to work before it answers, it first sends one short line with `vahtovik acknowledge`, at once: that your messages arrived, and what it does now, such as "Let me check." or "Doing it now.". It sends that in the same step as its first tool call, so it adds less than 1% to the answer's cost. vahtovik adds the session's context size at the end, in thousands of tokens, such as "Let me check. (22k)". A new session starts at about 20 thousand, so a larger number means the old session goes on, and each of its steps costs more. One line goes out for each batch of messages, and vahtovik does not retry it if it fails. `vahtovik messages` leaves these lines out. To turn them off, say so in the channel's `rules` (see Limited channels).
- It returns one reply for each message, and vahtovik sends the replies through the same channel, as answers to your messages. A message without a reply counts as a failed attempt; after 3, vahtovik replies itself that it could not handle the message.
- Replies that only keep the conversation going (plans to confirm, questions, "done") are one to three short lines. Only the information you asked for gets more room.
- It works inside the run, and the run waits for it. What it changes counts in the same run: workers it spawns or tells, and artifacts it adds, get their turns. It passes long work to workers.
- vahtovik keeps your messages and its replies. `vahtovik messages <channel>` shows them, and the agent reads them the same way when you refer to something its session does not know.
- Anyone who can post in the channel's chat can give vahtovik orders, unless you limit the channel (below).

The `file` and `telegram` skills can read; see their skills. `vahtovik status` shows each input channel: when a new session starts, when it was read last, and its messages; `-v` also shows its limits. Each input channel has a session of its own. Two Telegram input channels need two bots, because only one reader may take a bot's updates: give each its own credentials.

### Limited channels

A channel that other people can post in can get limits in its `input`:

```yaml
channels:
  team-telegram:
    skill: telegram
    credentials: [team-bot]
    input:
      senders: ["@alice", "id:123456"]   # default: anyone in the chat
      commands: [status, log, tell]      # default: everything, as in the terminal
      roles: [kernel-cve]                # default: all roles
      rules: |                           # optional: your own rules for the channel's agent
        Answer only about kernel CVEs.
```

- `senders`: vahtovik stores only the messages from these senders, as the skill's `receive` names them (Telegram: `@username`, or `id:<user id>`). It ignores the other messages, without a reply, and writes a journal line for each.
- `rules`: they go into the agent's prompt with every message. They work in a full channel too.
- `commands` (some of `status`, `log`, `tell`, `artifact`, `worker`) and `roles` make the channel **limited**; `roles` alone allows all five commands. A limited channel's agent may read only vahtovik's skills, the knowledge layer without its credentials, `README.md`, `CLAUDE.md`, the schemas, and its roles' folders, pollers and workers, never a credentials or `.env` file. It may run only its commands, each alone, as `vahtovik <command> ...`, `vahtovik messages` for its own channel and `vahtovik acknowledge`, and it can change no file, fetch no web page and use no other tool. With `roles`, the commands it runs see only the workers of those roles, and `vahtovik log` shows only their lines and the channel's own.

The agent knows its limits from its prompt, and Claude Code enforces them: a limited channel's session runs with `--permission-mode dontAsk`, only the Read, Grep, Glob and Bash tools, no MCP servers, and vahtovik's guard (`src/vahtovik/channel_guard.py`) as its PreToolUse hook. The guard allows or denies every tool call, and writes a journal line for each call it denies. All other sessions keep `bypassPermissions`: full channels, worker, monitoring and watchdog sessions, and the `claude` you run yourself.

`tell`, `artifact` and `worker` start workers, and these work by their role's full rules. Allow these commands only to people you would let ask a worker for anything that its role's rules allow.

### Notices

`notices: [channels]` in `vahtovik.yaml` says where vahtovik sends its notices: the watchdog's (below) and the usage warnings. Without it, they go to every input channel that is not limited, your own chats. Failed notices are retried, up to 5 attempts.

## Watchdog

The watchdog looks after the worker sessions at the start of a run, once `watchdog.every` (5 minutes) has passed since it last did. Then:

- it closes the windows of failed workers that nobody looks at;
- it fails the turns whose window closed before the agent reported its status, so they are retried;
- it sends a notice about each worker that waits for your answer, needs attention, or failed after its last retry. Notices repeat every `watchdog.notify_every` (6 hours; 0 never repeats) until the status changes, and stop while you look at the worker's window. Every notice's subject starts with `WATCHDOG:`;
- it warns once about a worker turn that runs longer than `watchdog.long_turn_after` (3 hours), since sessions have no time limit;
- when vahtovik stops retrying a reply to you (after 5 failed attempts), it tells the other notice channels, with the reply itself.

It also asks an AI session (the `watchdog` stage: Sonnet with medium effort by default) to check the worker sessions: every `running` or `background` one, and every other one that changed since the last check: with an open window, or `finished`, `scheduled` or `waiting` with a window that ended before a check. It skips the call when there is nothing to check, and the cost shows in `vahtovik status`. The AI gets the facts of each session (status, turn, window, lock, the screen and the end of the conversation), follows the strict skill `skills/watchdog/SKILL.md`, and picks one action per session from a fixed list. vahtovik checks each pick before it acts:

- `none`: all is right. A done window (`finished`, `scheduled` or `waiting`) closes only after this answer;
- `press`: press the keys of a known screen, such as Claude Code's "trust this folder" question. The screens and their keys are listed in the skill, and vahtovik presses the keys, never the AI;
- `nudge`: the agent is idle in its window but its work is not done, for example its background work hung. vahtovik types the skill's fixed message that tells the agent to check its work and report its status. Only to an idle agent;
- `resume`: the window is gone but the work is unfinished. vahtovik opens the session again with the skill's fixed message to finish where it stopped. Only when the worker has no window and no lock;
- `retry`: the window is gone and the latest turn's work is lost: vahtovik counts the turn as failed, and the next run repeats it. Only when the worker has no window and no lock;
- `maintenance`: the session needs a person, for example to log in again. It keeps its window and its lock until you fix it: open it, fix the problem, and type "continue";
- `failed`: the session cannot go on; the turn is retried.

The AI never writes text for a session, and vahtovik never touches a window that a person is looking at. Every action except `none` also sends a notice, so you can follow what the watchdog does while it is new. For `maintenance` this is the usual "Needs attention" notice, sent at once.

## Usage limits

With `auth: subscription`, all vahtovik sessions use your Claude plan's usage limits, and when a limit is full, the sessions stop until it resets. Claude plans have a 5-hour limit, a weekly limit for all models, and weekly limits for single models, such as Fable. vahtovik warns you before a limit is full.

At the start of a run, once `usage.every` (10 minutes; 0: never) has passed since the last check, vahtovik asks Claude Code how full the limits are: the numbers that Claude Code's `/usage` shows. Claude Code answers without an AI session, so the check costs nothing. It uses its own login, so vahtovik never reads your login token.

Each limit gets one warning at each percent of `usage.warn_at` (80, 90, 95 and 99) that it passes, until it resets; then it warns again. A limit that passes several of them between two checks gets one warning, at the highest. One message holds the warnings of one check, with the time each limit resets:

```
Claude usage
The 5-hour limit is at 91% (past 90%). It resets today at 18:20, in 3 hours 11 minutes.
```

The warnings go to the `notices` channels. `vahtovik status` shows the limits at the last check, and the error if the check fails now; the journal has a line for each warning, and one line when the check starts to fail.

Each account of `claude.accounts` (see Claude accounts) has limits of its own. vahtovik checks the default login and each account on its own, and an account's warnings say which account they are about, as in the subject "Claude usage, account work".

## Claude accounts

By default, all vahtovik sessions use one Claude login: Claude Code's own profile, or the one in `claude.config_dir`. A role or a poller can use another Claude account instead, so that work spreads over the usage limits of several plans. Each account is a Claude Code profile folder of its own (`CLAUDE_CONFIG_DIR`), with its own login, which Claude Code keeps and refreshes. vahtovik never reads the login.

```yaml
# vahtovik.yaml
claude:
  accounts:
    work: {config_dir: ~/.claude-work}

# roles/<role>/ROLE.md front matter, for its workers
account: work

# pollers/<poller>/poller.yaml, for its monitoring sessions
claude: {account: work}
```

Set up an account once: run `CLAUDE_CONFIG_DIR=~/.claude-work claude` in the project folder, log in with the account, and accept "trust this folder". `vahtovik check` shows who each login is logged in as, and reports a profile that is not logged in or does not trust the project folder.

- A role's workers use its account, in their turns, in `vahtovik open` and when the watchdog resumes them.
- Channel-input and watchdog sessions use the default login.
- Claude Code keeps a session's conversation in the profile it ran with. A role that moves to another account therefore starts new sessions for its workers: each agent rebuilds its picture from `context.md`, as after a lost session. The costs of the old sessions still count.
- Accounts work only with `auth: subscription`.
- Claude Code's personal settings, such as `settings.json`, belong to each profile. vahtovik keeps each profile's `CLAUDE.md` out of its sessions, as it does with `~/.claude/CLAUDE.md`.

## Journal

vahtovik writes one short line per key event to `data/journal.log`: runs, messages from input channels and their answers, monitoring, new workers, turns, statuses, messages to workers, interactive sessions and failures.

```
2026-10-17 14:03:12 run 20261017-140312 Started run: uv-releases
2026-10-17 14:03:12 run 20261017-140312 uv-releases/uv-releases: Started monitoring (rss)
2026-10-17 14:03:40 run 20261017-140312 uv-tracker/a3f9c2-uv-0-13: Created worker "uv 0.13.x", spawned by poller uv-releases
2026-10-17 14:03:40 run 20261017-140312 uv-releases/uv-releases: Finished monitoring in 28s: 3 found, 2 new, $0.04
2026-10-17 14:03:41 run 20261017-140312 uv-tracker/a3f9c2-uv-0-13: Started turn: 2 artifacts, new session 1b2c3d4e, tmux window @3
2026-10-17 14:03:41 run 20261017-140312 Finished run in 29s: 2 new artifacts, 1 worker turn started, $0.04
2026-10-17 14:05:10 run 20261017-140312 uv-tracker/a3f9c2-uv-0-13: Status finished, importance high: uv 0.13 ...
2026-10-17 15:20:01 interactive         uv-tracker/a3f9c2-uv-0-13: Started interactive session, resumed session 1b2c3d4e
```

The second column says who wrote the line: a timer run, an `interactive` session, or a `manual` command. A run does not wait for its turns, so the lines of a turn's agent come after the run's last line, with the run's id. Problems start with "Failed" or "Skipped". A run with nothing due writes one line, such as `Nothing due: uv-releases in 4h12m` (the time until the poller's next source is due), so you can see the timer works.

- `vahtovik log -f`: watch what happens now.
- `vahtovik log a3f9c2`: one worker's history. Any text works as a filter: a role, a poller, a run, a date.
- `vahtovik log -n 0 | less`: the whole journal.

`vahtovik run` in a terminal also prints the lines as it goes. Workers know where the journal is, so you can ask them about it too.

## Configuration

- `vahtovik.yaml`: Claude login mode (`subscription` or `api-key`), the Claude accounts that roles and pollers can use (`claude.accounts`, see Claude accounts), the default model and effort (`opus`, the newest Opus, and `high`), model, effort and dollar cap per stage (`monitor`, `worker`, `watchdog`, `input`), how many worker turns work at once (`claude.parallel_turns`), the watchdog's pace and notices, the warnings about the Claude plan's usage limits (`usage`), where notices go (`notices`), and vahtovik's channels. A role sets the model and effort of its workers, and a poller those of its monitoring sessions; they win over `vahtovik.yaml`'s. The watchdog and input channels belong to neither, so only `vahtovik.yaml` sets them.
- `knowledge/`, `roles/`, `pollers/`: see above. `paths: {data, knowledge, skills, roles, pollers}` in `vahtovik.yaml` can move these folders.
- `knowledge/credentials/*.env`: all secrets; see The knowledge layer.

vahtovik keeps your personal `~/.claude/CLAUDE.md` and parent-folder `CLAUDE.md` files out of its sessions. Set `claude.config_dir` to give vahtovik a separate Claude Code profile, and `claude.accounts` to give a role or a poller an account of its own.

vahtovik 2 starts fresh: it refuses a database of vahtovik 1. Move the old `data/` folder away, and write the old watches as roles and pollers; the vahtovik agent can help.

## The vahtovik agent

Run `claude` in this folder, and you talk to the vahtovik agent: the agent that runs this installation for you. You can also talk to it through a channel (see Channel-input). Ask it in plain words, for example:
- "watch our dependencies for new CVEs, and give each CVE a worker that triages it";
- "merge the uv 0.9 and uv 0.10 workers";
- "add this link to the workers it belongs to";
- "ask every uv-tracker worker whether it still needs an answer from me".

It knows vahtovik's parts and commands, and it reads the files of a role, a poller or a worker before it works on it. It tells you its plan before it changes anything. It changes runtime state only with `vahtovik` commands, never by editing `data/`.

The root `CLAUDE.md` makes it the vahtovik agent, and loads its skill, [skills/vahtovik/SKILL.md](skills/vahtovik/SKILL.md). That skill names a sub-skill for each kind of task:
- `knowledge/`: add skills and credentials;
- `roles/`: set up and change roles;
- `pollers/`: set up and change pollers;
- `workers/`: spawn, merge, split, schedule and archive workers;
- `artifacts/`: add artifacts to workers;
- `tell/`: pass messages to workers;
- `channel/`: answer the person through a channel with `input`.

The sessions that vahtovik starts never load the root `CLAUDE.md`: monitoring, worker and watchdog sessions keep parent-folder `CLAUDE.md` files out (see Configuration). Your own sessions in this folder load it, including the ones where you work on vahtovik's code; ask the agent for code changes, and it makes them.

## Commands

A worker is `<id>`, `<id>-<key>` or `<role>/<key>`.

| Command | What it does |
|---|---|
| `run [--poller P] [--stage input\|monitor\|workers\|deliver] [--force] [--dry-run]` | one cycle; `--force` reads every input channel and monitors every source, whatever their schedules; `--poller` monitors only that poller, starts only the turns of its roles' workers, and leaves the input channels out; `--dry-run` makes vahtovik's channels, and the `send` scripts that the run's workers report with, print instead of send |
| `open WORKER [--new-session]` | show a worker's session window, starting the session if it is not running |
| `turn finished\|scheduled\|waiting\|background [--importance I] [--summary S] [--question Q ...] [--reason R] [--after 6h] [--at 08:00] [--check-after 30m]` | for agents: end a turn with the worker's status |
| `worker spawn ROLE/KEY [--title T] [--skill S ...] [--message M] [--report-back]` | spawn a worker; inside a worker, that worker is its parent; with a message, it starts with it at the next run |
| `worker list [--role R] [--status S] [--tree] [--all]` | the workers: status, who spawned them, and what they do |
| `worker show WORKER` | all about one worker: status, schedule, parent, children, artifacts, messages, latest turn, folder |
| `worker merge B [C ...] --into A` | merge workers into worker A, which keeps its role: their artifacts, turns, costs and waiting messages move to A, their record goes into A's Work log, they are archived, and their keys lead to A from then on |
| `worker split A --artifact ID [--artifact ID ...] --into C [--title T]` | move artifacts of worker A to worker C, existing or new as `<role>/<key>`; they wait for C's next turn |
| `worker schedule WORKER --after 6h\|--at 08:00\|--off [--reason R]` | schedule a worker's next turn, or cancel it |
| `worker archive WORKER --reason R` | no more turns; its folder goes to `data/archive/<role>/`, and lists leave it out |
| `artifact add [--worker W ...] [--json TEXT]` | add artifacts, as JSON on stdin or in `--json` (one object or a list, by `src/vahtovik/schemas/artifact.json`), to each worker named; inside a worker, to itself by default, as processed |
| `tell WORKER [WORKER ...] [--message M]` | pass a message (default: stdin) to workers' inboxes: typed into an open idle window, or in their next turn |
| `memory add [--role R] [--text T]` | add a note (default: stdin) to the role's `MEMORY.md`; inside a worker, the role comes from the environment |
| `memory remove NUMBER --reason R [--role R]` | remove an entry from the role's `MEMORY.md` |
| `messages [C] [-n N]` | an input channel's last N messages (default 20, 0 for all), with vahtovik's replies, oldest first; inside a channel session, the channel comes from the environment |
| `acknowledge --text T [--to ID]` | for channel agents: send one short line at once that the person's messages arrived, with the session's context size added, such as "(22k)"; by default in reply to the last waiting message; once per batch, never retried |
| `log [TEXT] [-n N] [-f]` | the last N journal lines (default 40, 0 for all) that contain TEXT; `-f` keeps showing new lines |
| `status [--role R] [-v]` | a table of the pollers, with their schedules and the time left until a run monitors their next source; a table of the workers by role: status, parent, processed artifacts and cost in the last 24 hours and 30 days, the totals per role and for all, and undelivered messages; then the input channels and their messages; the usage limits; and the cost of worker, monitoring, input and watchdog sessions in the same periods, and in total. `-v` gives all details as text instead |
| `install-timer [--every 1m]` | start vahtovik periodically with a systemd user timer |
| `uninstall-timer` | remove that timer |
| `check` | validate the config, the knowledge layer, the skills and credentials of roles, sources and channels, `claude`, who each Claude login is logged in as, their trust of the project folder, and tmux |
| `test-channel C [--dry-run]` | send a test message through one of vahtovik's channels, no AI |

## Scheduling

vahtovik has no background process. A timer starts one cycle at a fixed period, every minute by default. Each source is monitored only when it is due by its schedule, and the watchdog runs only every `watchdog.every` (5 minutes), but every cycle starts the worker turns that wait. Nothing runs more often than the timer fires, so keep the period at 1 minute, the default.

A poller, or a source, has one of two schedules in `poller.yaml`:

```yaml
interval: 6h                    # every 6 hours after the last monitoring: 30m, 6h, 1d
at: ["08:00", "20:00"]          # or: at these times of the day, in the computer's local time; one time or a list
```

Not both at once. A source without its own `interval` or `at` has its poller's; a poller with neither has `interval: 1h`. With `at`, a source is due at the first of its times after it was last monitored. So a time missed while the computer was off is made up once, at the first run after it is back. A source that was never monitored is due at once, with either schedule. Write the times in quotes: YAML reads an unquoted `20:00` as a number, which vahtovik turns back into the time, but quotes make it plain.

Runs never overlap. Turns go on in their windows after the run that started them, and have no time limit. A run that starts while another is still going does nothing and writes a line such as `Skipped run: run 20261020-140312 has been going for 47m00s`. Sources that are due stay due, so the first run after the long one picks them up. The systemd timer does not even start a run while one is going, so these fires leave no line.

`vahtovik install-timer [--every 1m]` sets up a systemd user timer and starts it. The period must divide an hour or a day: 1m, 5m, 15m, 30m, 1h, 6h, 1d. Run it again to change the period. `vahtovik uninstall-timer` removes the timer.

- `systemctl --user list-timers vahtovik.timer` shows the next run.
- `journalctl --user -u vahtovik` shows anything a run prints, such as a crash trace.
- A run missed while the computer was off or asleep happens when it is back.
- The timer runs only while you are logged in, unless you run `loginctl enable-linger $USER` once.
- The timer uses the `PATH` of the shell you installed it from, so it finds `uv` and `claude`. If you move them, install the timer again.

Without systemd, use cron with `bin/vahtovik-cron`. It writes anything a run prints to `data/logs/<date>.log`.

```cron
* * * * * /path/to/vahtovik/bin/vahtovik-cron
```

## Development

```bash
uv run pytest
```

Tests use a fake `claude` command (`tests/fake_claude.py`), so they make no AI calls.
