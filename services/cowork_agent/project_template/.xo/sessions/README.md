# Sessions

Append-only log of every session. **Never delete** outside the compression policy.

## Layout

```
sessions/
├── index.md                # Manifest. One line per session.
├── {YYYY-MM-DD}/
│   └── session.md          # Narrative for that session.
└── compressed/
    └── {YYYY-MM}.md        # Folded summaries of older sessions.
```

## Compression policy (cheap and predictable)

At every session-close, the session-closer subagent runs this rule:

> If `sessions/` contains more than **20** date-stamped folders, fold the oldest **5** into `sessions/compressed/{YYYY-MM}.md` (one paragraph per folded session). Verify the compressed file is non-empty, then delete the originals.

That's the entire policy. No semantic compression, no token-counting, no thresholds beyond the count check. Predictable trigger, bounded work, easy to audit.

## Reading

The main agent reads only the last 3 lines of `index.md` at boot. To read further (full session narratives, compressed archives), it dispatches the `memory-archivist` subagent. Never read these files directly from the main thread.
