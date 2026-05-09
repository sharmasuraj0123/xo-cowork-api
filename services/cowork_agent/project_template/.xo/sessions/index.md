# Sessions index

> Manifest of all sessions. One line per session. Append-only. The main agent reads only the **last 3 lines** of this file at boot — anything older is for the memory-archivist.

Format:
```
- YYYY-MM-DD: [outcome] one-sentence summary  →  /.xo/sessions/{date}/session.md
```

---
