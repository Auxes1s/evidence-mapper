from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

from .index import Index
from .models import Chunk, Evidence
from .prompts import PROMPT_VERSION, task_instruction
from .store import Store

MODES = {"inventory","evidence","contradictions","chronology","exhaustive","gap-search","source-trace"}
DEPTH_PASSES = {"quick": 2, "standard": 4, "deep": 7}
STRATEGIES = ["direct terminology", "synonyms and alternate phrasings", "entity and acronym variants",
              "date and chronology", "contradiction and qualification", "source tracing", "unresolved gap"]
STOP = {"what","when","where","which","that","this","with","from","into","does","did","was","were","have","has","find","evidence","claim","project"}

def _terms(question: str, strategy: str) -> list[str]:
    quoted = re.findall(r'["“]([^"”]+)["”]', question)
    words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]+", question.lower()) if len(w)>2 and w not in STOP]
    phrases = quoted + [" ".join(words[i:i+3]) for i in range(max(0, len(words)-2))] + words
    if "date" in strategy or "chronology" in strategy: phrases += re.findall(r"\b(?:19|20)\d{2}\b", question) + ["approved", "began", "operational", "completed"]
    if "contradiction" in strategy: phrases += ["however", "incorrect", "revised", "not completed", "delayed", "correction"]
    if "source tracing" in strategy: phrases += ["approved", "signed", "minutes", "memorandum", "resolution"]
    if "synonyms" in strategy: phrases += ["finding", "result", "outcome", "effect", "conclusion", "reported", "observed"]
    if "entity" in strategy: phrases += [w.upper() for w in words if len(w) <= 8]
    if strategy.startswith("model-suggested:"):
        phrases += [x.strip() for x in strategy.split(":",1)[1].split("|") if x.strip()]
    return list(dict.fromkeys(x for x in phrases if x))[:64]

def _candidate_prompt(question: str, mode: str, strategy: str, chunks: list[Chunk], existing: str | None,
                      propositions: list[tuple[str,str]] | None = None) -> str:
    parts = [f"RESEARCH QUESTION: {question}", f"MODE: {mode}", f"STRATEGY: {strategy}", task_instruction(mode)]
    if propositions:
        parts.append("PROPOSITIONS:\n"+"\n".join(f"{pid}: {text}" for pid,text in propositions))
    parts.append('Return all relevant findings in these candidates, bounded to 4 per candidate. Preserve adverse evidence. Each text label such as [S000] supplies the span_id "S000"; copy that ID into the finding. Never leave span_id empty. Assign the supplied proposition_id. Return one disposition for every candidate chunk_id. Do not copy or rewrite quotations.')
    if existing: parts.append("EXISTING EVIDENCE OR GAP DESCRIPTION:\n" + existing)
    parts.append("CANDIDATES:")
    for c in chunks:
        meta = {k:v for k,v in c.dict().items() if k not in {"text","metadata"} and v not in (None,{},[])}
        meta["parent_id"]=c.metadata.get("parent_id")
        selected=c.metadata.get("selected_span_ids")
        meta["allowed_span_ids"]=[s["span_id"] for s in _candidate_spans(c)
                                  if selected is None or s["span_id"] in selected]
        rendered = "\n".join(f"[{s['span_id']}] {s['text']}" for s in _candidate_spans(c)
                             if selected is None or s["span_id"] in selected)
        parts.append(f"--- CANDIDATE {json.dumps(meta, ensure_ascii=False)} ---\nTEXT:\n{rendered}")
    return "\n\n".join(parts)

def _fit_chunks(chunks: list[Chunk], max_chars: int) -> list[Chunk]:
    out, used = [], 0
    for chunk in chunks:
        cost = len(chunk.text) + 500
        if out and used + cost > max_chars: break
        out.append(chunk); used += cost
    return out

def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()

def _normalize_worker_finding(finding: dict[str, Any]) -> dict[str, Any]:
    """Tolerate small-model schema aliases while preserving quote validation."""
    out = dict(finding)
    if not out.get("excerpt"):
        out["excerpt"] = out.get("evidence") or out.get("quote") or out.get("text") or ""
    out.setdefault("topic", "candidate evidence")
    out.setdefault("evidence_type", "contextual")
    out.setdefault("stage", "other")
    out.setdefault("relevance", "candidate")
    out.setdefault("qualification", "")
    out.setdefault("contradiction", "")
    out.setdefault("short_note", "Exact candidate passage returned by the local worker.")
    for key in ("qualification","contradiction"):
        if str(out[key]).strip().lower() in {"none","n/a","not applicable","null"}:
            out[key]=""
    return out

def _decompose_claim(question: str) -> list[str]:
    parts = re.split(r"\s*;\s*|\s*,\s*(?:(?:but|while|whereas|and)\s+)?(?=[A-Za-z])|\s+and\s+(?=(?:the|it|they|this|that)\b)", question, flags=re.I)
    cleaned = [p.strip(" .") for p in parts if len(p.strip(" .")) >= 8]
    return cleaned if len(cleaned) > 1 else [question]

