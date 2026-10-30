from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .extract import file_hash
from .index import Index
from .promotion import estimate_tokens
from .store import Store
from .workspace import Workspace


def _record(root: Path, ledger: Workspace, evidence_id: str, run_id=None) -> dict:
    query = "SELECT record_json FROM evidence_records WHERE evidence_id=?"
    args = [evidence_id]
    if run_id:
        query += " AND run_id=?"
        args.append(run_id)
    rows = ledger.db.execute(query + " ORDER BY rowid", args).fetchall()
    records = [json.loads(row[0]) for row in rows]
    if not records and not run_id:
        records = [
            r for r in Store(root).read("evidence.jsonl") if r.get("id") == evidence_id
        ]
    if not records:
        raise ValueError(f"Unknown evidence ID: {evidence_id}")
    rec = dict(records[0])
    try:
        current = file_hash(root / rec["source_path"], 10)
        rec["stale"] = current != rec.get("source_hash")
        rec["current_source_hash"] = current
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
        rec["stale"] = None
        rec["current_source_hash"] = None
    return rec


def _open(path):
    root = Path(path).expanduser().resolve()
    return root, Workspace(Store(root).state)


def _deliver(ledger, rec, kind, payload):
    payload["expansion_tokens_recorded"] = estimate_tokens(payload)
    ledger.record_delivery(rec.get("search_id", "legacy"), kind, payload)
    return payload


def get_evidence(path: str | Path, evidence_id: str, run_id=None) -> dict[str, Any]:
    root, ledger = _open(path)
    try:
        rec = _record(root, ledger, evidence_id, run_id)
        return _deliver(
            ledger,
            rec,
            "get-evidence",
            {k: v for k, v in rec.items() if k != "context"},
        )
    finally:
        ledger.db.close()


def expand_evidence_context(
    path: str | Path, evidence_id: str, radius: int = 2, run_id=None
) -> dict[str, Any]:
    if not isinstance(radius, int) or radius < 0:
        raise ValueError("radius must be a nonnegative integer")
    root, ledger = _open(path)
    try:
        rec = _record(root, ledger, evidence_id, run_id)
        # v3 context always comes from the pinned run, including structural dependencies.
        if rec.get("context_blocks"):
            context = "\n\n".join(b["text"] for b in rec["context_blocks"])
        elif rec.get("context"):
            context = rec["context"]
        else:
            index = Index(root, Store(root))
            try:
                source = [
                    c
                    for c in index.chunks_for_source(rec["source_path"])
                    if c.source_hash == rec["source_hash"]
                ]
                hit = next(
                    (c for c in source if rec.get("excerpt", "") in c.text), None
                )
                context = (
                    "\n\n".join(c.text for c in index.expand(hit, radius))
                    if hit
                    else ""
                )
            finally:
                index.db.close()
        return _deliver(
            ledger,
            rec,
            "expand-context",
            {
                "id": evidence_id,
                "source_path": rec["source_path"],
                "location": _location(rec),
                "radius": radius,
                "context": context,
                "search_id": rec.get("search_id"),
                "source_hash": rec.get("source_hash"),
                "stale": rec["stale"],
                "current_source_hash": rec["current_source_hash"],
            },
        )
    finally:
        ledger.db.close()


def open_source_location(
    path: str | Path, evidence_id: str, run_id=None
) -> dict[str, Any]:
    root, ledger = _open(path)
    try:
        rec = _record(root, ledger, evidence_id, run_id)
        return _deliver(
            ledger,
            rec,
            "open-source-location",
            {
                "id": evidence_id,
                "absolute_path": str(root / rec["source_path"]),
                "source_path": rec["source_path"],
                "location": _location(rec),
                "source_hash": rec.get("source_hash"),
                "current_source_hash": rec["current_source_hash"],
                "stale": rec["stale"],
                "excerpt": rec.get("excerpt"),
            },
        )
    finally:
        ledger.db.close()


def _related(path, evidence_id, limit, adverse):
    root, ledger = _open(path)
    try:
        rec = _record(root, ledger, evidence_id)
        records = [
            json.loads(r[0])
            for r in ledger.db.execute(
                "SELECT record_json FROM evidence_records ORDER BY rowid"
            )
        ]
        if not records:
            records = Store(root).read("evidence.jsonl")
        records = [
            r
            for r in records
            if r.get("id") != evidence_id
            and r.get("research_question") == rec.get("research_question")
            and (
                not adverse
                or r.get("evidence_type") == "contradictory"
                or r.get("contradiction")
            )
        ]
        chosen = {}
        for record in records:
            chosen.setdefault(record["id"], record)
            if len(chosen) >= limit:
                break
        key = "contradictions" if adverse else "related"
        return _deliver(
            ledger,
            rec,
            "contradiction-evidence" if adverse else "related-evidence",
            {"id": evidence_id, key: [_brief(r) for r in chosen.values()]},
        )
    finally:
        ledger.db.close()


