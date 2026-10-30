from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


AUDIT_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS runs (
 run_id TEXT PRIMARY KEY, status TEXT NOT NULL, started_at TEXT NOT NULL,
 completed_at TEXT, question TEXT, mode TEXT, depth TEXT, corpus_snapshot_id TEXT,
 config_json TEXT NOT NULL, prompt_version TEXT NOT NULL, model_json TEXT NOT NULL,
 incomplete_reasons_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS corpus_snapshots (
 snapshot_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, root TEXT NOT NULL,
 policy_json TEXT NOT NULL, manifest_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS corpus_files (
 snapshot_id TEXT NOT NULL, source_path TEXT NOT NULL, status TEXT NOT NULL,
 reason TEXT, source_type TEXT, size INTEGER, mtime REAL, source_hash TEXT,
 extracted_text_hash TEXT, parser_version TEXT, chunk_count INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(snapshot_id, source_path)
);
CREATE TABLE IF NOT EXISTS retrieval_candidates (
 run_id TEXT NOT NULL, strategy TEXT NOT NULL, query_id TEXT NOT NULL,
 rank INTEGER NOT NULL, chunk_id TEXT NOT NULL, source_path TEXT NOT NULL,
 score REAL, stage TEXT NOT NULL, reason TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
 PRIMARY KEY(run_id, strategy, query_id, chunk_id, stage)
);
CREATE TABLE IF NOT EXISTS model_calls (
 call_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, strategy TEXT NOT NULL,
 prompt_hash TEXT NOT NULL, prompt_version TEXT NOT NULL, token_method TEXT NOT NULL,
 preflight_input_tokens INTEGER NOT NULL, context_tokens INTEGER NOT NULL,
 output_reserved_tokens INTEGER NOT NULL, safety_margin_tokens INTEGER NOT NULL,
 reported_input_tokens INTEGER, reported_output_tokens INTEGER, done_reason TEXT,
 status TEXT NOT NULL, chunk_ids_json TEXT NOT NULL, model_json TEXT NOT NULL,
 response_json TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS call_cache (
 prompt_hash TEXT NOT NULL, model_json TEXT NOT NULL, response_json TEXT NOT NULL,
 usage_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(prompt_hash, model_json)
);
CREATE TABLE IF NOT EXISTS candidate_dispositions (
 run_id TEXT NOT NULL, call_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
 status TEXT NOT NULL, evidence_ids_json TEXT NOT NULL DEFAULT '[]', reason TEXT,
 PRIMARY KEY(run_id, call_id, chunk_id)
);
CREATE TABLE IF NOT EXISTS evidence_records (
 run_id TEXT NOT NULL, evidence_id TEXT NOT NULL, proposition_id TEXT,
 chunk_id TEXT NOT NULL, call_id TEXT NOT NULL, record_json TEXT NOT NULL,
 PRIMARY KEY(run_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS claims (
 run_id TEXT NOT NULL, claim_id TEXT NOT NULL, proposition_id TEXT NOT NULL,
 claim_text TEXT NOT NULL, status TEXT NOT NULL, verification_status TEXT NOT NULL,
 PRIMARY KEY(run_id, claim_id)
);
CREATE TABLE IF NOT EXISTS claim_evidence (
 run_id TEXT NOT NULL, claim_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
 stance TEXT NOT NULL, PRIMARY KEY(run_id, claim_id, evidence_id)
);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditStore:
    """Transactional, queryable run provenance. JSONL remains an export surface."""

    def __init__(self, state: Path):
        self.path = state / "audit.sqlite3"
        existed = self.path.exists()
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        if existed and self.db.execute("PRAGMA user_version").fetchone()[0] < 3:
            backup = state / "audit.before-v3.sqlite3"
            if not backup.exists():
                with sqlite3.connect(backup) as target:
                    self.db.backup(target)
        self.db.executescript(AUDIT_SCHEMA)
        columns={r[1] for r in self.db.execute("PRAGMA table_info(model_calls)")}
        if "response_json" not in columns:
            self.db.execute("ALTER TABLE model_calls ADD COLUMN response_json TEXT")
        if "request_json" not in columns:
            self.db.execute("ALTER TABLE model_calls ADD COLUMN request_json TEXT")
        if "usage_json" not in columns:
            self.db.execute("ALTER TABLE model_calls ADD COLUMN usage_json TEXT")
        retrieval_columns={r[1] for r in self.db.execute("PRAGMA table_info(retrieval_candidates)")}
        if "metadata_json" not in retrieval_columns:
            self.db.execute("ALTER TABLE retrieval_candidates ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
        corpus_columns={r[1] for r in self.db.execute("PRAGMA table_info(corpus_files)")}
        if "parser_version" not in corpus_columns:
            self.db.execute("ALTER TABLE corpus_files ADD COLUMN parser_version TEXT")
        self.db.commit()

    def start_run(self, run_id: str, *, question: str | None, mode: str, depth: str,
                  config: dict[str, Any], prompt_version: str, model: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO runs(run_id,status,started_at,question,mode,depth,config_json,prompt_version,model_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, "running", utcnow(), question, mode, depth, _json(config), prompt_version, _json(model)),
        )
        self.db.commit()

    def record_snapshot(self, run_id: str, snapshot_id: str, root: Path, policy: dict[str, Any],
                        manifest_hash: str, files: Iterable[dict[str, Any]]) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO corpus_snapshots VALUES(?,?,?,?,?)",
                            (snapshot_id, utcnow(), str(root), _json(policy), manifest_hash))
            for item in files:
                self.db.execute(
                    """INSERT OR REPLACE INTO corpus_files
                    (snapshot_id,source_path,status,reason,source_type,size,mtime,source_hash,extracted_text_hash,parser_version,chunk_count)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (snapshot_id, item["source_path"], item["status"], item.get("reason"),
                     item.get("source_type"), item.get("size"), item.get("mtime"),
                     item.get("source_hash"), item.get("extracted_text_hash"), item.get("parser_version"),
                     int(item.get("chunk_count") or 0)),
                )
            self.db.execute("UPDATE runs SET corpus_snapshot_id=? WHERE run_id=?", (snapshot_id, run_id))

    def record_candidates(self, rows: Iterable[dict[str, Any]]) -> None:
        with self.db:
            for row in rows:
                self.db.execute(
                    """INSERT OR REPLACE INTO retrieval_candidates
                    (run_id,strategy,query_id,rank,chunk_id,source_path,score,stage,reason,metadata_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (row["run_id"], row["strategy"], row["query_id"], row["rank"], row["chunk_id"],
                     row["source_path"], row.get("score"), row["stage"], row.get("reason"),_json(row.get("metadata",{}))),
                )

    def start_call(self, row: dict[str, Any]) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO model_calls
                (call_id,run_id,strategy,prompt_hash,prompt_version,token_method,preflight_input_tokens,
                 context_tokens,output_reserved_tokens,safety_margin_tokens,status,chunk_ids_json,model_json,request_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["call_id"], row["run_id"], row["strategy"], row["prompt_hash"], row["prompt_version"],
                 row["token_method"], row["preflight_input_tokens"], row["context_tokens"],
                 row["output_reserved_tokens"], row["safety_margin_tokens"], "running",
                 _json(row["chunk_ids"]), _json(row["model"]), _json(row.get("request"))),
            )

    def finish_call(self, call_id: str, *, status: str, usage: dict[str, Any] | None = None,
                    response: dict[str, Any] | None = None, error: str | None = None) -> None:
        usage = usage or {}
        with self.db:
            self.db.execute(
                """UPDATE model_calls SET status=?,reported_input_tokens=?,reported_output_tokens=?,
                done_reason=?,response_json=?,error=?,usage_json=? WHERE call_id=?""",
                (status, usage.get("input_tokens"), usage.get("output_tokens"),
                 usage.get("done_reason"), _json(response) if response is not None else None, error,
                 _json({k:v for k,v in usage.items() if k not in {"request","response_content"}}),call_id),
            )

    def cached_call(self, prompt_hash: str, model: dict[str, Any]) -> tuple[dict[str,Any],dict[str,Any]] | None:
        row=self.db.execute("SELECT response_json,usage_json FROM call_cache WHERE prompt_hash=? AND model_json=?",
                            (prompt_hash,_json(model))).fetchone()
        return (json.loads(row[0]),json.loads(row[1])) if row else None

    def cache_call(self, prompt_hash: str, model: dict[str, Any], response: dict[str,Any], usage: dict[str,Any]) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO call_cache VALUES(?,?,?,?,?)",
                            (prompt_hash,_json(model),_json(response),_json(usage),utcnow()))

    def discard_call_cache(self,prompt_hash: str,model: dict) -> None:
        with self.db:
            self.db.execute("DELETE FROM call_cache WHERE prompt_hash=? AND model_json=?",(prompt_hash,_json(model)))

    def record_dispositions(self, rows: Iterable[dict[str, Any]]) -> None:
        with self.db:
            for row in rows:
                self.db.execute(
                    "INSERT OR REPLACE INTO candidate_dispositions VALUES(?,?,?,?,?,?)",
                    (row["run_id"], row["call_id"], row["chunk_id"], row["status"],
                     _json(row.get("evidence_ids", [])), row.get("reason")),
                )

    def record_evidence(self, run_id: str, records: Iterable[dict[str, Any]]) -> None:
        with self.db:
            for rec in records:
                self.db.execute(
                    "INSERT OR REPLACE INTO evidence_records VALUES(?,?,?,?,?,?)",
                    (run_id, rec["id"], rec.get("proposition_id"), rec.get("chunk_id", ""),
                     rec.get("call_id", ""), _json(rec)),
                )

    def record_claims(self, run_id: str, claims: Iterable[dict[str, Any]]) -> None:
        with self.db:
            for claim in claims:
                self.db.execute("INSERT OR REPLACE INTO claims VALUES(?,?,?,?,?,?)",
                    (run_id, claim["claim_id"], claim["proposition_id"], claim["claim_text"],
                     claim["status"], claim["verification_status"]))
                self.db.execute("DELETE FROM claim_evidence WHERE run_id=? AND claim_id=?",
                                (run_id, claim["claim_id"]))
                for stance, ids in claim.get("evidence", {}).items():
                    for evidence_id in ids:
                        self.db.execute("INSERT INTO claim_evidence VALUES(?,?,?,?)",
                                        (run_id, claim["claim_id"], evidence_id, stance))

    def finish_run(self, run_id: str, status: str, reasons: list[str]) -> None:
        with self.db:
            self.db.execute("UPDATE runs SET status=?,completed_at=?,incomplete_reasons_json=? WHERE run_id=?",
                            (status, utcnow(), _json(reasons), run_id))

    def run_report(self, run_id: str) -> dict[str, Any]:
        run = self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not run:
            raise ValueError(f"Unknown run ID: {run_id}")
        def count(sql: str) -> int:
            return int(self.db.execute(sql, (run_id,)).fetchone()[0])
        snapshot = run["corpus_snapshot_id"]
        statuses = {}
        if snapshot:
            statuses = {r[0]: int(r[1]) for r in self.db.execute(
                "SELECT status,count(*) FROM corpus_files WHERE snapshot_id=? GROUP BY status", (snapshot,))}
        return {
            "run_id": run_id, "status": run["status"], "corpus_snapshot_id": snapshot,
            "incomplete_reasons": json.loads(run["incomplete_reasons_json"]),
            "corpus": statuses,
            "retrieval": {"unique_chunks_retrieved": count("SELECT count(DISTINCT chunk_id) FROM retrieval_candidates WHERE run_id=? AND stage='retrieved'"),
                          "retrieval_instances": count("SELECT count(*) FROM retrieval_candidates WHERE run_id=? AND stage='retrieved'"),
                          "unique_chunks_scheduled": count("SELECT count(DISTINCT chunk_id) FROM retrieval_candidates WHERE run_id=? AND stage='scheduled'"),
                          "scheduled_instances": count("SELECT count(*) FROM retrieval_candidates WHERE run_id=? AND stage='scheduled'"),
                          "omitted_by_retrieval_limit": count("SELECT count(*) FROM retrieval_candidates WHERE run_id=? AND stage='not_scheduled'")},
            "model_processing": {
                "calls": count("SELECT count(*) FROM model_calls WHERE run_id=?"),
                "unique_chunks_with_dispositions": count("SELECT count(DISTINCT chunk_id) FROM candidate_dispositions WHERE run_id=?"),
                "processing_instances_with_dispositions": count("SELECT count(*) FROM candidate_dispositions WHERE run_id=?"),
                "failed_or_unacknowledged": count("SELECT count(*) FROM candidate_dispositions WHERE run_id=? AND status IN ('failed','unacknowledged','omitted')"),
            },
            "evidence_ledger": {"records": count("SELECT count(*) FROM evidence_records WHERE run_id=?")},
            "synthesis": {"claims": count("SELECT count(*) FROM claims WHERE run_id=?"),
                          "unverified_claims": count("SELECT count(*) FROM claims WHERE run_id=? AND verification_status!='verified'")},
        }

    def run_manifest(self, run_id: str) -> dict[str, Any]:
        run=self.db.execute("SELECT * FROM runs WHERE run_id=?",(run_id,)).fetchone()
        if not run: raise ValueError(f"Unknown run ID: {run_id}")
        snapshot=run["corpus_snapshot_id"]
        run_row=dict(run)
        for key in ("config_json","model_json","incomplete_reasons_json"):
            run_row[key.removesuffix("_json")]=json.loads(run_row.pop(key))
        files=[dict(r) for r in self.db.execute(
            "SELECT * FROM corpus_files WHERE snapshot_id=? ORDER BY source_path",(snapshot,))] if snapshot else []
        candidates=[]
        for row in self.db.execute("SELECT * FROM retrieval_candidates WHERE run_id=? ORDER BY strategy,query_id,stage,rank",(run_id,)):
            item=dict(row); item["metadata"]=json.loads(item.pop("metadata_json")); candidates.append(item)
        calls=[]
        for row in self.db.execute("SELECT * FROM model_calls WHERE run_id=? ORDER BY rowid",(run_id,)):
            item=dict(row); item["chunk_ids"]=json.loads(item.pop("chunk_ids_json")); item["model"]=json.loads(item.pop("model_json"))
            item["request"]=json.loads(item.pop("request_json")) if item.get("request_json") else None
            item["usage"]=json.loads(item.pop("usage_json")) if item.get("usage_json") else {}
            if item.get("response_json") is not None: item["response"]=json.loads(item.pop("response_json"))
            else: item.pop("response_json",None)
            calls.append(item)
        dispositions=[]
        for row in self.db.execute("SELECT * FROM candidate_dispositions WHERE run_id=? ORDER BY call_id,chunk_id",(run_id,)):
            item=dict(row); item["evidence_ids"]=json.loads(item.pop("evidence_ids_json")); dispositions.append(item)
        evidence=[json.loads(r[0]) for r in self.db.execute(
            "SELECT record_json FROM evidence_records WHERE run_id=? ORDER BY evidence_id",(run_id,))]
        claims=[]
        for row in self.db.execute("SELECT * FROM claims WHERE run_id=? ORDER BY proposition_id",(run_id,)):
            item=dict(row); item["verification_scope"]="source_links_and_exact_spans_only"
            links=self.db.execute("SELECT stance,evidence_id FROM claim_evidence WHERE run_id=? AND claim_id=? ORDER BY stance,evidence_id",
                                                 (run_id,item["claim_id"]))
            item["evidence_links"]=[{"stance":x[0],"evidence_id":x[1]} for x in links]; claims.append(item)
        return {"run":run_row,"corpus_files":files,"retrieval_candidates":candidates,
                "model_calls":calls,"candidate_dispositions":dispositions,
                "evidence_records":evidence,"claims":claims}
