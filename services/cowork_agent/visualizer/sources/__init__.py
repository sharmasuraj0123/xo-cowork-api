"""Source protocol home.

Concrete sources moved to ``services/cowork_agent/adapters/<name>/visualizer_source.py``
so each agent owns its own watcher source alongside its other adapter
modules (``adapter.py``, ``usage.py``, ``streaming.py``, …). The
active source is resolved at watcher startup by
``services/cowork_agent/visualizer/source_loader.py``.

What stays here: :mod:`.base`, the :class:`.base.Source` Protocol that
concrete sources structurally conform to.
"""
