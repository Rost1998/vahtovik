---
name: artifacts
description: Add artifacts that the person brings, such as links, documents or notes, to the right workers.
---

# Adding artifacts

The person brings items, such as a link, a document or a note. They want them in one worker, in several workers, or "in the workers they belong to".

1. **Know the rules.** When a poller feeds the role, its `poller.md` says what counts as an artifact and how to make its id, title and summary. Follow it as monitoring does, so the same item always gets the same id.
2. **Build each artifact:**
   - `id`: stable, as `poller.md` says; usually the canonical URL without tracking parameters;
   - `title`;
   - `summary`: a few sentences; open the link with web fetch if you need more;
   - optional: `url`, `details`, and `source_id` if the item comes from a source of a poller. Without a `source_id`, the worker sees the source as `person`.

   The schema is `src/vahtovik/schemas/artifact.json`.
3. **Choose the workers:**
   - if the person names workers, use those;
   - if they say "where it belongs", compare each item with the poller's rule for worker keys, and with each worker's Summary and Artifacts in its `context.md` (`uv run vahtovik worker list` shows what each works on). An item may belong to several workers. If none fits, propose a new worker (`skills/vahtovik/workers/SKILL.md`).

   Show the person the artifacts and the workers. Add them once they agree.
4. **Add them:**

   ```bash
   uv run vahtovik artifact add --worker <worker> [--worker <worker> ...] <<'EOF'
   [{"id": "...", "title": "...", "url": "...", "summary": "..."}]
   EOF
   ```

   `--json '[...]'` gives the same JSON without stdin. For each artifact and worker, the command prints `added` or `there already`. Each worker can have an artifact once.
5. **Tell the person when it happens.** Each worker processes its new artifacts at its next turn. The next run starts that turn, within the timer's period (1 minute by default).
   - To start the turns now, run `uv run vahtovik run --stage workers`. Ask first: it starts AI sessions.
   - If the person wants a worker to do something special with the artifacts, tell the worker in the same breath (`skills/vahtovik/tell/SKILL.md`): its next turn gets both.
