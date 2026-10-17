---
name: web-search
description: Search the web for new items on a topic. No script; the agent uses its web search and web fetch tools.
required_params: [topic]
---

# Web search

Parameters:
- `topic` (required): what to search for, in plain words.
- `sites` (optional): a list of sites to prefer or limit the search to.
- `lookback` (optional, default `7 days`): how recent items must be.

Steps:
1. Read `searches.md` in your state folder if it exists. It lists earlier queries and the items they found.
2. Run a few web searches for the topic (and sites, if given). Prefer items published within `lookback`.
3. Open promising results with web fetch when the monitoring rules need more detail.
4. Use the canonical URL of each item as the artifact id. Skip items listed in `searches.md` or in "Artifacts already registered".
5. Append today's queries and the items you returned to `searches.md` in your state folder, so the next run does not repeat them.
