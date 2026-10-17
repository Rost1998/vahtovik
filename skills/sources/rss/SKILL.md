---
name: rss
description: Collect new entries from an RSS or Atom feed.
required_params: [url]
---

# RSS / Atom feed

Parameters:
- `url` (required): the feed URL.
- `max` (optional, default 50): the most entries to return in one run.

Run:

```bash
python3 <skill folder>/scripts/fetch.py --url <url> --state-dir <state folder> [--max <max>]
```

The script prints JSON: `{"feed": ..., "new": [{"id", "title", "link", "published", "summary"}], "error": null}`. It lists only entries it has not printed before for this state folder, newest first. If more than `max` entries are new (for example on the first run), only the newest `max` are listed and the rest are skipped.

- Use the entry's `id` as the artifact id, or its `link` when the id is empty.
- Entry summaries are often short. Fetch the entry's link when the monitoring rules need more detail to decide.
- If `error` is not null, put it in `notes` and return no artifacts.
