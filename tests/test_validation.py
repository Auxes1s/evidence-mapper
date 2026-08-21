from repo_research.models import Chunk
from repo_research.research import _candidate_spans, _evidence_from_finding, _normalize_worker_finding

def test_rejects_hallucinated_quote():
    chunk=Chunk("c","a.txt","txt","h","Actual source text.",0,line_start=1,line_end=1)
    finding={"excerpt":"Invented quote.","evidence_type":"direct","stage":"operation"}
    assert _evidence_from_finding(finding,chunk,"q","m","s","context") is None

def test_rejects_empty_quote():
    chunk=Chunk("c","a.txt","txt","h","Actual source text.",0,line_start=1,line_end=1)
    finding={"excerpt":"","evidence_type":"direct","stage":"operation"}
    assert _evidence_from_finding(finding,chunk,"q","m","s","context") is None

def test_normalizes_small_model_evidence_alias():
    finding=_normalize_worker_finding({"chunk_id":"c","evidence":"Actual source text."})
    assert finding["excerpt"] == "Actual source text."
    assert _evidence_from_finding(finding,Chunk("c","a.txt","txt","h","Actual source text.",0),"q","m","s","context") is not None

def test_span_id_selects_source_text_without_worker_copying_it():
    chunk=Chunk("c","a.txt","txt","h","First sentence. Exact second sentence!",0)
    spans=_candidate_spans(chunk)
    selected=next(s for s in spans if s["text"] == "Exact second sentence!")
    assert selected["span_id"] == "S001"
    finding={"span_id":selected["span_id"],"evidence_type":"direct","stage":"operation"}
    ev=_evidence_from_finding(finding,chunk,"q","m","s","context",spans=spans)
    assert ev and ev.excerpt == "Exact second sentence!"
    assert "deterministic_span_selection" in ev.validation_flags
