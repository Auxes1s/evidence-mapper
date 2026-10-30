"""Run one benchmark job against the version selected by PYTHONPATH."""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import repo_research
from repo_research.config import load_config
from repo_research.ollama import OllamaBackend
from repo_research.research import research


class FaultBackend:
    """Inject the same valid/invalid model responses into either pipeline."""

    model = "controlled-error-injection"

    def __init__(self, cfg, fault):
        self.cfg, self.fault = cfg, fault
        self.usage_log, self.malformed_log = [], []

    def model_info(self, model):
        return {"model": model, "digest": "fixed-error-injection-v1"}

    def generate_json(self, system, prompt, schema=None):
        self.usage_log.append({"input_tokens": max(1, len(prompt) // 4),
                               "output_tokens": 100, "elapsed_seconds": 0,
                               "done": True, "done_reason": "stop"})
        if "RELATION_CHECK" in prompt:
            return {"relations": [], "unresolved": []}
        candidates = []
        for block in prompt.split("--- CANDIDATE ")[1:]:
            header, _, text = block.partition("\nTEXT:\n")
            meta = json.loads(header.split(" ---", 1)[0])
            spans = re.findall(r"\[(S\d+)\]\s*([^\n]+)", text)
            candidates.append((meta["chunk_id"], spans))
        assert candidates, "The test must actually reach model extraction"
        chunk_id, spans = candidates[0]
        span_id, excerpt = spans[0]
        finding = {"chunk_id": chunk_id, "span_id": span_id, "excerpt": excerpt,
                   "topic": "approval", "evidence_type": "direct", "stage": "approval",
                   "relevance": "direct", "qualification": "", "contradiction": "",
                   "short_note": "Controlled test response."}
        result = {"findings": [finding], "dispositions": [
            {"chunk_id": cid, "status": "evidence_extracted", "reason": "Controlled response"}
            for cid, _ in candidates], "no_evidence_found": False, "suggested_terms": []}
        if self.fault == "invented-quotation":
            finding.pop("span_id")
            finding["excerpt"] = "The Falcon programme closed in 2099 with a billion beneficiaries."
        elif self.fault == "invented-source":
            finding["chunk_id"] = "source-id-never-supplied"
        elif self.fault == "missing-acknowledgements":
            result["dispositions"] = []
        elif self.fault == "unacknowledged-empty-output":
            result["findings"] = []
            result["dispositions"] = []
            result["no_evidence_found"] = True
        elif self.fault == "truncated-output":
            self.usage_log[-1]["done_reason"] = "length"
        elif self.fault == "genuine-no-match":
            result["findings"] = []
            result["no_evidence_found"] = True
            for disposition in result["dispositions"]:
                disposition["status"] = "no_relevant_evidence"
        return result


def collect(root, result):
    if result.get("schema_version") != "3.0":
        return list(result.get("evidence", [])), [result]
    from repo_research.bundles import get_bundle, overview
    from repo_research.retrieval import expand_evidence_context

    units, pages = [], [result]
    page = result
    while True:
        for prop in page["propositions"]:
            cursor = None
            while True:
                bundle = get_bundle(root, result["search_id"], prop["bundle_id"], cursor)
                units.extend(bundle["units"])
                pages.append(bundle)
                for requirement in bundle["inspection_requirements"]:
                    if requirement.get("evidence_id"):
                        expanded = expand_evidence_context(root, requirement["evidence_id"],
                                                           run_id=result["search_id"])
                        pages.append(expanded)
                        units.append({"source_path": expanded["source_path"],
                                      "excerpt": "", "context_blocks": [{"text": expanded["context"]}]})
                cursor = bundle["next_cursor"]
                if cursor is None:
                    break
        if page["next_cursor"] is None:
            break
        page = overview(root, result["search_id"], page["next_cursor"])
        pages.append(page)
    return units, pages


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("job")
    args = parser.parse_args()
    job = json.loads(Path(args.job).read_text())
    root = Path(job["root"])
    cfg = load_config(root)
    worker = (FaultBackend(cfg, job["fault"]) if job.get("fault") else OllamaBackend(cfg))
    started = time.perf_counter()
    result, units, pages, error = None, [], [], None
    try:
        result = research(root, job["question"], mode="evidence", depth="quick", backend=worker)
        units, pages = collect(root, result)
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
    elapsed = time.perf_counter() - started
    payload = {"elapsed_seconds": elapsed, "module": repo_research.__file__,
               "result": result, "units": units, "delivered_pages": pages,
               "error": error, "model_calls": len(worker.usage_log),
               "model_usage": worker.usage_log,
               "completed": error is None and result is not None and result.get("status") != "incomplete"}
    Path(job["output"]).write_text(json.dumps(payload, indent=2, default=str) + "\n")
    print(json.dumps({"seconds": round(elapsed, 3), "calls": payload["model_calls"],
                      "completed": payload["completed"], "error": error}, default=str))


if __name__ == "__main__":
    main()
