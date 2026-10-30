from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from repo_research.budgeting import conservative_token_count, partition_by_budget
from repo_research.extract import discover_files
from repo_research.index import Index
from repo_research.models import Chunk
from repo_research.ollama import MockBackend
from repo_research.promotion import compact_record
from repo_research.research import _diverse_candidates, research
from repo_research.retrieval import coverage_report, run_manifest, telemetry
from repo_research.store import Store


def _cfg(**overrides):
    cfg={"ignore_dirs":[".codex-research"],"include":[],"exclude":[],"max_file_bytes":100,
         "chunk_chars":20,"chunk_overlap_chars":2,"file_timeout_seconds":0,
         "ocr_image_only_pdfs":False,"ocr_max_pages":10,"archive_source_bytes":False}
    cfg.update(overrides)
    return cfg


def test_discovery_manifest_accounts_for_silent_exclusions(tmp_path: Path):
    (tmp_path/"ok.txt").write_text("evidence")
    (tmp_path/"large.txt").write_text("x"*101)
    (tmp_path/"binary.bin").write_bytes(b"abc")
    (tmp_path/"excluded.md").write_text("excluded")
    files,manifest=discover_files(tmp_path,_cfg(exclude=["excluded.md"]))
    assert [p.name for p in files] == ["ok.txt"]
    status={x["source_path"]:x["status"] for x in manifest}
    assert status == {"binary.bin":"unsupported","excluded.md":"excluded",
                      "large.txt":"oversized","ok.txt":"eligible"}


def test_index_rehashes_even_when_size_and_mtime_are_unchanged(tmp_path: Path):
    source=tmp_path/"source.txt"; source.write_text("first value")
    store=Store(tmp_path); index=Index(tmp_path,store); cfg=_cfg(max_file_bytes=1000)
    index.build(cfg); before=source.stat()
    source.write_text("other value")
    os.utime(source,(before.st_atime,before.st_mtime))
    stats=index.build(cfg)
    assert stats["files_indexed"] == 1
    assert index.all_chunks()[0].text == "other value"


def test_retrieval_allows_multiple_chunks_from_one_source_but_deduplicates_copies(tmp_path: Path):
    text="alpha evidence one\nalpha evidence two\nalpha evidence three"
    (tmp_path/"a.txt").write_text(text); (tmp_path/"copy.txt").write_text(text)
    store=Store(tmp_path); index=Index(tmp_path,store); index.build(_cfg(max_file_bytes=1000))
    got=_diverse_candidates(index,["alpha evidence"],"direct terminology",10)
    assert len({x.chunk_id for x in got}) >= 2
    assert len({x.source_path for x in got}) == 1


def test_optional_hybrid_retrieval_fuses_lexical_and_vector_channels(tmp_path: Path):
    class Embedder:
        def embed(self,texts,model=None):
            return [[1.0,0.0] if ("concept" in text or "semantic" in text) else [0.0,1.0] for text in texts]
    (tmp_path/"lexical.txt").write_text("alpha terminology")
    (tmp_path/"semantic.txt").write_text("semantic relationship")
    store=Store(tmp_path); index=Index(tmp_path,store); index.build(_cfg(max_file_bytes=1000,chunk_chars=100))
    embedder=Embedder(); index.ensure_embeddings("fake",embedder)
    got=index.hybrid_search(["alpha"],"concept",2,"fake",embedder)
    assert {x.source_path for x in got} == {"lexical.txt","semantic.txt"}
    assert all(x.metadata["retrieval_channels"] for x in got)


def test_budget_partitions_unicode_without_exceeding_safe_bound():
    chunks=[Chunk(str(i),"a.txt","txt","h","研究証拠"*30,i) for i in range(5)]
    render=lambda batch:"\n".join(c.text for c in batch)
    batches=partition_by_budget(chunks,render,system="system",prefix="",context_tokens=900,
                                output_tokens=100,safety_margin_tokens=100)
    assert sum(len(x) for x in batches)==5
    assert all(conservative_token_count("system",render(batch)).tokens+200 <= 900 for batch in batches)


def test_run_persists_exact_calls_dispositions_claims_and_coverage(tmp_path: Path):
    (tmp_path/"a.txt").write_text("Alpha evidence supports the finding. Another relevant result follows.")
    out=research(tmp_path,"What supports Alpha?",mode="evidence",depth="quick",fresh=True,backend=MockBackend())
    assert out["status"] == "complete_for_retrieved_scope"
    report=coverage_report(tmp_path,out["search_id"])
    assert report["retrieval"]["unique_chunks_scheduled"] >= 1
    assert report["model_processing"]["unique_chunks_with_dispositions"] >= 1
    assert report["model_processing"]["failed_or_unacknowledged"] == 0
    assert report["synthesis"]["propositions"] == 1
    db=sqlite3.connect(tmp_path/".codex-research/audit.sqlite3")
    call=db.execute("SELECT prompt_hash,chunk_ids_json,preflight_input_tokens,status FROM model_calls").fetchone()
    assert len(call[0])==64 and json.loads(call[1]) and call[2]>0 and call[3]=="complete"
    assert "does not mean the full corpus was reviewed" in report["scope_statement"]
    manifest=run_manifest(tmp_path,out["search_id"])
    assert manifest["corpus_files"] and manifest["model_calls"] and manifest["candidate_dispositions"]
    assert manifest["model_calls"][0]["response"]