def _candidate_spans(chunk: Chunk) -> list[dict[str, Any]]:
    """Deterministic sentence/line spans that the worker can select by ID."""
    boundaries = list(re.finditer(r"[^\n]+?(?:[.!?]+(?=\s|$)|(?=\n)|$)", chunk.text))
    out = []
    for match in boundaries:
        text = match.group().strip()
        if not text: continue
        start = match.start() + len(match.group()) - len(match.group().lstrip())
        out.append({"span_id":f"S{len(out):03d}", "text":text,
                    "start":start, "end":start + len(text)})
    return out or [{"span_id":"S000", "text":chunk.text,
                    "start":0, "end":len(chunk.text)}]

def _diverse_candidates(index: Index, propositions: list[str], strategy: str, limit: int,
                        *, with_omitted: bool = False, embedding_model: str | None = None,
                        embedder=None):
    pools = [(index.hybrid_search(_terms(p,strategy),p,limit+1,embedding_model,embedder)
              if embedding_model else index.search(_terms(p, strategy), limit + 1)) for p in propositions]
    out, chunk_ids, canonical_path_by_hash = [], set(), {}
    while any(pools) and len(out) < limit:
        for pool in pools:
            while pool:
                chunk = pool.pop(0)
                if chunk.chunk_id in chunk_ids: continue
                canonical=canonical_path_by_hash.get(chunk.source_hash)
                if canonical is not None and canonical != chunk.source_path: continue
                canonical_path_by_hash.setdefault(chunk.source_hash,chunk.source_path)
                chunk_ids.add(chunk.chunk_id); out.append(chunk); break
            if len(out) >= limit: break
    omitted=[]
    seen=set(chunk_ids)
    for pool in pools:
        for chunk in pool:
            if chunk.chunk_id not in seen:
                omitted.append(chunk); seen.add(chunk.chunk_id)
    return (out,omitted) if with_omitted else out

def _evidence_from_finding(f: dict, chunk: Chunk, question: str, model: str, search_id: str,
                           context: str, spans: list[dict[str, Any]] | None = None,
                           *, call_id: str = "", proposition_id: str = "") -> Evidence | None:
    excerpt = str(f.get("excerpt", "")).strip()
    flags = []; selected_span=None
    if f.get("span_id"):
        selected = {s["span_id"]:s for s in (spans or _candidate_spans(chunk))}.get(str(f["span_id"]))
        if not selected: return None
        selected_span=selected
        excerpt = selected["text"]
        flags.append("deterministic_span_selection")
    if not excerpt:
        return None
    verified = _normalize(excerpt) in _normalize(chunk.text)
    if not verified and re.search(r"(?:\.{3,}|…)", excerpt):
        segments = [x.strip(" \t\r\n\"'.,;:()[]") for x in re.split(r"(?:\.{3,}|…)", excerpt)]
        exact = [x for x in segments if len(x) >= 30 and _normalize(x) in _normalize(chunk.text)]
        if exact:
            excerpt = max(exact, key=len)
            verified = True
            flags.append("excerpt_trimmed_to_contiguous_exact_segment")
    if not verified:
        flags.append("excerpt_not_found_in_candidate")
        return None
    allowed_types = {"direct","indirect","contextual","retrospective","contradictory","qualifying"}
    allowed_stages = {"discussion","proposal","approval","funding","initiation","implementation","operation","completion","handover","institutionalization","closure","other"}
    et = f.get("evidence_type", "contextual"); stage = f.get("stage", "other")
    if et not in allowed_types: flags.append("unsupported_evidence_type"); et = "contextual"
    if stage not in allowed_stages: flags.append("unsupported_stage"); stage = "other"
    now = datetime.now(timezone.utc).isoformat()
    span_id = str(f.get("span_id", ""))
    eid = hashlib.sha256(f"{question}\x1f{proposition_id}\x1f{chunk.chunk_id}\x1f{span_id}\x1f{excerpt}".encode()).hexdigest()[:24]
    return Evidence(eid, question, str(f.get("topic","")), chunk.source_path, chunk.source_type,
        excerpt, context, str(f.get("relevance","candidate")), str(f.get("short_note","")), model,
        now, chunk.source_hash, search_id, page=chunk.page, sheet=chunk.sheet, section=chunk.section,
        line_start=chunk.line_start, line_end=chunk.line_end, row_start=chunk.row_start, row_end=chunk.row_end,
        paragraph_start=chunk.paragraph_start, paragraph_end=chunk.paragraph_end,
        document_date=f.get("document_date"), event_date=f.get("event_date"), evidence_type=et, stage=stage,
        qualification=str(f.get("qualification","")), contradiction=str(f.get("contradiction","")),
        quote_verified=True, validation_flags=flags, fingerprint=eid,
        proposition_id=proposition_id, chunk_id=chunk.chunk_id, span_id=span_id,
        call_id=call_id, prompt_version=PROMPT_VERSION,
        span_char_start=selected_span["start"] if selected_span else None,
        span_char_end=selected_span["end"] if selected_span else None)

