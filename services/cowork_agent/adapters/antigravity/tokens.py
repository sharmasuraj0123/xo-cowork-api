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
``-wal`` sidecar. A read-only (``mode=ro``) connection still honors the ``-wal``
when the ``-shm`` sidecar is readable, so we open **read-only first** and never
mutate agy's store on the accounting path. Only if that read-only path can't
surface the data — it raises, or returns no rows while a non-empty ``-wal``
suggests uncheckpointed rows are hidden — do we fall back to a short read-write
open that runs ``PRAGMA wal_checkpoint(TRUNCATE)`` and re-reads. The fallback is
the exception, not the default.

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


def _read_calls(con: sqlite3.Connection) -> list[tuple]:
    """Parse one ``(idx, model, in_tokens, out_tokens)`` per ``gen_metadata`` row.

    The protobuf/row parsing is unchanged — only the connection strategy differs
    between the read-only and checkpointing callers. May raise ``sqlite3.Error``
    (e.g. missing table), which the callers translate into a fallback / zeros."""
    calls: list[tuple] = []
    cur = con.execute("SELECT idx, data FROM gen_metadata ORDER BY idx")
    for idx, data in cur:
        if not isinstance(data, (bytes, bytearray)):
            continue
        m1 = _dec(_sub(_dec(data), 1) or b"")
        model = _str(m1, 21) or _str(m1, 19)
        out_t = _var(_dec(_sub(m1, 4) or b""), 3)
        in_t = _var(_dec(_sub(_dec(_sub(m1, 9) or b""), 10) or b""), 1)
        calls.append((idx, model, in_t or 0, out_t or 0))
    return calls


def _read_ro(db_path: Path) -> list[tuple] | None:
    """Read token rows via a **read-only** (non-mutating) connection.

    ``mode=ro`` still honors the ``-wal`` sidecar when ``-shm`` is readable, so
    this is the default accounting path — it never touches agy's store. Returns
    the parsed calls, or ``None`` if the DB could not be opened/read read-only
    (so the caller can fall back to a checkpointing open)."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        return _read_calls(con)
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _read_checkpointed(db_path: Path) -> list[tuple]:
    """Fallback: open **read-write**, flush the ``-wal`` into the main db via
    ``PRAGMA wal_checkpoint(TRUNCATE)``, then read.

    This mutates agy's store, so it runs only when :func:`_read_ro` can't see
    the data. Numbers match the historical (always-checkpoint) behavior."""
    try:
        con = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return []
    try:
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.commit()
        except sqlite3.Error:
            pass
        return _read_calls(con)
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _has_pending_wal(db_path: Path) -> bool:
    """True if a non-empty ``-wal`` sidecar exists (maybe uncheckpointed rows)."""
    try:
        return Path(f"{db_path}-wal").stat().st_size > 0
    except OSError:
        return False


def extract_usage(db_path: str | Path) -> dict:
    """One row per model call in ``gen_metadata``. Returns a summary dict:

        {"num_calls", "total_input", "total_output", "context_peak",
         "model", "calls": [(idx, model, in_tokens, out_tokens), …]}

    Reads **read-only** by default (no mutation of agy's store); only falls back
    to a checkpointing read-write open when the read-only path can't surface the
    data. Best-effort — missing DB / table / malformed blob → zeros."""
    empty = {"num_calls": 0, "total_input": 0, "total_output": 0,
             "context_peak": 0, "model": None, "calls": []}
    p = Path(db_path)
    if not p.is_file():
        return empty

    # Default: non-mutating read-only read (honors -wal via -shm).
    calls = _read_ro(p)
    # Fall back to the (mutating) checkpointing open only when the read-only
    # path genuinely failed to surface data: it raised (calls is None), or it
    # saw no rows while a non-empty -wal suggests uncheckpointed data is hidden
    # from a read-only connection. Genuinely-empty DBs are left untouched.
    if calls is None or (not calls and _has_pending_wal(p)):
        calls = _read_checkpointed(p)

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
