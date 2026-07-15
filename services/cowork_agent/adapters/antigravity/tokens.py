"""
Antigravity (agy) token accounting — read from the SQLite trajectory DB.

Tokens are **not** in the JSONL transcript. They live in
``conversations/<uuid>.db``, table ``gen_metadata``, column ``data`` — a protobuf
blob, one row per model call. Field paths inside ``gen_metadata.data``:

    input/context tokens (this call):  f1.f9.f10.f1
    output tokens (this call):         f1.f4.f3
    model display name:                f1.f21  (e.g. "Gemini 3.5 Flash (Low)")

**Caveats (make agy's numbers NOT comparable to claude/openclaw):**
  * These are **client-side tokenizer ESTIMATES**, not the provider's billed usage.
  * There is **no cached-token field** — no cache_read / cache_write for agy.
  * Output **excludes** hidden reasoning/thinking tokens.
  * Each agentic turn re-sends the growing context, so Σ(per-call input) ≫ final
    context. We record ``total_input`` (Σ per-call = billed-input upper bound) and
    ``context_peak`` (max per-call = context-window peak).

**WAL gotcha:** the DB is WAL-mode and agy writes ``gen_metadata`` rows into the
``-wal`` sidecar. Opening read-only can miss them; we open a short read-write
connection to ``PRAGMA wal_checkpoint(TRUNCATE)`` first (agy has exited by the
time we read, so this is safe), then read.

Stdlib-only. Best-effort throughout: any failure yields zeros, never raises.
"""
from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

from services.cowork_agent.adapters.antigravity.paths import conversation_db


# ── Minimal protobuf wire decoder ─────────────────────────────────────────────


def _varint(b: bytes, i: int) -> tuple[int, int]:
    s = r = 0
    while True:
        x = b[i]; i += 1; r |= (x & 0x7F) << s
        if not x & 0x80:
            return r, i
        s += 7


def _dec(b: bytes) -> list[tuple[int, int, object]]:
    out: list[tuple[int, int, object]] = []
    i, n = 0, len(b)
    while i < n:
        try:
            k, i = _varint(b, i)
        except IndexError:
            break
        f, wt = k >> 3, k & 7
        try:
            if wt == 0:
                v, i = _varint(b, i)
            elif wt == 1:
                v = struct.unpack("<Q", b[i:i + 8])[0]; i += 8
            elif wt == 2:
                ln, i = _varint(b, i); v = b[i:i + ln]; i += ln
            elif wt == 5:
                v = struct.unpack("<I", b[i:i + 4])[0]; i += 4
            else:
                break
        except (IndexError, struct.error):
            break
        out.append((f, wt, v))
    return out


def _sub(d, fn):
    return next((v for f, wt, v in d if f == fn and wt == 2), None)


def _var(d, fn):
    return next((v for f, wt, v in d if f == fn and wt == 0), None)


def _str(d, fn):
    return next((v.decode("utf-8", "replace") for f, wt, v in d if f == fn and wt == 2), None)


# ── Extraction ────────────────────────────────────────────────────────────────


def _checkpoint(db_path: Path) -> None:
    """Flush the -wal sidecar into the main db so a read sees all rows."""
    try:
        con = sqlite3.connect(str(db_path))
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:
        pass


def extract_usage(db_path: str | Path) -> dict:
    """One row per model call in ``gen_metadata``. Returns a summary dict:

        {"num_calls", "total_input", "total_output", "context_peak",
         "model", "calls": [(idx, model, in_tokens, out_tokens), …]}

    Best-effort — missing DB / table / malformed blob → zeros."""
    empty = {"num_calls": 0, "total_input": 0, "total_output": 0,
             "context_peak": 0, "model": None, "calls": []}
    p = Path(db_path)
    if not p.is_file():
        return empty
    _checkpoint(p)
    calls: list[tuple] = []
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    except sqlite3.Error:
        return empty
    try:
        cur = con.execute("SELECT idx, data FROM gen_metadata ORDER BY idx")
        for idx, data in cur:
            if not isinstance(data, (bytes, bytearray)):
                continue
            m1 = _dec(_sub(_dec(data), 1) or b"")
            model = _str(m1, 21) or _str(m1, 19)
            out_t = _var(_dec(_sub(m1, 4) or b""), 3)
            in_t = _var(_dec(_sub(_dec(_sub(m1, 9) or b""), 10) or b""), 1)
            calls.append((idx, model, in_t or 0, out_t or 0))
    except sqlite3.Error:
        return empty
    finally:
        con.close()
    return {
        "num_calls": len(calls),
        "total_input": sum(c[2] for c in calls),
        "total_output": sum(c[3] for c in calls),
        "context_peak": max((c[2] for c in calls), default=0),
        "model": calls[0][1] if calls else None,
        "calls": calls,
    }


def conversation_tokens(conversation_id: str) -> dict:
    """Token summary for one conversation (by uuid). Best-effort."""
    return extract_usage(conversation_db(conversation_id))


__all__ = ["extract_usage", "conversation_tokens"]
