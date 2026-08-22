"""Sinks — apply events to watcher-owned state files.

Each sink is a stateless module exposing one top-level function (or a
small class) that takes an explicit state path plus events/state and
rewrites exactly one owned file. Most outputs live under project
``.xo/``; ephemeral presence lives under ``~/.quirq/watcher/``.
State across ticks lives in the files themselves; the watcher re-reads
on each call. This makes the sinks restart-safe and trivially testable.

Files owned by each sink:

* :mod:`project_json`     → ``project.json``         (one-shot identity fill)
* :mod:`sessions_augment` → ``sessions/sessions-augment.json``
* :mod:`todos`            → ``todos.json``
* :mod:`activity`         → ``~/.quirq/watcher/activity/projects/<id>.json``
* :mod:`stats`            → ``stats.json``
* :mod:`timeline`         → ``timeline.jsonl``  (append-only, rotated)

The adapter-owned ``sessions/sessionslist.json`` is NOT in this list
— see docs/watcher-design.md §3.7.
"""
