# TODO

## Unify the relationships between workers

Today a worker has one relationship: its parent, the worker that spawned it, and
`--report-back` makes vahtovik tell that parent how the child's turns end.

Make this one general relationship: a worker may watch a set of other workers,
and be watched by a set of other workers. Parent and child, and report-back,
become cases of it.

To decide:

- what to do when many watchers want to tell something to the worker they
  watch: the order of their messages, instructions that contradict each other,
  and whether the watched worker gets them one by one or together in one turn;
- who may start and end a watch: the watcher, the watched worker, the person;
- what a watcher is told about, and when: every turn's end, or only `finished`,
  `waiting` and `failed` as report-back does now;
- how `worker list --tree` and `worker show` present it once it is no longer
  a tree.