def test_model_outputs_are_replayed_from_immutable_call_cache(tmp_path: Path):
    (tmp_path/"a.txt").write_text("Alpha evidence supports the finding.")
    first=research(tmp_path,"Alpha evidence",mode="evidence",depth="quick",fresh=True,backend=MockBackend())
    second=research(tmp_path,"Alpha evidence",mode="evidence",depth="quick",backend=MockBackend())
    assert telemetry(tmp_path,first["search_id"])["qwen_generations"] > 0
    assert telemetry(tmp_path,second["search_id"])["qwen_generations"] == 0
    manifest=run_manifest(tmp_path,second["search_id"])
    assert manifest["model_calls"] and all(x["status"]=="cached" for x in manifest["model_calls"])


def test_missing_candidate_dispositions_fail_closed(tmp_path: Path):
    class NonAcknowledging(MockBackend):
        def generate_json(self,*args,**kwargs):
            result=super().generate_json(*args,**kwargs)
            result["findings"]=[]; result["dispositions"]=[]; result["no_evidence_found"]=True
            return result
    (tmp_path/"a.txt").write_text("Alpha evidence supports the finding.")
    out=research(tmp_path,"Alpha evidence",mode="evidence",depth="quick",fresh=True,backend=NonAcknowledging())
    assert out["status"] == "incomplete"
    assert any("schema" in x or "not acknowledged" in x for x in run_manifest(tmp_path,out["search_id"])["research_state"]["errors"])


def test_output_exhaustion_and_retrieval_limit_are_surfaced(tmp_path: Path):
    class Exhausted(MockBackend):
        def generate_json(self,*args,**kwargs):
            result=super().generate_json(*args,**kwargs)
            self.usage_log[-1]["done_reason"]="length"
            return result
    (tmp_path/"a.txt").write_text("\n".join(f"Alpha evidence item {i}." for i in range(20)))
    cfg=tmp_path/"test-config.yaml"
    cfg.write_text("candidate_limit: 1\nchunk_chars: 60\nchunk_overlap_chars: 0\n")
    out=research(tmp_path,"Alpha evidence",mode="evidence",depth="quick",fresh=True,
                 backend=Exhausted(),config_path=cfg)
    assert out["status"]=="incomplete"
    assert any("output budget" in x for x in run_manifest(tmp_path,out["search_id"])["research_state"]["errors"])
    report=coverage_report(tmp_path,out["search_id"])
    assert report["retrieval"]["omitted_by_retrieval_limit"] >= 1


def test_context_expansion_stays_within_structural_unit(tmp_path: Path):
    index=Index(tmp_path,Store(tmp_path))
    chunks=[Chunk("p1a","x.pdf","pdf","h","page one a",0,page=1),
            Chunk("p1b","x.pdf","pdf","h","page one b",1,page=1),
            Chunk("p2a","x.pdf","pdf","h","page two a",0,page=2)]
    for chunk in chunks:
        loc=chunk.dict(); text=loc.pop("text"); cid=loc.pop("chunk_id")
        for key in ("source_path","source_type","source_hash","ordinal"): loc.pop(key)
        index.db.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?)",
            (cid,chunk.source_path,chunk.source_type,chunk.source_hash,text,chunk.ordinal,json.dumps(loc),0,0))
    index.db.commit()
    assert [x.chunk_id for x in index.expand(chunks[0],1)] == ["p1a","p1b"]


def test_shortened_promotion_preview_is_not_presented_as_verified_quote():
    rec={"id":"E0001","source_path":"a.txt","source_type":"txt","source_hash":"h",
         "excerpt":"word "*100,"worker_note":"note","quote_verified":True,"evidence_type":"direct","stage":"other"}
    compact=compact_record(rec,"compact")
    assert compact["excerpt_truncated"] is True
    assert compact["quote_verified"] is False
    assert compact["source_span_verified"] is True


def test_corrupt_jsonl_fails_visibly(tmp_path: Path):
    store=Store(tmp_path)
    (store.state/"evidence.jsonl").write_text('{"valid":true}\n{"broken":')
    with pytest.raises(ValueError,match="Corrupt JSONL"):
        store.read("evidence.jsonl")
