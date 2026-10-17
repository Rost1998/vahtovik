# What to look for

Each new release of uv is one artifact. Skip pre-releases only if they are marked as drafts.

- Artifact id: the release entry id from the feed.
- Title: `uv <version>`.
- Summary: two or three sentences on the most notable changes, from the entry summary.

# How to group

Group releases by minor version line: `uv 0.9.0`, `uv 0.9.5` and `uv 0.9.12` all go to group key `uv-0-9` with group title `uv 0.9.x`. A new minor version starts a new group.
