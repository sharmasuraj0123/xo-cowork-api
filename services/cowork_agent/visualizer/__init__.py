"""Visualizer subsystem.

Two halves with a strict boundary:

* ``reader.py`` is pure: it loads ``.xo/`` JSON files and produces
  Python dicts. The BFF scope handles (``services/cowork_agent/scopes.py``
  ``VisualizerScope`` / ``WorkspaceVisualizerScope``) call into the
  reader. Nothing here imports ``os``/``pathlib`` for filesystem reads
  outside of ``reader.py``.

* The writer (the watcher) lives under ``sources/``, ``ingest/``,
  ``sinks/``, ``workspace/`` (added in Phase 2). It owns every
  ``.xo/`` file the BFF reads, with one exception: ``.xo/sessions/
  sessionslist.json`` is adapter-owned (see docs/watcher-design.md
  §3.7).

BFF routes import only the scope handles, not anything in this
package. The scope handles import from ``reader.py`` only.
"""