def related_evidence(
    path: str | Path, evidence_id: str, limit: int = 8
) -> dict[str, Any]:
    return _related(path, evidence_id, limit, False)


def contradiction_evidence(
    path: str | Path, evidence_id: str, limit: int = 8
) -> dict[str, Any]:
    return _related(path, evidence_id, limit, True)


def telemetry(path: str | Path, search_id: str) -> dict[str, Any]:
    root, ledger = _open(path)
    try:
        row = ledger.db.execute(
            "SELECT state_json FROM research_state WHERE run_id=?", (search_id,)
        ).fetchone()
        if row:
            state = json.loads(row[0])
            chunks = ledger.chunks(state["snapshot_id"])
            calls = [
                dict(r)
                for r in ledger.db.execute(
                    "SELECT * FROM model_calls WHERE run_id=?", (search_id,)
                )
            ]
            paths = {
                r[0]
                for r in ledger.db.execute(
                    "SELECT DISTINCT source_path FROM retrieval_candidates WHERE run_id=? AND stage='retrieved'",
                    (search_id,),
                )
            }
            hashes = sorted({c.source_hash for c in chunks if c.source_path in paths})
            deliveries = ledger.db.execute(
                "SELECT count(*),coalesce(sum(estimated_tokens),0) FROM deliveries WHERE run_id=?",
                (search_id,),
            ).fetchone()
            out = {
                "search_id": search_id,
                "source_files_indexed": len({c.source_path for c in chunks}),
                "qwen_generations": state["total_calls"],
                "candidate_files": len(paths),
                "candidate_source_hashes": len(hashes),
                "candidate_source_hash_list": hashes,
                "estimated_corpus_tokens_indexed": sum(len(c.text) for c in chunks)
                // 4,
                "estimated_local_input_tokens_processed": sum(
                    c["reported_input_tokens"] or 0
                    for c in calls
                    if c["status"] != "cached"
                ),
                "elapsed_local_inference_seconds": round(
                    sum(
                        json.loads(c["usage_json"] or "{}").get("elapsed_seconds", 0)
                        for c in calls
                        if c["status"] != "cached"
                    ),
                    3,
                ),
                "evidence_records_persisted": len(ledger.evidence(search_id)),
                "evidence_records_promoted": 0,
                "qwen_failures": ledger.db.execute(
                    "SELECT count(*) FROM model_failures WHERE run_id=?", (search_id,)
                ).fetchone()[0],
                "failed_model_calls": sum(c["status"] == "failed" for c in calls),
                "additional_source_expansions": deliveries[0],
                "estimated_expansion_tokens": deliveries[1],
                "estimated_total_frontier_research_tokens": deliveries[1],
            }
        else:
            records = [
                r
                for r in Store(root).read("telemetry.jsonl")
                if r.get("search_id") == search_id
            ]
            if not records:
                raise ValueError(f"Unknown search ID: {search_id}")
            out = dict(records[-1])
            expansions = [
                r
                for r in Store(root).read("expansions.jsonl")
                if r.get("search_id") == search_id
            ]
            out.update(
                additional_source_expansions=len(expansions),
                estimated_expansion_tokens=sum(
                    r.get("estimated_tokens", 0) for r in expansions
                ),
            )
            out["estimated_total_frontier_research_tokens"] = (
                out.get("estimated_promoted_tokens", 0)
                + out["estimated_expansion_tokens"]
            )
        out["estimate_note"] = (
            "Token and compression figures are approximate research-context estimates, not API billing or exact Codex usage."
        )
        return out
    finally:
        ledger.db.close()


def coverage_report(path: str | Path, run_id: str) -> dict[str, Any]:
    _, ledger = _open(path)
    try:
        report = ledger.run_report(run_id)
        report["scope_statement"] = (
            "All scheduled retrieval candidates received a disposition; this does not mean the full corpus was reviewed."
            if report["status"] == "complete_for_retrieved_scope"
            else "The run is incomplete. No completeness claim is permitted."
        )
        return report
    finally:
        ledger.db.close()


def run_manifest(path: str | Path, run_id: str) -> dict[str, Any]:
    _, ledger = _open(path)
    try:
        return ledger.run_manifest(run_id)
    finally:
        ledger.db.close()


def _location(rec):
    keys = (
        "page",
        "sheet",
        "section",
        "line_start",
        "line_end",
        "row_start",
        "row_end",
        "paragraph_start",
        "paragraph_end",
    )
    return {k: rec[k] for k in keys if rec.get(k) is not None}


def _brief(rec):
    return {
        "id": rec.get("id"),
        "evidence_type": rec.get("evidence_type"),
        "stage": rec.get("stage"),
        "source_path": rec.get("source_path"),
        "location": _location(rec),
        "excerpt": rec.get("excerpt"),
        "qualification": rec.get("qualification"),
        "contradiction": rec.get("contradiction"),
    }
