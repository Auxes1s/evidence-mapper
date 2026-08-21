SYSTEM = """You are a subordinate local repository evidence scout. Retrieve evidence; do not make the final judgment.
Every finding must refer to one supplied candidate chunk_id and deterministic span_id.
Never invent filenames, locations, dates, or quotations. Retrieval is not verification. Similar wording is not proof.
Prefer contemporaneous primary records, but preserve contradictory, qualifying, and retrospective evidence.
Return NO findings when evidence is absent. Select supplied spans; do not reproduce their text.
Output only JSON matching the requested schema."""

FINDINGS_SCHEMA = {
 "type":"object", "properties": {
  "findings":{"type":"array","items":{"type":"object","properties":{
   "chunk_id":{"type":"string"},"span_id":{"type":"string"},"topic":{"type":"string"},
   "document_date":{"type":["string","null"]},"event_date":{"type":["string","null"]},
   "evidence_type":{"type":"string","enum":["direct","indirect","contextual","retrospective","contradictory","qualifying"]},
   "stage":{"type":"string","enum":["discussion","proposal","approval","funding","initiation","implementation","operation","completion","handover","institutionalization","closure","other"]},
   "relevance":{"type":"string"},"qualification":{"type":"string"},"contradiction":{"type":"string"},
   "short_note":{"type":"string"}},"required":["chunk_id","span_id"]}},
  "no_evidence_found":{"type":"boolean"},"suggested_terms":{"type":"array","items":{"type":"string"}}
 }, "required":["findings","no_evidence_found","suggested_terms"]
}

def task_instruction(mode: str) -> str:
    return {
      "evidence": "Find passages that bear directly or indirectly on each proposition.",
      "contradictions": "Search independently for conflicting dates, corrections, narrower meanings, qualifications, and alternative interpretations. Do not merely restate support.",
      "chronology": "Extract dated candidate events and preserve distinct stages; do not collapse proposal into implementation or operation.",
      "exhaustive": "Use high recall and include support, contradiction, qualification, chronology, and source-tracing clues.",
      "gap-search": "Target what is missing or weakly supported in the supplied question/evidence description.",
      "source-trace": "Find the closest, earliest, or most authoritative source underlying the claim; distinguish original evidence from repetition.",
      "inventory": "Identify authoritative-looking source families, dates, entities, and hierarchies without attempting to summarize everything.",
    }[mode]
