from __future__ import annotations

import hashlib
import json
import math
import shutil
import signal
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .extract import discover_files, extract_file, file_hash, extraction_policy, stable_id
from .models import Chunk
from .store import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
 chunk_id TEXT PRIMARY KEY, source_path TEXT, source_type TEXT, source_hash TEXT,
 text TEXT, ordinal INTEGER, location_json TEXT, mtime REAL, size INTEGER
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(chunk_id UNINDEXED, source_path, text, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS sections (
 section_id TEXT PRIMARY KEY, source_path TEXT NOT NULL, source_hash TEXT NOT NULL,
 locator_json TEXT NOT NULL, text TEXT NOT NULL, chunk_ids_json TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS sections_fts USING fts5(section_id UNINDEXED, source_path, text, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS sources (
 source_path TEXT PRIMARY KEY, source_type TEXT, source_hash TEXT, mtime REAL, size INTEGER,
 chunk_count INTEGER, indexed_at TEXT
);
CREATE TABLE IF NOT EXISTS chunk_embeddings (
 chunk_id TEXT NOT NULL, model TEXT NOT NULL, source_hash TEXT NOT NULL, vector_json TEXT NOT NULL,
 PRIMARY KEY(chunk_id, model)
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
    def __init__(self, root: Path, store: Store, database=None):
        self.root, self.store = root, store
        self.db = sqlite3.connect(database or store.state / "index.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.embedding_identity = None
        self.embedding_workspace = None
        self.query_instruction = ""

    def populate(self, chunks: list[Chunk]) -> None:
        """Build a derived index from an immutable ledger snapshot."""
        if self.db.execute("SELECT count(*) FROM chunks").fetchone()[0]:
            return
        grouped = {}
        with self.db:
            for c in chunks:
                loc = c.dict(); text = loc.pop("text"); cid = loc.pop("chunk_id")
                for key in ("source_path","source_type","source_hash","ordinal"): loc.pop(key)
                self.db.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?)",
                                (cid,c.source_path,c.source_type,c.source_hash,text,c.ordinal,json.dumps(loc),0,0))
                self.db.execute("INSERT INTO chunks_fts VALUES(?,?,?)",(cid,c.source_path,text))
                grouped.setdefault(c.source_path,[]).append(c)
            for path,members in grouped.items():
                self._index_sections(path,members)
                c=members[0]
                self.db.execute("INSERT INTO sources VALUES(?,?,?,?,?,?,?)",
                                (path,c.source_type,c.source_hash,0,0,len(members),"snapshot"))

    def _cache_objects(self, path: Path, digest: str, chunks: list[Chunk], cfg: dict) -> None:
        objects=self.store.state/"objects"/digest[:2]
        objects.mkdir(parents=True,exist_ok=True)
        parsed=objects/f"{digest}.{stable_id(str(path.relative_to(self.root)),extraction_policy(cfg))}.chunks.json"
        if not parsed.exists():
            parsed.write_text(json.dumps([c.dict() for c in chunks],ensure_ascii=False,sort_keys=True),encoding="utf-8")
        if cfg.get("archive_source_bytes"):
            archived=objects/f"{digest}.source"
            if not archived.exists(): shutil.copyfile(path,archived)

    def _index_sections(self, source_path: str, chunks: list[Chunk]) -> None:
        self.db.execute("DELETE FROM sections_fts WHERE section_id IN (SELECT section_id FROM sections WHERE source_path=?)",(source_path,))
        self.db.execute("DELETE FROM sections WHERE source_path=?",(source_path,))
        groups: dict[tuple,list[Chunk]]={}
        for chunk in chunks:
            unit=(chunk.page,chunk.sheet,chunk.section)
            groups.setdefault(unit,[]).append(chunk)
        for unit,members in groups.items():
            locator={k:v for k,v in zip(("page","sheet","section"),unit) if v is not None}
            section_id=hashlib.sha256(f"{source_path}\x1f{members[0].source_hash}\x1f{json.dumps(locator,sort_keys=True)}".encode()).hexdigest()[:24]
            text="\n\n".join(c.text for c in sorted(members,key=lambda x:x.metadata.get("document_position",x.ordinal)))
            chunk_ids=[c.chunk_id for c in sorted(members,key=lambda x:x.ordinal)]
            self.db.execute("INSERT INTO sections VALUES(?,?,?,?,?,?)",
                            (section_id,source_path,members[0].source_hash,json.dumps(locator),text,json.dumps(chunk_ids)))
            self.db.execute("INSERT INTO sections_fts VALUES(?,?,?)",(section_id,source_path,text))

    def build(self, cfg: dict, force: bool = False) -> dict:
        from datetime import datetime, timezone
        failures = []
        files, manifest = discover_files(self.root, cfg, failures)
        manifest_by_path = {x["source_path"]:x for x in manifest}
        indexed, unchanged = 0, 0
        seen = set()
        for path in files:
            rel = str(path.relative_to(self.root)); seen.add(rel)
            try:
                with _file_deadline(int(cfg.get("file_timeout_seconds", 120))):
                    st = path.stat()
                    digest = file_hash(path, cfg.get("file_timeout_seconds"))
                    manifest_by_path[rel]["source_hash"] = digest
                    old = self.db.execute("SELECT source_hash FROM sources WHERE source_path=?", (rel,)).fetchone()
                    existing_chunks=self.chunks_for_source(rel) if old else []
                    parser_current=bool(existing_chunks) and existing_chunks[0].metadata.get("parser_policy")==extraction_policy(cfg)
                    if old and not force and old["source_hash"] == digest and parser_current:
                        unchanged += 1
                        existing_chunks=self.chunks_for_source(rel)
                        extracted_hash=hashlib.sha256("\x1e".join(c.text for c in existing_chunks).encode()).hexdigest()
                        manifest_by_path[rel].update(status="indexed", chunk_count=len(existing_chunks),
                                                     extracted_text_hash=extracted_hash)
                        diagnostic=existing_chunks[0].metadata.get("extraction_diagnostic")
                        if diagnostic:
                            failures.append(diagnostic)
                            manifest_by_path[rel]["reason"]=diagnostic["error"]
                        self._cache_objects(path,digest,existing_chunks,cfg)
                        if not self.db.execute("SELECT 1 FROM sections WHERE source_path=? LIMIT 1",(rel,)).fetchone():
                            self._index_sections(rel,existing_chunks)
                        continue
                    chunks, failure = extract_file(path, self.root, cfg, source_hash=digest)
            except (OSError, TimeoutError) as exc:
                failures.append({"source_path": rel, "source_hash": "", "error": str(exc),
                                 "source_type": path.suffix.lower().lstrip(".") or "filesystem"})
                manifest_by_path[rel].update(status="failed", reason=str(exc))
                self.db.execute("DELETE FROM chunks_fts WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE source_path=?)", (rel,))
                self.db.execute("DELETE FROM chunk_embeddings WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE source_path=?)", (rel,))
                self.db.execute("DELETE FROM sections_fts WHERE section_id IN (SELECT section_id FROM sections WHERE source_path=?)",(rel,))
                self.db.execute("DELETE FROM sections WHERE source_path=?",(rel,))
                self.db.execute("DELETE FROM chunks WHERE source_path=?", (rel,))
                self.db.execute("DELETE FROM sources WHERE source_path=?", (rel,))
                continue
            self.db.execute("DELETE FROM chunks_fts WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE source_path=?)", (rel,))
            self.db.execute("DELETE FROM chunk_embeddings WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE source_path=?)", (rel,))
            self.db.execute("DELETE FROM sections_fts WHERE section_id IN (SELECT section_id FROM sections WHERE source_path=?)",(rel,))
            self.db.execute("DELETE FROM sections WHERE source_path=?",(rel,))
            self.db.execute("DELETE FROM chunks WHERE source_path=?", (rel,))
            self.db.execute("DELETE FROM sources WHERE source_path=?", (rel,))
            if failure:
                failures.append(failure)
                if not chunks:
                    manifest_by_path[rel].update(status="failed", reason=failure["error"])
                    continue
                chunks[0].metadata["extraction_diagnostic"]=failure
            for position,c in enumerate(chunks):
                c.metadata["document_position"]=position
                loc = c.dict(); text = loc.pop("text"); cid = loc.pop("chunk_id")
                for k in ("source_path", "source_type", "source_hash", "ordinal"): loc.pop(k)
                self.db.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?)",
                    (cid, c.source_path, c.source_type, c.source_hash, text, c.ordinal,
                     json.dumps(loc), st.st_mtime, st.st_size))
                self.db.execute("INSERT INTO chunks_fts VALUES (?,?,?)", (cid, c.source_path, text))
            self._index_sections(rel,chunks)
            extracted_hash = hashlib.sha256("\x1e".join(c.text for c in chunks).encode()).hexdigest()
            self.db.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?)",
                (rel, path.suffix.lower().lstrip("."), digest, st.st_mtime, st.st_size,
                 len(chunks), datetime.now(timezone.utc).isoformat()))
            manifest_by_path[rel].update(status="indexed",chunk_count=len(chunks),extracted_text_hash=extracted_hash,
                                         reason=failure["error"] if failure else None)
            self._cache_objects(path,digest,chunks,cfg)
            indexed += 1
            # Bound rollback exposure for very large/cloud-backed corpora. A later
            # per-file timeout should not discard hours of completed extraction.
            if indexed % 25 == 0:
                self.db.commit()
        removed = [r[0] for r in self.db.execute("SELECT source_path FROM sources") if r[0] not in seen]
        for rel in removed:
            self.db.execute("DELETE FROM chunks_fts WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE source_path=?)", (rel,))
            self.db.execute("DELETE FROM chunk_embeddings WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE source_path=?)", (rel,))
            self.db.execute("DELETE FROM sections_fts WHERE section_id IN (SELECT section_id FROM sections WHERE source_path=?)",(rel,))
            self.db.execute("DELETE FROM sections WHERE source_path=?",(rel,))
            self.db.execute("DELETE FROM chunks WHERE source_path=?", (rel,)); self.db.execute("DELETE FROM sources WHERE source_path=?", (rel,))
        self.db.commit()
        sources = [dict(r) for r in self.db.execute("SELECT * FROM sources ORDER BY source_path")]
        self.store.replace("source_index.jsonl", sources)
        if failures: self.store.append_many("extraction_failures.jsonl", failures)
        return {"files_discovered": len(manifest), "files_eligible":len(files), "files_indexed": indexed, "files_unchanged": unchanged,
                "files_removed": len(removed), "chunks": self.db.execute("SELECT count(*) FROM chunks").fetchone()[0],
                "extraction_failures": failures, "file_manifest":list(manifest_by_path.values())}

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
              JOIN chunks c USING(chunk_id) WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?""", (query, max(limit*2,limit))).fetchall()
            section_rows=self.db.execute("""SELECT s.*,bm25(sections_fts) score FROM sections_fts
              JOIN sections s USING(section_id) WHERE sections_fts MATCH ? ORDER BY score LIMIT ?""",(query,max(4,limit//2))).fetchall()
        except sqlite3.OperationalError as exc:
            raise RuntimeError(f"FTS retrieval failed for query {query!r}: {exc}") from exc
        fused: dict[str,tuple[float,Chunk,dict[str,int]]]={}
        for rank,row in enumerate(rows,1):
            chunk=self._row_chunk(row); fused[chunk.chunk_id]=(1/(60+rank),chunk,{"chunk_fts":rank})
        section_rank=0
        for section in section_rows:
            for chunk_id in json.loads(section["chunk_ids_json"]):
                section_rank+=1; current=fused.get(chunk_id)
                chunk=current[1] if current else self.chunk_by_id(chunk_id)
                if not chunk: continue
                score,ranks=(current[0],current[2]) if current else (0.0,{})
                ranks["section_fts"]=section_rank; fused[chunk_id]=(score+1/(60+section_rank),chunk,ranks)
        ordered=sorted(fused.values(),key=lambda x:(-x[0],x[1].chunk_id))[:limit]
        out=[]
        for score,chunk,ranks in ordered:
            chunk.metadata.update(retrieval_score=score,retrieval_channels=sorted(ranks),retrieval_ranks=ranks)
            out.append(chunk)
        return out

    def ensure_embeddings(self, model: str, embedder, batch_size: int = 32) -> int:
        key = self.embedding_identity or model
        rows=self.db.execute("""SELECT c.* FROM chunks c LEFT JOIN chunk_embeddings e
            ON c.chunk_id=e.chunk_id AND e.model=?
            WHERE e.chunk_id IS NULL OR e.source_hash!=c.source_hash ORDER BY c.source_path,c.ordinal""",(key,)).fetchall()
        chunks=[self._row_chunk(r) for r in rows]
        created=0
        missing=[]
        for c in chunks:
            vector=self.embedding_workspace.vector(c,key) if self.embedding_workspace else None
            if vector is not None:
                self.db.execute("INSERT OR REPLACE INTO chunk_embeddings VALUES(?,?,?,?)",(c.chunk_id,key,c.source_hash,json.dumps(vector)))
            else: missing.append(c)
        for start in range(0,len(missing),max(1,batch_size)):
            batch=missing[start:start+max(1,batch_size)]
            vectors=embedder.embed([c.text for c in batch],model=model)
            if len(vectors)!=len(batch): raise ValueError("Embedding backend omitted inputs")
            with self.db:
                for chunk,vector in zip(batch,vectors):
                    if not vector or not all(math.isfinite(x) for x in vector): raise ValueError("Invalid embedding vector")
                    self.db.execute("INSERT OR REPLACE INTO chunk_embeddings VALUES(?,?,?,?)",
                                    (chunk.chunk_id,key,chunk.source_hash,json.dumps(vector,separators=(",",":"))))
                    if self.embedding_workspace: self.embedding_workspace.save_vector(chunk,key,vector)
                    created+=1
        self.db.commit()
        return created

    def hybrid_search(self, terms: list[str], query_text: str, limit: int, model: str, embedder) -> list[Chunk]:
        lexical=self.search(terms,max(limit*3,limit))
        key=self.embedding_identity or model
        if self.query_instruction:
            query_text=f"Instruct: {self.query_instruction}\nQuery: {query_text}"
        query_vector=(self.embedding_workspace.query_vector(query_text,key,embedder,model)
                      if self.embedding_workspace else embedder.embed([query_text],model=model)[0])
        qnorm=sum(x*x for x in query_vector)**0.5 or 1.0
        vector_rank=[]
        for row in self.db.execute("""SELECT c.*,e.vector_json FROM chunk_embeddings e
            JOIN chunks c USING(chunk_id) WHERE e.model=?""",(key,)):
            vector=json.loads(row["vector_json"])
            if len(vector)!=len(query_vector): raise ValueError("Embedding dimension mismatch; rebuild the derived index")
            norm=sum(x*x for x in vector)**0.5 or 1.0
            score=sum(a*b for a,b in zip(query_vector,vector))/(qnorm*norm)
            vector_rank.append((score,self._row_chunk(row)))
        vector_rank.sort(key=lambda x:(-x[0],x[1].chunk_id))
        fused: dict[str,tuple[float,Chunk,dict[str,int]]] = {}
        for channel,ranked in (("lexical",lexical),("vector",[c for _,c in vector_rank[:max(limit*3,limit)]])):
            for rank,chunk in enumerate(ranked,1):
                score,kept,ranks=fused.get(chunk.chunk_id,(0.0,chunk,{}))
                ranks[channel]=rank
                fused[chunk.chunk_id]=(score+1.0/(60+rank),kept,ranks)
        ordered=sorted(fused.values(),key=lambda x:(-x[0],x[1].chunk_id))[:limit]
        out=[]
        for score,chunk,ranks in ordered:
            chunk.metadata.update(retrieval_score=score,retrieval_channels=sorted(ranks),retrieval_ranks=ranks)
            out.append(chunk)
        return out

    def all_chunks(self, limit: int | None = None) -> list[Chunk]:
        sql = "SELECT * FROM chunks ORDER BY source_path, ordinal" + (" LIMIT ?" if limit else "")
        rows = self.db.execute(sql, (limit,)).fetchall() if limit else self.db.execute(sql).fetchall()
        return sorted([self._row_chunk(r) for r in rows],key=lambda c:(c.source_path,c.metadata.get("document_position",c.ordinal)))

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
        return sorted([self._row_chunk(r) for r in rows],key=lambda c:c.metadata.get("document_position",c.ordinal))

    def expand(self, chunk: Chunk, radius: int) -> list[Chunk]:
        peers=self.chunks_for_source(chunk.source_path)
        def same_unit(other: Chunk) -> bool:
            for key in ("page","sheet","section"):
                value=getattr(chunk,key)
                if value is not None and getattr(other,key) != value: return False
            return True
        members=[c for c in peers if same_unit(c)]
        position=next((i for i,c in enumerate(members) if c.chunk_id==chunk.chunk_id),None)
        return members[max(0,position-radius):position+radius+1] if position is not None else []
