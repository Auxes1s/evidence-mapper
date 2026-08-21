from __future__ import annotations

import hashlib, json, re, time, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ensure_repo_config, load_config
from .index import Index
from .models import Chunk, Evidence
from .ollama import Backend, OllamaBackend
from .prompts import FINDINGS_SCHEMA, SYSTEM, task_instruction
from .promotion import compact_record, estimate_tokens, promote
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
    return list(dict.fromkeys(x for x in phrases if x))[:24]

def _candidate_prompt(question: str, mode: str, strategy: str, chunks: list[Chunk], existing: str | None) -> str:
    parts = [f"RESEARCH QUESTION: {question}", f"MODE: {mode}", f"STRATEGY: {strategy}", task_instruction(mode)]
    parts.append("Return at most 8 strongest findings for this strategy. Preserve adverse evidence when present. Select one supplied span_id per finding. Do not copy or rewrite quotations.")
    if existing: parts.append("EXISTING EVIDENCE OR GAP DESCRIPTION:\n" + existing[:8000])
    parts.append("CANDIDATES:")
    for c in chunks:
        meta = {k:v for k,v in c.dict().items() if k != "text" and v not in (None,{},[])}
        rendered = "\n".join(f"[{s['span_id']}] {s['text']}" for s in _candidate_spans(c))
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
    return out

def _decompose_claim(question: str) -> list[str]:
    parts = re.split(r"\s*;\s*|\s*,\s*(?:(?:but|while|whereas|and)\s+)?(?=[A-Za-z])|\s+and\s+(?=(?:the|it|they|this|that)\b)", question, flags=re.I)
    cleaned = [p.strip(" .") for p in parts if len(p.strip(" .")) >= 8]
    return cleaned if len(cleaned) > 1 else [question]

def _candidate_spans(chunk: Chunk) -> list[dict[str, Any]]:
    """Deterministic sentence/line spans that the worker can select by ID."""
    boundaries = list(re.finditer(r"[^\n.!?]+(?:[.!?]+(?=\s|$)|\n|$)", chunk.text))
    out = []
    for match in boundaries:
        text = match.group().strip()
        if not text: continue
        start = match.start() + len(match.group()) - len(match.group().lstrip())
        out.append({"span_id":f"S{len(out):03d}", "text":text,
                    "start":start, "end":start + len(text)})
    return out or [{"span_id":"S000", "text":chunk.text,
                    "start":0, "end":len(chunk.text)}]

def _diverse_candidates(index: Index, propositions: list[str], strategy: str, limit: int) -> list[Chunk]:
    pools = [index.search(_terms(p, strategy), limit) for p in propositions]
    out, hashes, paths = [], set(), set()
    while any(pools) and len(out) < limit:
        for pool in pools:
            while pool:
                chunk = pool.pop(0)
                if chunk.source_hash in hashes or chunk.source_path in paths: continue
                hashes.add(chunk.source_hash); paths.add(chunk.source_path); out.append(chunk); break
            if len(out) >= limit: break
    return out

