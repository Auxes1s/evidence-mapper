from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import load_config
from .index import Index
from .promotion import estimate_tokens
from .store import Store

def _root(path: str | Path) -> tuple[Path, Store, Index]:
    root = Path(path).expanduser().resolve()
    store = Store(root)
    return root, store, Index(root, store)

def _record(store: Store, evidence_id: str) -> dict[str, Any]:
    matches = [r for r in store.read("evidence.jsonl") if r.get("id") == evidence_id]
    if not matches: raise ValueError(f"Unknown evidence ID: {evidence_id}")
    # The first persisted occurrence is the canonical record that created the stable ID.
    return matches[0]

def _log_expansion(store: Store, record: dict[str, Any], kind: str, payload: Any) -> int:
    tokens = estimate_tokens(payload)
    store.append("expansions.jsonl", {"search_id": record.get("search_id"), "evidence_id": record.get("id"),
                 "kind": kind, "estimated_tokens": tokens, "timestamp": datetime.now(timezone.utc).isoformat()})
    return tokens

def get_evidence(path: str | Path, evidence_id: str) -> dict[str, Any]:
    _, store, _ = _root(path); rec = _record(store, evidence_id)
    payload = {k:v for k,v in rec.items() if k != "context"}
    payload["expansion_tokens_recorded"] = _log_expansion(store, rec, "get-evidence", payload)
    return payload

def expand_evidence_context(path: str | Path, evidence_id: str, radius: int = 2) -> dict[str, Any]:
    _, store, index = _root(path); rec = _record(store, evidence_id)
    source_chunks = index.chunks_for_source(rec["source_path"])
    normalized_excerpt = re.sub(r"\s+", " ", rec.get("excerpt", "")).lower()
    hit = next((c for c in source_chunks if normalized_excerpt in re.sub(r"\s+", " ", c.text).lower()), None)
    if hit:
        chunks = index.expand(hit, radius)
        context = "\n\n".join(c.text for c in chunks)
    else:
        context = rec.get("context", "")
    payload = {"id": evidence_id, "source_path": rec["source_path"], "location": _location(rec),
               "radius": radius, "context": context}
    payload["expansion_tokens_recorded"] = _log_expansion(store, rec, "expand-context", payload)
    return payload

def open_source_location(path: str | Path, evidence_id: str) -> dict[str, Any]:
    root, store, _ = _root(path); rec = _record(store, evidence_id)
    payload = {"id": evidence_id, "absolute_path": str(root / rec["source_path"]),
               "source_path": rec["source_path"], "location": _location(rec),
               "source_hash": rec.get("source_hash"), "excerpt": rec.get("excerpt")}
    payload["expansion_tokens_recorded"] = _log_expansion(store, rec, "open-source-location", payload)
    return payload

def related_evidence(path: str | Path, evidence_id: str, limit: int = 8) -> dict[str, Any]:
    _, store, _ = _root(path); rec = _record(store, evidence_id)
    records = [r for r in store.read("evidence.jsonl") if r.get("id") != evidence_id]
    same_question = [r for r in records if r.get("research_question") == rec.get("research_question")]
    same_topic = [r for r in records if r.get("topic") == rec.get("topic")]
    chosen=[]
    for r in same_question + same_topic:
        if r.get("id") not in {x.get("id") for x in chosen}: chosen.append(r)
        if len(chosen)>=limit: break
    payload={"id":evidence_id,"related":[_brief(r) for r in chosen]}
    payload["expansion_tokens_recorded"] = _log_expansion(store, rec, "related-evidence", payload)
    return payload

def contradiction_evidence(path: str | Path, evidence_id: str, limit: int = 8) -> dict[str, Any]:
    _, store, _ = _root(path); rec = _record(store, evidence_id)
    chosen=[r for r in store.read("evidence.jsonl")
            if r.get("research_question")==rec.get("research_question") and r.get("id")!=evidence_id
            and (r.get("evidence_type")=="contradictory" or r.get("contradiction"))][:limit]
    payload={"id":evidence_id,"contradictions":[_brief(r) for r in chosen]}
    payload["expansion_tokens_recorded"] = _log_expansion(store, rec, "contradiction-evidence", payload)
    return payload

def telemetry(path: str | Path, search_id: str) -> dict[str, Any]:
    _, store, _ = _root(path)
    records=[r for r in store.read("telemetry.jsonl") if r.get("search_id")==search_id]
    if not records: raise ValueError(f"Unknown search ID: {search_id}")
    out=dict(records[-1])
    expansions=[x for x in store.read("expansions.jsonl") if x.get("search_id")==search_id]
    out["additional_source_expansions"] = len(expansions)
    out["estimated_expansion_tokens"] = sum(int(x.get("estimated_tokens") or 0) for x in expansions)
    frontier=out.get("estimated_promoted_tokens",0)+out["estimated_expansion_tokens"]
    out["estimated_total_frontier_research_tokens"] = frontier
    corpus=out.get("estimated_corpus_tokens_indexed",0)
    out["estimated_corpus_to_frontier_compression"] = round(corpus/frontier,2) if frontier else None
    out["estimate_note"] = "Token and compression figures are approximate research-context estimates, not API billing or exact Codex usage."
    return out

def _location(rec):
    keys=("page","sheet","section","line_start","line_end","row_start","row_end","paragraph_start","paragraph_end")
    return {k:rec[k] for k in keys if rec.get(k) is not None}

def _brief(rec):
    return {"id":rec.get("id"),"evidence_type":rec.get("evidence_type"),"stage":rec.get("stage"),
            "source_path":rec.get("source_path"),"location":_location(rec),"excerpt":rec.get("excerpt"),
            "qualification":rec.get("qualification"),"contradiction":rec.get("contradiction")}
