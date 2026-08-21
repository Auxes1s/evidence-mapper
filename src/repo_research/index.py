from __future__ import annotations

import json, sqlite3, signal
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable
from .extract import eligible_files, extract_file
from .models import Chunk
from .store import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
 chunk_id TEXT PRIMARY KEY, source_path TEXT, source_type TEXT, source_hash TEXT,
 text TEXT, ordinal INTEGER, location_json TEXT, mtime REAL, size INTEGER
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(chunk_id UNINDEXED, source_path, text, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS sources (
 source_path TEXT PRIMARY KEY, source_type TEXT, source_hash TEXT, mtime REAL, size INTEGER,
 chunk_count INTEGER, indexed_at TEXT
);
"""

@contextmanager
def _file_deadline(seconds: int):
    """Bound cloud-backed file work on POSIX; no-op where alarms are unavailable."""
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return
    previous = signal.getsignal(signal.SIGALRM)
    def expired(signum, frame):
        raise TimeoutError(f"per-file extraction exceeded {seconds} seconds")
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)

class Index:
    def __init__(self, root: Path, store: Store):
        self.root, self.store = root, store
        self.db = sqlite3.connect(store.state / "index.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def build(self, cfg: dict, force: bool = False) -> dict:
        from datetime import datetime, timezone
        failures = []
        files, indexed, unchanged = eligible_files(self.root, cfg, failures), 0, 0
        seen = set()
        for path in files:
            rel = str(path.relative_to(self.root)); seen.add(rel)
            try:
                with _file_deadline(int(cfg.get("file_timeout_seconds", 120))):
                    st = path.stat()
                    old = self.db.execute("SELECT mtime,size FROM sources WHERE source_path=?", (rel,)).fetchone()
                    if old and not force and old["mtime"] == st.st_mtime and old["size"] == st.st_size:
                        unchanged += 1
                        continue
                    chunks, failure = extract_file(path, self.root, cfg)
            except (OSError, TimeoutError) as exc:
                failures.append({"source_path": rel, "source_hash": "", "error": str(exc),
                                 "source_type": path.suffix.lower().lstrip(".") or "filesystem"})
                continue
            self.db.execute("DELETE FROM chunks_fts WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE source_path=?)", (rel,))
            self.db.execute("DELETE FROM chunks WHERE source_path=?", (rel,))
            self.db.execute("DELETE FROM sources WHERE source_path=?", (rel,))
            if failure:
                failures.append(failure); continue
            for c in chunks:
                loc = c.dict(); text = loc.pop("text"); cid = loc.pop("chunk_id")
                for k in ("source_path", "source_type", "source_hash", "ordinal"): loc.pop(k)
                self.db.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?)",
                    (cid, c.source_path, c.source_type, c.source_hash, text, c.ordinal,
                     json.dumps(loc), st.st_mtime, st.st_size))
                self.db.execute("INSERT INTO chunks_fts VALUES (?,?,?)", (cid, c.source_path, text))
            digest = chunks[0].source_hash if chunks else ""
            self.db.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?)",
                (rel, path.suffix.lower().lstrip("."), digest, st.st_mtime, st.st_size,
                 len(chunks), datetime.now(timezone.utc).isoformat()))
            indexed += 1
            # Bound rollback exposure for very large/cloud-backed corpora. A later
            # per-file timeout should not discard hours of completed extraction.
            if indexed % 25 == 0:
                self.db.commit()
        removed = [r[0] for r in self.db.execute("SELECT source_path FROM sources") if r[0] not in seen]
        for rel in removed:
            self.db.execute("DELETE FROM chunks_fts WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE source_path=?)", (rel,))
            self.db.execute("DELETE FROM chunks WHERE source_path=?", (rel,)); self.db.execute("DELETE FROM sources WHERE source_path=?", (rel,))
        self.db.commit()
        sources = [dict(r) for r in self.db.execute("SELECT * FROM sources ORDER BY source_path")]
        self.store.replace("source_index.jsonl", sources)
        if failures: self.store.append_many("extraction_failures.jsonl", failures)
        return {"files_discovered": len(files), "files_indexed": indexed, "files_unchanged": unchanged,
                "files_removed": len(removed), "chunks": self.db.execute("SELECT count(*) FROM chunks").fetchone()[0],
                "extraction_failures": failures}

    def _row_chunk(self, row) -> Chunk:
        loc = json.loads(row["location_json"])
        return Chunk(row["chunk_id"], row["source_path"], row["source_type"], row["source_hash"],
                     row["text"], row["ordinal"], **loc)

    def search(self, terms: list[str], limit: int = 48) -> list[Chunk]:
        clean = []
        for term in terms:
            words = [w.replace('"', '') for w in term.split() if len(w) > 1]
            if words: clean.append(" AND ".join(f'"{w}"' for w in words[:8]))
        query = " OR ".join(f"({x})" for x in clean) or '"evidence"'
        try:
            rows = self.db.execute("""SELECT c.*, bm25(chunks_fts) score FROM chunks_fts
              JOIN chunks c USING(chunk_id) WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?""", (query, limit)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        return [self._row_chunk(r) for r in rows]

    def all_chunks(self, limit: int | None = None) -> list[Chunk]:
        sql = "SELECT * FROM chunks ORDER BY source_path, ordinal" + (" LIMIT ?" if limit else "")
        rows = self.db.execute(sql, (limit,)).fetchall() if limit else self.db.execute(sql).fetchall()
        return [self._row_chunk(r) for r in rows]

    def source_hashes(self) -> dict[str, str]:
        return {r[0]: r[1] for r in self.db.execute("SELECT source_path,source_hash FROM sources")}

    def text_stats(self) -> dict[str, int]:
        row = self.db.execute("SELECT count(*), coalesce(sum(length(text)),0), count(distinct source_path) FROM chunks").fetchone()
        return {"chunks": int(row[0]), "characters": int(row[1]), "sources": int(row[2])}

    def chunk_by_id(self, chunk_id: str) -> Chunk | None:
        row = self.db.execute("SELECT * FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
        return self._row_chunk(row) if row else None

    def chunks_for_source(self, source_path: str) -> list[Chunk]:
        rows = self.db.execute("SELECT * FROM chunks WHERE source_path=? ORDER BY ordinal", (source_path,)).fetchall()
        return [self._row_chunk(r) for r in rows]

    def expand(self, chunk: Chunk, radius: int) -> list[Chunk]:
        rows = self.db.execute("SELECT * FROM chunks WHERE source_path=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal",
            (chunk.source_path, max(0, chunk.ordinal-radius), chunk.ordinal+radius)).fetchall()
        return [self._row_chunk(r) for r in rows]
