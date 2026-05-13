"""Sinks — apply events to ``<project>/.xo/`` state files.

Each sink is a stateless module exposing one top-level function (or a
small class) that takes ``(xo_dir, events_or_state)`` and rewrites
exactly one file under ``xo_dir``. State across ticks lives in the
files themselves; the watcher re-reads on each call. This makes the
sinks restart-safe and trivially testable.

Files owned by each sink:

* :mod:`project_json`     → ``project.json``         (one-shot identity fill)
* :mod:`sessions_augment` → ``sessions/sessions-augment.json``
* :mod:`todos`            → ``todos.json``
* :mod:`activity`         → ``activity.json``
* :mod:`stats`            → ``stats.json``
* :mod:`timeline`         → ``timeline.jsonl``  (append-only, rotated)

The adapter-owned ``sessions/sessionslist.json`` is NOT in this list
— see docs/watcher-design.md §3.7.
"""
