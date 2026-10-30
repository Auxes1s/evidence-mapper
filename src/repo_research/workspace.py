"""Authoritative, snapshot-pinned research state; indexes and JSONL are derived."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from .audit import AuditStore, _json
from .models import Chunk

SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence_ids (
 fingerprint TEXT PRIMARY KEY, evidence_id TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_chunks (
 snapshot_id TEXT NOT NULL, chunk_id TEXT NOT NULL, position INTEGER NOT NULL,
 record_json TEXT NOT NULL, PRIMARY KEY(snapshot_id,chunk_id)
);
CREATE TABLE IF NOT EXISTS source_units (
 snapshot_id TEXT NOT NULL, unit_id TEXT NOT NULL, kind TEXT NOT NULL,
 parent_id TEXT, previous_id TEXT, next_id TEXT, record_json TEXT NOT NULL,
 PRIMARY KEY(snapshot_id,unit_id)
);
CREATE TABLE IF NOT EXISTS research_state (
 run_id TEXT PRIMARY KEY, state_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
 task_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, proposition_id TEXT NOT NULL,
 kind TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE INDEX IF NOT EXISTS task_queue ON tasks(run_id,status,proposition_id);
CREATE TABLE IF NOT EXISTS evidence_relations (
 run_id TEXT NOT NULL, proposition_id TEXT NOT NULL, relation_id TEXT NOT NULL,
 kind TEXT NOT NULL, evidence_ids_json TEXT NOT NULL, note TEXT NOT NULL,
 interpretation_status TEXT NOT NULL DEFAULT 'candidate',
 PRIMARY KEY(run_id,relation_id)
);
CREATE TABLE IF NOT EXISTS bundles (
 run_id TEXT NOT NULL, bundle_id TEXT NOT NULL, proposition_id TEXT NOT NULL,
 record_json TEXT NOT NULL, PRIMARY KEY(run_id,bundle_id)
);
CREATE TABLE IF NOT EXISTS embedding_cache (
 chunk_id TEXT NOT NULL, source_hash TEXT NOT NULL, identity TEXT NOT NULL,
 vector_json TEXT NOT NULL, PRIMARY KEY(chunk_id,source_hash,identity)
);
CREATE TABLE IF NOT EXISTS query_embedding_cache (
 query_hash TEXT NOT NULL, identity TEXT NOT NULL, vector_json TEXT NOT NULL,
 PRIMARY KEY(query_hash,identity)
);
CREATE TABLE IF NOT EXISTS deliveries (
 delivery_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL,
 estimated_tokens INTEGER NOT NULL, record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_failures (
 failure_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, call_id TEXT NOT NULL,
 record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_migrations (
 name TEXT PRIMARY KEY
);
"""


def identity(*parts: object) -> str:
    return hashlib.sha256(_json(parts).encode()).hexdigest()[:24]