def _evidence_from_finding(f: dict, chunk: Chunk, question: str, model: str, search_id: str,
                           context: str, spans: list[dict[str, Any]] | None = None) -> Evidence | None:
    excerpt = str(f.get("excerpt", "")).strip()
    flags = []
    if f.get("span_id"):
        selected = {s["span_id"]:s for s in (spans or _candidate_spans(chunk))}.get(str(f["span_id"]))
        if not selected: return None
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
    eid = hashlib.sha256(f"{question}\x1f{chunk.source_path}\x1f{chunk.source_hash}\x1f{excerpt}".encode()).hexdigest()[:24]
    return Evidence(eid, question, str(f.get("topic","")), chunk.source_path, chunk.source_type,
        excerpt, context, str(f.get("relevance","candidate")), str(f.get("short_note","")), model,
        now, chunk.source_hash, search_id, page=chunk.page, sheet=chunk.sheet, section=chunk.section,
        line_start=chunk.line_start, line_end=chunk.line_end, row_start=chunk.row_start, row_end=chunk.row_end,
        paragraph_start=chunk.paragraph_start, paragraph_end=chunk.paragraph_end,
        document_date=f.get("document_date"), event_date=f.get("event_date"), evidence_type=et, stage=stage,
        qualification=str(f.get("qualification","")), contradiction=str(f.get("contradiction","")),
        quote_verified=True, validation_flags=flags, fingerprint=eid)

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
    for c in index.all_chunks(limit=300):
        years.update(re.findall(r"\b(?:19|20)\d{2}\b", c.text))
        for name in re.findall(r"\b[A-Z][A-Za-z&.-]+(?:\s+[A-Z][A-Za-z&.-]+){1,4}\b", c.text): entities[name] = entities.get(name,0)+1
    likely = [s["source_path"] for s in sources if re.search(r"signed|approved|minutes|resolution|report|official|memorandum", s["source_path"], re.I)][:12]
    return {"file_families": families, "likely_authoritative_sources": likely,
            "date_range": [min(years),max(years)] if years else [],
            "major_entities": [x[0] for x in sorted(entities.items(), key=lambda x:x[1], reverse=True)[:12]],
            "extraction_failures": stats["extraction_failures"]}

