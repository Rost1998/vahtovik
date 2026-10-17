---
name: web-page
description: Watch one web page and find what changed since the last run, including newly added links.
required_params: [url]
---

# Web page

Parameters:
- `url` (required): the page to watch.

Run:

```bash
python3 <skill folder>/scripts/fetch.py --url <url> --state-dir <state folder>
```

The script fetches the page, turns it into text, compares it with the snapshot from the last run in this state folder, and prints JSON:
- `first_run`: true when there was no snapshot yet;
- `added_lines`: text lines that are new since the last run (on the first run, the start of the whole page text);
- `new_links`: links (`text`, `href`) that were not on the page last time (on the first run, all links);
- `error`: a fetch problem, or null.

- New items on listing pages (news, advisories, releases) usually show up as `new_links`. Use the absolute link as the artifact id.
- For a page without links per item, use `<url>#<short slug of the change>` as the id.
- Fetch a linked page when the monitoring rules need more detail to decide.
- If `error` is not null, put it in `notes` and return no artifacts.
