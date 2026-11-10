---
name: artifacts
description: Add artifacts that the person brings, such as links, documents or notes, to the right groups of a watch.
---

# Adding artifacts

The person brings items, such as a link, a document or a note. They want them in one group, in several groups, or "in the groups they belong to".

1. **Know the watch.** Its `monitor.md` says what counts as an artifact and how to make its id, title and summary. Follow it as monitoring does, so the same item always gets the same id.
2. **Build each artifact:**
   - `id`: stable, as `monitor.md` says; usually the canonical URL without tracking parameters;
   - `title`;
   - `summary`: a few sentences; open the link with web fetch if you need more;
   - optional: `url`, `details`, and `source_id` if the item comes from one of the watch's sources. Without a `source_id`, the group sees the source as `person`.

   The schema is `src/vahtovik/schemas/artifact.json`.
3. **Choose the groups:**
   - if the person names groups, use those;
   - if they say "where it belongs", compare each item with the watch's grouping rule, and with each group's Summary and Artifacts in its `context.md`. An item may belong to several groups. If none fits, propose a new group (`skills/vahtovik/groups/SKILL.md`).

   Show the person the artifacts and the groups. Add them once they agree.
4. **Add them:**

   ```bash
   uv run vahtovik artifact add <watch> --group <key> [--group <key> ...] <<'EOF'
   [{"id": "...", "title": "...", "url": "...", "summary": "..."}]
   EOF
   ```

   `--json '[...]'` gives the same JSON without stdin. For each artifact and group, the command prints `added` or `there already`. Each group can have an artifact once.
5. **Tell the person when it happens.** Each group processes its new artifacts at its next turn. The next run starts that turn, within the timer's period (1 minute by default).
   - To start the turns now, run `uv run vahtovik run --watch <watch> --stage group`. Ask first: it starts AI sessions.
   - If the person wants a group agent to do something special with the artifacts, wait until that turn has ended (`vahtovik status`). Then tell the agent (`skills/vahtovik/tell/SKILL.md`).