def _reuse(store: Store, index: Index, question: str) -> list[dict]:
    hashes = index.source_hashes(); out = []
    for rec in store.read("evidence.jsonl"):
        if _normalize(rec.get("research_question","")) != _normalize(question): continue
        rec = dict(rec); rec["stale"] = hashes.get(rec.get("source_path")) != rec.get("source_hash")
        if not rec["stale"]: out.append(rec)
    return out

def _inventory(index: Index, store: Store, stats: dict) -> dict:
    sources = store.read("source_index.jsonl")
    families: dict[str,int] = {}
    for s in sources: families[s["source_type"]] = families.get(s["source_type"],0)+1
    years, entities = set(), {}
    total_chunks=index.text_stats()["chunks"]
    scanned=index.all_chunks(limit=300)
    for c in scanned:
        years.update(re.findall(r"\b(?:19|20)\d{2}\b", c.text))
        for name in re.findall(r"\b[A-Z][A-Za-z&.-]+(?:\s+[A-Z][A-Za-z&.-]+){1,4}\b", c.text): entities[name] = entities.get(name,0)+1
    likely = [s["source_path"] for s in sources if re.search(r"signed|approved|minutes|resolution|report|official|memorandum", s["source_path"], re.I)][:12]
    return {"file_families": families, "likely_authoritative_sources": likely,
            "date_range": [min(years),max(years)] if years else [],
            "major_entities": [x[0] for x in sorted(entities.items(), key=lambda x:x[1], reverse=True)[:12]],
            "metadata_chunks_scanned":len(scanned),"metadata_chunks_total":total_chunks,
            "metadata_scan_complete":len(scanned)==total_chunks,
            "extraction_failures": stats["extraction_failures"]}


def _snapshot_identity(root: Path, cfg: dict[str, Any], manifest: list[dict[str, Any]]) -> tuple[str,str,dict[str,Any]]:
    policy={k:cfg.get(k) for k in ("include","exclude","ignore_dirs","max_file_bytes","chunk_chars",
                                    "chunk_overlap_chars","ocr_image_only_pdfs","ocr_max_pages")}
    payload=json.dumps({"root":str(root),"policy":policy,"files":manifest},ensure_ascii=False,
                       sort_keys=True,separators=(",",":"))
    digest=hashlib.sha256(payload.encode()).hexdigest()
    return digest[:24],digest,policy


def _proposition_pairs(question: str) -> list[tuple[str,str]]:
    return [(f"P{i:03d}", text) for i,text in enumerate(_decompose_claim(question),1)]


def _assign_proposition(finding: dict[str,Any], pairs: list[tuple[str,str]], excerpt: str) -> tuple[str,bool]:
    supplied=str(finding.get("proposition_id", ""))
    valid={pid for pid,_ in pairs}
    if supplied in valid:
        return supplied,False
    words=set(re.findall(r"[a-z0-9]{3,}",excerpt.lower()))
    ranked=sorted(((len(words & set(re.findall(r"[a-z0-9]{3,}",text.lower()))),pid) for pid,text in pairs),reverse=True)
    return (ranked[0][1] if ranked else "P000"),True


def _build_claims(run_id: str, pairs: list[tuple[str,str]], records: list[dict[str,Any]],
                  source_hashes: dict[str,str]) -> list[dict[str,Any]]:
    claims=[]
    for pid,text in pairs:
        related=[r for r in records if r.get("proposition_id")==pid]
        support=[r["id"] for r in related if r.get("evidence_type") in {"direct","indirect","contextual","retrospective"}]
        contradiction=[r["id"] for r in related if r.get("evidence_type")=="contradictory" or r.get("contradiction")]
        qualification=[r["id"] for r in related if r.get("evidence_type")=="qualifying" or r.get("qualification")]
        if support and contradiction: status="mixed_evidence"
        elif support: status="supporting_evidence_present"
        elif contradiction: status="contradicting_evidence_present"
        else: status="unresolved"
        verified=bool(related) and all(r.get("quote_verified") and r.get("chunk_id") and
            source_hashes.get(r.get("source_path"))==r.get("source_hash") for r in related)
        claims.append({"claim_id":hashlib.sha256(f"{text}".encode()).hexdigest()[:24],
                       "proposition_id":pid,"claim_text":text,"status":status,
                       "verification_status":"verified" if verified else "unresolved",
                       "verification_scope":"source_links_and_exact_spans_only",
                       "evidence":{"support":support,"qualification":qualification,"contradiction":contradiction}})
    return claims

def research(path, question=None, mode="inventory", depth="standard", *, fresh=False,
             challenge_existing=False, existing_evidence=None, config_path=None,
             backend=None, rebuild=False, output_budget=None, propositions=None, resume=None):
    """Run bounded research and return a v3 overview; fetch evidence with get_bundle."""
    from .engine import run_research
    return run_research(path,question,mode,depth,fresh=fresh,challenge_existing=challenge_existing,
                        existing_evidence=existing_evidence,config_path=config_path,backend=backend,
                        rebuild=rebuild,output_budget=output_budget,propositions=propositions,resume=resume)