class Workspace(AuditStore):
    def __init__(self, state: Path):
        super().__init__(state)
        self.db.executescript(SCHEMA)
        # Import old mappings once. Preserve identifiers and reject conflicting imports.
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 3:
            with self.db:
                path = state / "evidence_ids.jsonl"
                if path.exists():
                    for line in path.read_text().splitlines():
                        if not line.strip():
                            continue
                        item = json.loads(line)
                        self.db.execute(
                            "INSERT INTO evidence_ids VALUES(?,?)",
                            (item["fingerprint"], item["evidence_id"]),
                        )
                self.db.execute("PRAGMA user_version=3")
        # Pre-ledger releases stored their complete history only in JSONL.
        # Import before the first export rebuild, without replacing ledger records.
        if not self.db.execute(
            "SELECT 1 FROM workspace_migrations WHERE name='legacy-evidence-import'"
        ).fetchone():
            with self.db:
                path = state / "evidence.jsonl"
                if path.exists():
                    for number, line in enumerate(path.read_text().splitlines(), 1):
                        if not line.strip():
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise ValueError(
                                f"Corrupt legacy evidence at {path}:{number}"
                            ) from exc
                        if not isinstance(rec, dict) or not rec.get("id"):
                            raise ValueError(
                                f"Invalid legacy evidence at {path}:{number}"
                            )
                        run_id = rec.get("search_id") or "legacy-import"
                        self.db.execute(
                            "INSERT OR IGNORE INTO evidence_records VALUES(?,?,?,?,?,?)",
                            (
                                run_id,
                                rec["id"],
                                rec.get("proposition_id"),
                                rec.get("chunk_id") or "legacy",
                                rec.get("call_id") or "legacy",
                                _json(rec),
                            ),
                        )
                self.db.execute(
                    "INSERT INTO workspace_migrations VALUES('legacy-evidence-import')"
                )

    def assign_ids(self, records: list[dict]) -> list[dict]:
        out = []
        self.db.execute("BEGIN IMMEDIATE")
        try:
            largest = (
                self.db.execute(
                    "SELECT max(CAST(substr(evidence_id,2) AS INTEGER)) FROM evidence_ids"
                ).fetchone()[0]
                or 0
            )
            for original in records:
                rec = dict(original)
                fingerprint = rec.get("fingerprint") or rec["id"]
                row = self.db.execute(
                    "SELECT evidence_id FROM evidence_ids WHERE fingerprint=?",
                    (fingerprint,),
                ).fetchone()
                if row:
                    eid = row[0]
                else:
                    largest += 1
                    eid = f"E{largest:04d}"
                    self.db.execute(
                        "INSERT INTO evidence_ids VALUES(?,?)", (fingerprint, eid)
                    )
                rec.update(id=eid, fingerprint=fingerprint)
                out.append(rec)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return out

    def record_delivery(self, run_id: str, kind: str, payload: dict) -> int:
        from .promotion import estimate_tokens

        tokens = estimate_tokens(payload)
        record = {"search_id": run_id, "kind": kind, "estimated_tokens": tokens}
        with self.db:
            self.db.execute(
                "INSERT INTO deliveries VALUES(?,?,?,?,?)",
                (uuid.uuid4().hex, run_id, kind, tokens, _json(record)),
            )
        with (self.path.parent / "expansions.jsonl").open("a") as stream:
            stream.write(_json(record) + "\n")
        return tokens

    def save_chunks(self, snapshot: str, chunks: list[Chunk]) -> None:
        with self.db:
            by_source: dict[str, list[Chunk]] = {}
            for position, chunk in enumerate(chunks):
                self.db.execute(
                    "INSERT OR IGNORE INTO snapshot_chunks VALUES(?,?,?,?)",
                    (snapshot, chunk.chunk_id, position, _json(chunk.dict())),
                )
                by_source.setdefault(chunk.source_path, []).append(chunk)
            for path, members in by_source.items():
                document = identity(path, members[0].source_hash, "document")
                self.db.execute(
                    "INSERT OR IGNORE INTO source_units VALUES(?,?,?,?,?,?,?)",
                    (
                        snapshot,
                        document,
                        "document",
                        None,
                        None,
                        None,
                        _json(
                            {"source_path": path, "source_hash": members[0].source_hash}
                        ),
                    ),
                )
                for i, chunk in enumerate(members):
                    parent = chunk.metadata.get("parent_id") or document
                    parent_text = chunk.metadata.get("parent_text")
                    if parent_text is not None:
                        self.db.execute(
                            "INSERT OR IGNORE INTO source_units VALUES(?,?,?,?,?,?,?)",
                            (
                                snapshot,
                                parent,
                                "structural_unit",
                                document,
                                None,
                                None,
                                _json({"text": parent_text, "source_path": path}),
                            ),
                        )
                    record = chunk.dict()
                    record["metadata"] = {
                        k: v for k, v in chunk.metadata.items() if k != "parent_text"
                    }
                    self.db.execute(
                        "INSERT OR IGNORE INTO source_units VALUES(?,?,?,?,?,?,?)",
                        (
                            snapshot,
                            chunk.chunk_id,
                            "block",
                            parent,
                            members[i - 1].chunk_id if i else None,
                            members[i + 1].chunk_id if i + 1 < len(members) else None,
                            _json(record),
                        ),
                    )

    def chunks(self, snapshot: str) -> list[Chunk]:
        return [
            Chunk(**json.loads(row[0]))
            for row in self.db.execute(
                "SELECT record_json FROM snapshot_chunks WHERE snapshot_id=? ORDER BY position",
                (snapshot,),
            )
        ]

    def save_state(self, run_id: str, state: dict) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO research_state VALUES(?,?)",
                (run_id, _json(state)),
            )

    def state(self, run_id: str) -> dict:
        row = self.db.execute(
            "SELECT state_json FROM research_state WHERE run_id=?", (run_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"Run {run_id} has no v3 checkpoint")
        return json.loads(row[0])

    def enqueue(self, run_id: str, pid: str, kind: str, payload: dict) -> str:
        task_id = identity(run_id, pid, kind, payload)
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO tasks VALUES(?,?,?,?,?,?,0,NULL)",
                (task_id, run_id, pid, kind, "pending", _json(payload)),
            )
        return task_id

    def tasks(self, run_id: str, pid: str | None = None, statuses=()) -> list[dict]:
        query = "SELECT * FROM tasks WHERE run_id=?"
        args: list[Any] = [run_id]
        if pid:
            query += " AND proposition_id=?"
            args.append(pid)
        if statuses:
            query += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
            args.extend(statuses)
        out = []
        for row in self.db.execute(query + " ORDER BY rowid", args):
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            out.append(item)
        return out

    def task_status(self, task_id: str, status: str, error: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE tasks SET status=?,error=?,attempts=attempts+? WHERE task_id=?",
                (status, error, int(status == "running"), task_id),
            )

    def evidence(self, run_id: str, pid: str | None = None) -> list[dict]:
        query = "SELECT record_json FROM evidence_records WHERE run_id=?"
        args = [run_id]
        if pid:
            query += " AND proposition_id=?"
            args.append(pid)
        return [
            json.loads(row[0])
            for row in self.db.execute(query + " ORDER BY evidence_id", args)
        ]

    def add_relation(
        self, run_id: str, pid: str, kind: str, evidence_ids: list[str], note: str
    ) -> None:
        valid = {r["id"] for r in self.evidence(run_id, pid)}
        if not evidence_ids or not set(evidence_ids) <= valid:
            raise ValueError(
                "Relationship must cite evidence in the same proposition and run"
            )
        if kind not in {
            "supports",
            "qualifies",
            "conflicts_with",
            "references",
            "possible_supersession",
        }:
            raise ValueError(f"Unsupported evidence relationship: {kind}")
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO evidence_relations VALUES(?,?,?,?,?,?,'candidate')",
                (
                    run_id,
                    pid,
                    identity(pid, kind, sorted(evidence_ids)),
                    kind,
                    _json(evidence_ids),
                    note,
                ),
            )

    def relations(self, run_id: str, pid: str) -> list[dict]:
        out = []
        for row in self.db.execute(
            "SELECT * FROM evidence_relations WHERE run_id=? AND proposition_id=? ORDER BY relation_id",
            (run_id, pid),
        ):
            item = dict(row)
            item["evidence_ids"] = json.loads(item.pop("evidence_ids_json"))
            out.append(item)
        return out

    def vector(self, chunk: Chunk, key: str) -> list[float] | None:
        row = self.db.execute(
            "SELECT vector_json FROM embedding_cache WHERE chunk_id=? AND source_hash=? AND identity=?",
            (chunk.chunk_id, chunk.source_hash, key),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_vector(self, chunk: Chunk, key: str, vector: list[float]) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO embedding_cache VALUES(?,?,?,?)",
                (chunk.chunk_id, chunk.source_hash, key, _json(vector)),
            )

    def query_vector(self, text: str, key: str, embedder, model: str) -> list[float]:
        digest = identity(text)
        row = self.db.execute(
            "SELECT vector_json FROM query_embedding_cache WHERE query_hash=? AND identity=?",
            (digest, key),
        ).fetchone()
        if row:
            return json.loads(row[0])
        vector = embedder.embed([text], model=model)[0]
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO query_embedding_cache VALUES(?,?,?)",
                (digest, key, _json(vector)),
            )
        return vector

    def run_report(self, run_id: str) -> dict:
        result = super().run_report(run_id)
        row = self.db.execute(
            "SELECT state_json FROM research_state WHERE run_id=?", (run_id,)
        ).fetchone()
        if row:
            state = json.loads(row[0])
            tasks = self.tasks(run_id)
            result["research"] = {
                "stop_reason": state.get("stop_reason"),
                "tasks": {
                    status: sum(t["status"] == status for t in tasks)
                    for status in (
                        "complete",
                        "pending",
                        "running",
                        "failed",
                        "deferred",
                    )
                },
                "propositions": state["progress"],
                "extraction_omissions": state.get("extraction_omissions", []),
            }
            result["synthesis"] = {
                "propositions": len(state["propositions"]),
                "interpretation_status": "candidate",
                "source_spans_verified": len(self.evidence(run_id)),
                "relationships": self.db.execute(
                    "SELECT count(*) FROM evidence_relations WHERE run_id=?", (run_id,)
                ).fetchone()[0],
            }
        return result

    def run_manifest(self, run_id: str) -> dict:
        result = super().run_manifest(run_id)
        if self.db.execute(
            "SELECT 1 FROM research_state WHERE run_id=?", (run_id,)
        ).fetchone():
            state = self.state(run_id)
            result.update(
                research_state=state,
                tasks=self.tasks(run_id),
                evidence_relations=[
                    r
                    for p in state["propositions"]
                    for r in self.relations(run_id, p["id"])
                ],
                bundles=[
                    json.loads(r[0])
                    for r in self.db.execute(
                        "SELECT record_json FROM bundles WHERE run_id=? ORDER BY proposition_id",
                        (run_id,),
                    )
                ],
            )
        return result
