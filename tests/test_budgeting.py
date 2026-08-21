from __future__ import annotations

import json
import re
from pathlib import Path
import pytest

from repo_research.ollama import MockBackend
from repo_research.promotion import promote
from repo_research.research import research
from repo_research.retrieval import (contradiction_evidence, expand_evidence_context,
                                     get_evidence, open_source_location, related_evidence,
                                     telemetry)
from repo_research.store import Store


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    (tmp_path/"primary.md").write_text("# Signed Resolution\nOn 15 March 2023, the Board approved establishment of the Atlas Unit. Operations began on 1 July 2023.\n", encoding="utf-8")
    (tmp_path/"later-summary.txt").write_text("A retrospective summary incorrectly states that Atlas operations began in June 2023.\n", encoding="utf-8")
    (tmp_path/"proposal.md").write_text("A proposal dated 2 January 2023 recommends creating an Atlas Unit; it does not implement the proposal.\n", encoding="utf-8")
    (tmp_path/"constraints.md").write_text("Staffing and procurement constraints delayed some Atlas activities, but no counterfactual magnitude was measured.\n", encoding="utf-8")
    return tmp_path


def _record(i: int, evidence_type: str = "direct", path: str | None = None) -> dict:
    return {
        "id": f"E{i:04d}", "research_question": "q", "topic": "topic",
        "source_path": path or f"working/summary-{i}.txt", "source_type": "txt",
        "source_hash": f"h{i}", "search_id": "s", "excerpt": (f"Evidence passage {i}. " * 30),
        "context": "large local context that must not be promoted " * 200,
        "relevance": "direct", "worker_note": "Candidate note " * 20,
        "worker_model": "mock", "retrieved_at": "now", "evidence_type": evidence_type,
        "stage": "approval", "qualification": "qualification" if evidence_type == "qualifying" else "",
        "contradiction": "contradiction" if evidence_type == "contradictory" else "",
        "quote_verified": True,
    }


def test_large_evidence_set_is_promoted_with_budget_and_adverse_preservation():
    records=[_record(1,"direct","signed/Agreement Amendment.pdf"),
             _record(2,"qualifying","official/Minutes.pdf"),
             _record(3,"contradictory","official/Correction Minutes.pdf")]
    records += [_record(i) for i in range(4,45)]
    packet=promote(records,budget_name="compact",budget_tokens=750,max_records=12,
                   coverage={"repository_files_indexed":500,"candidate_files":80,
                             "files_substantively_inspected":40,"search_strategies":7,
                             "extraction_failures":2},gaps=["one unresolved gap"])
    assert packet["estimated_tokens"] <= 750
    assert len(packet["promoted_ids"]) <= 12
    assert "E0003" in packet["promoted_ids"]  # contradiction survives the small budget
    assert "E0002" in packet["promoted_ids"]  # qualification survives too
    assert "E0001" in packet["promoted_ids"]  # high-authority direct evidence is prioritized
    assert all("context" not in r for k in ("best_support","best_qualification","best_contradiction","other_promoted") for r in packet[k])


def test_research_persists_full_evidence_but_returns_compact_packet(corpus):
    out=research(corpus,"When did Atlas become operational?",mode="exhaustive",depth="deep",
                 fresh=True,backend=MockBackend(),output_budget="compact")
    persisted=[r for r in Store(corpus).read("evidence.jsonl") if r.get("search_id")==out["search_id"]]
    assert persisted
    assert len(out["evidence"]) <= 12
    assert out["evidence_packet"]["estimated_tokens"] <= 750
    assert len(persisted) >= len(out["evidence"])
    assert all(re.fullmatch(r"E\d{4,}", r["id"]) for r in persisted)
    serialized=json.dumps(out)
    assert "large local context" not in serialized
    assert "candidate_files_considered" not in serialized
    assert out["summary"]["evidence_records_persisted"] == len(persisted)


def test_progressive_evidence_retrieval_and_telemetry(corpus):
    q="Atlas operations began on 1 July 2023"
    support=research(corpus,q,mode="evidence",depth="standard",fresh=True,
                     backend=MockBackend(),output_budget="standard")
    research(corpus,q,mode="contradictions",depth="deep",fresh=True,
             backend=MockBackend(),output_budget="compact")
    eid=support["evidence"][0]["id"]
    rec=get_evidence(corpus,eid)
    assert rec["id"]==eid and rec["source_path"]
    expanded=expand_evidence_context(corpus,eid,radius=1)
    assert expanded["context"] and expanded["source_path"]==rec["source_path"]
    opened=open_source_location(corpus,eid)
    assert Path(opened["absolute_path"]).exists() and opened["location"] is not None
    assert isinstance(related_evidence(corpus,eid)["related"],list)
    adverse=contradiction_evidence(corpus,eid)["contradictions"]
    assert adverse and all(x["evidence_type"]=="contradictory" or x["contradiction"] for x in adverse)
    t=telemetry(corpus,support["search_id"])
    assert t["source_files_indexed"] >= 1
    assert t["estimated_local_input_tokens_processed"] > 0
    assert t["evidence_records_persisted"] >= 1
    assert t["additional_source_expansions"] >= 4
    assert t["estimated_expansion_tokens"] > 0
    assert "not API billing" in t["estimate_note"]


def test_output_budget_modes_have_expected_order(corpus):
    values=[]
    for budget in ("compact","standard","expanded"):
        out=research(corpus,"Reconstruct Atlas establishment and operation",mode="chronology",
                     depth="deep",fresh=True,backend=MockBackend(),output_budget=budget)
        values.append(out["evidence_packet"]["budget_tokens"])
        assert out["evidence_packet"]["estimated_tokens"] <= out["evidence_packet"]["budget_tokens"]
    assert values == [750,1500,4000]


@pytest.mark.parametrize("mode", ["evidence","contradictions","chronology","exhaustive","gap-search","source-trace"])
def test_all_research_modes_still_return_budgeted_packets(corpus, mode):
    out=research(corpus,"Trace Atlas establishment, operation, and conflicting dates",mode=mode,
                 depth="quick",fresh=True,backend=MockBackend(),output_budget="compact",
                 existing_evidence="The operation date remains uncertain" if mode=="gap-search" else None)
    assert out["mode"]==mode
    assert out["evidence_packet"]["estimated_tokens"] <= 750
    assert "telemetry" in out and "summary" in out