def research(path: str | Path, question: str | None = None, mode: str = "inventory", depth: str = "standard",
             *, fresh: bool = False, challenge_existing: bool = False, existing_evidence: str | None = None,
             config_path: str | Path | None = None, backend: Backend | None = None,
             rebuild: bool = False, output_budget: str | None = None) -> dict[str, Any]:
    root = Path(path).expanduser().resolve()
    if not root.is_dir(): raise ValueError(f"Not a directory: {root}")
    if mode not in MODES: raise ValueError(f"Unknown mode: {mode}")
    if depth not in DEPTH_PASSES: raise ValueError(f"Unknown depth: {depth}")
    if mode != "inventory" and not question: raise ValueError(f"--question is required for {mode}")
    cfg = load_config(root, config_path); ensure_repo_config(root, cfg)
    budget_name = output_budget or cfg.get("frontier_output_budget", "standard")
    budgets = cfg.get("frontier_output_budgets", {})
    if budget_name not in budgets: raise ValueError(f"Unknown output budget: {budget_name}")
    budget_tokens = int(budgets[budget_name])
    store, started = Store(root), time.monotonic(); index = Index(root, store)
    stats = index.build(cfg, force=rebuild)
    sid = uuid.uuid4().hex
    result: dict[str, Any] = {"schema_version":"1.1", "search_id":sid, "repository":str(root),
                              "question":question, "mode":mode, "depth":depth, "model":cfg["model"],
                              "output_budget":budget_name}
    if question: result["propositions"] = _decompose_claim(question)
    if mode == "inventory":
        result["inventory"] = _inventory(index, store, stats); evidence_records = []; strategies = ["deterministic inventory"]
        reused = []; candidate_paths=set(); inspected=set(); no_results=[]; rejected=[]
        qwen_generations=0; candidate_text_chars=0; usage=[]; generated_count=0
    else:
        reused = [] if fresh or challenge_existing else _reuse(store, index, question or "")
        evidence, strategies, inspected, candidate_paths, no_results, rejected = [], [], set(), set(), [], []
        worker = backend or OllamaBackend(cfg)
        propositions = _decompose_claim(question or "")
        persisted_malformed = 0
        qwen_generations, candidate_text_chars = 0, 0
        pass_count = DEPTH_PASSES[depth]
        if mode == "contradictions": chosen = STRATEGIES[4:5] + STRATEGIES[:max(0,pass_count-1)]
        elif mode == "source-trace": chosen = STRATEGIES[5:6] + STRATEGIES[:max(0,pass_count-1)]
        elif mode == "gap-search": chosen = STRATEGIES[6:7] + STRATEGIES[:max(0,pass_count-1)]
        else: chosen = STRATEGIES[:pass_count]
        for strategy in chosen:
            strategies.append(strategy)
            chunks = _fit_chunks(_diverse_candidates(index, propositions, strategy, cfg["candidate_limit"]), cfg["max_prompt_chars"])
            candidate_paths.update(c.source_path for c in chunks)
            if not chunks: no_results.append(strategy); continue
            inspected.update(c.source_path for c in chunks)
            candidate_text_chars += sum(len(c.text) for c in chunks)
            qwen_generations += 1
            try:
                response = worker.generate_json(SYSTEM, _candidate_prompt(question or "", mode, strategy, chunks, existing_evidence), FINDINGS_SCHEMA)
            finally:
                failures = list(getattr(worker, "malformed_log", []))[persisted_malformed:]
                for failure in failures:
                    store.append("qwen_failures.jsonl", {"search_id":sid,"strategy":strategy,
                        "timestamp":datetime.now(timezone.utc).isoformat(), **failure})
                persisted_malformed += len(failures)
            by_id = {c.chunk_id:c for c in chunks}
            found_this = 0
            for raw_finding in response.get("findings", []):
                finding = _normalize_worker_finding(raw_finding)
                chunk = by_id.get(finding.get("chunk_id"))
                if not chunk:
                    rejected.append({"strategy":strategy,"reason":"unknown_chunk_id","finding":finding}); continue
                expanded = index.expand(chunk, cfg["context_expansion"])
                context = "\n\n".join(x.text for x in expanded)
                ev = _evidence_from_finding(finding, chunk, question or "", worker.model, sid, context,
                                            spans=_candidate_spans(chunk))
                if ev: evidence.append(ev); found_this += 1
                else: rejected.append({"strategy":strategy,"reason":"excerpt_not_found_in_candidate","chunk_id":chunk.chunk_id,
                                       "source_path":chunk.source_path,"span_id":finding.get("span_id"),
                                       "excerpt":finding.get("excerpt","")})
            if not found_this: no_results.append(strategy)
        generated_count = len(evidence)
        unique = {e.id:e for e in evidence}; evidence = list(unique.values())
        evidence_records = store.assign_evidence_ids([e.dict() for e in evidence])
        store.append_many("evidence.jsonl", evidence_records)
        if mode == "chronology": store.append_many("chronology.jsonl", evidence_records)
        usage = list(getattr(worker, "usage_log", []))[-qwen_generations:] if qwen_generations else []
        if rejected: store.append_many("unresolved.jsonl", ({"search_id":sid, **x} for x in rejected))
    elapsed = round(time.monotonic()-started,3)
    coverage = {"repository_files_indexed":len(store.read("source_index.jsonl")),
                "candidate_files":len(candidate_paths), "files_substantively_inspected":len(inspected),
                "search_strategies":len(strategies), "extraction_failures":len(stats["extraction_failures"])}
    gaps = [f"No validated evidence returned for strategy: {x}" for x in no_results]
    if rejected: gaps.append(f"{len(rejected)} candidate findings rejected during quotation/provenance validation")
    packet = promote(evidence_records, budget_name=budget_name, budget_tokens=budget_tokens,
                     max_records=int(cfg.get("max_promoted_records",12)), coverage=coverage, gaps=gaps)
    promoted_ids=set(packet["promoted_ids"])
    promoted=[r for group in (packet["best_support"],packet["best_qualification"],
                              packet["best_contradiction"],packet["other_promoted"]) for r in group]
    # Reused evidence is reported by ID/path only; never dump the persistent record set again.
    reused_compact=[{"id":r.get("id"),"source_path":r.get("source_path")} for r in reused[:int(cfg.get("max_promoted_records",12))]]
    index_text=index.text_stats()
    input_tokens=sum(int(x.get("input_tokens") or 0) for x in usage)
    output_tokens=sum(int(x.get("output_tokens") or 0) for x in usage)
    inference_seconds=round(sum(float(x.get("elapsed_seconds") or 0) for x in usage),3)
    candidate_hash_list=sorted({index.source_hashes().get(p, "") for p in candidate_paths if index.source_hashes().get(p)})
    telemetry_record={
        "search_id":sid, "source_files_indexed":coverage["repository_files_indexed"],
        "candidate_files":coverage["candidate_files"], "files_substantively_inspected_locally":coverage["files_substantively_inspected"],
        "corpus_extracted_characters_indexed":index_text["characters"],
        "estimated_corpus_tokens_indexed":max(1,index_text["characters"]//4),
        "local_extracted_text_characters_processed":candidate_text_chars,
        "estimated_local_input_tokens_processed":input_tokens or max(0,candidate_text_chars//4),
        "qwen_generations":qwen_generations, "estimated_local_model_output_tokens":output_tokens,
        "qwen_failures":len(getattr(worker, "malformed_log", [])) if mode != "inventory" else 0,
        "candidate_source_hashes":len(candidate_hash_list), "candidate_source_hash_list":candidate_hash_list,
        "elapsed_local_inference_seconds":inference_seconds,
        "evidence_records_generated":generated_count,
        "evidence_records_persisted":len(evidence_records),
        "evidence_records_promoted":len(promoted_ids),
        "estimated_promoted_tokens":packet["estimated_tokens"],
        "additional_source_expansions":0, "estimated_expansion_tokens":0,
        "estimated_total_frontier_research_tokens":packet["estimated_tokens"],
        "estimated_corpus_to_promoted_compression":round((max(1,index_text["characters"]//4))/packet["estimated_tokens"],2) if packet["estimated_tokens"] else None,
        "estimated_corpus_to_frontier_compression":round((max(1,index_text["characters"]//4))/packet["estimated_tokens"],2) if packet["estimated_tokens"] else None,
        "elapsed_job_seconds":elapsed,
        "estimate_note":"Token and compression figures are approximate research-context estimates, not API billing or exact Codex usage.",
    }
    store.append("packets.jsonl", {"search_id":sid,"question":question,"packet":packet})
    store.append("telemetry.jsonl", telemetry_record)
    public_packet={"budget":packet["budget"],"budget_tokens":packet["budget_tokens"],
                   "coverage":packet["coverage"],"best_support":[r["id"] for r in packet["best_support"]],
                   "best_qualification":[r["id"] for r in packet["best_qualification"]],
                   "best_contradiction":[r["id"] for r in packet["best_contradiction"]],
                   "other_promoted":[r["id"] for r in packet["other_promoted"]],
                   "unresolved_gaps":packet["unresolved_gaps"],"promoted_ids":packet["promoted_ids"],
                   "estimated_tokens":packet["estimated_tokens"]}
    result.update({"evidence_packet":public_packet,"evidence":promoted,"reused_evidence":reused_compact,
                   "reused_evidence_count":len(reused),"telemetry":telemetry_record})
    summary = {"repository_files_indexed":len(store.read("source_index.jsonl")),
               "potentially_relevant_files":len(candidate_paths),
               "files_substantively_inspected":len(inspected),
               "evidence_records_generated":generated_count,
               "evidence_records_persisted":len(evidence_records),
               "evidence_records_returned":len(promoted),
               "contradictory_or_qualifying_records":sum(r.get("evidence_type") in {"contradictory","qualifying"} for r in evidence_records),
               "extraction_failures":len(stats["extraction_failures"]), "elapsed_seconds":elapsed}
    result["summary"] = summary
    log = {"search_id":sid,"question":question,"mode":mode,"depth":depth,
           "timestamp":datetime.now(timezone.utc).isoformat(),"model":cfg["model"],"search_strategies":strategies,
           "candidate_files_considered":sorted(candidate_paths),"files_actually_inspected":sorted(inspected),
           "evidence_persisted":[r["id"] for r in evidence_records],"evidence_promoted":packet["promoted_ids"],
           "no_result_queries":no_results,"rejected_findings":rejected,
           "source_hashes":index.source_hashes(),"elapsed_seconds":elapsed}
    store.append("searches.jsonl", log)
    return result
