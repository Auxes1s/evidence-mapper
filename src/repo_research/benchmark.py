from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from pathlib import Path

from .bundles import get_bundle, overview
from .config import load_config
from .research import research
from .retrieval import expand_evidence_context, run_manifest, telemetry


def run_benchmark(spec_path: str, model: str | None = None) -> dict:
    spec=json.loads(Path(spec_path).read_text(encoding="utf-8"))
    root=Path(spec["repository"]).resolve()
    override=root/".codex-research/benchmark-config.yaml"
    if model:
        override.parent.mkdir(parents=True,exist_ok=True)
        override.write_text(f"model: {model}\n",encoding="utf-8")
    totals={"known_evidence":0,"known_evidence_recovered":0,"known_evidence_in_fetched_context":0,
            "false_evidence_records":0,"additional_unjudged_evidence_records":0,
            "source_location_correct":0,"contradictions_expected":0,"contradictions_recovered":0,
            "frontier_delivery_tokens":0,"generation_calls":0,"incomplete_cases":0}
    started=time.monotonic(); cases=[]
    for case in spec["cases"]:
        result=research(root,case["question"],case.get("mode","evidence"),
                        case.get("depth",spec.get("depth","standard")),fresh=True,
                        config_path=override if model else None,propositions=case.get("propositions"))
        run_id=result["search_id"]
        units=[]; context=[]; page=result
        while True:
            for prop in page["propositions"]:
                cursor=None
                while True:
                    bundle=get_bundle(root,run_id,prop["bundle_id"],cursor)
                    units.extend(bundle["units"])
                    for unit in bundle["units"]:
                        context.append((unit["source_path"],"\n".join(b["text"] for b in unit["context_blocks"])))
                    for requirement in bundle["inspection_requirements"]:
                        if requirement.get("evidence_id"):
                            full=expand_evidence_context(root,requirement["evidence_id"],run_id=run_id)
                            context.append((full["source_path"],full["context"]))
                    cursor=bundle["next_cursor"]
                    if cursor is None: break
            if page["next_cursor"] is None: break
            page=overview(root,run_id,page["next_cursor"])
        manifest=run_manifest(root,run_id)
        evidence=manifest["evidence_records"]
        expected=case.get("expected",[]); totals["known_evidence"]+=len(expected)
        matched=set(); matched_records=set(); contextual=set()
        for i,anchor in enumerate(expected):
            for record in evidence:
                if (record["source_path"]==anchor["source_path"] and
                    anchor.get("contains","").lower() in record["excerpt"].lower()):
                    matched.add(i); matched_records.add(record["id"])
                    if all(record.get(k)==anchor[k] for k in ("page","sheet","row_start") if k in anchor):
                        totals["source_location_correct"]+=1
                    if anchor.get("contradictory") and (record["evidence_type"]=="contradictory" or record.get("contradiction")):
                        totals["contradictions_recovered"]+=1
                    break
            if any(path==anchor["source_path"] and anchor.get("contains","").lower() in text.lower() for path,text in context):
                contextual.add(i)
        totals["known_evidence_recovered"]+=len(matched)
        totals["known_evidence_in_fetched_context"]+=len(contextual)
        totals["contradictions_expected"]+=sum(bool(x.get("contradictory")) for x in expected)
        # Additional legitimate findings are unjudged unless the specification labels them false.
        false_ids={r["id"] for r in evidence for forbidden in case.get("unexpected",[])
                   if r["source_path"]==forbidden["source_path"] and forbidden.get("contains","").lower() in r["excerpt"].lower()}
        totals["false_evidence_records"]+=len(false_ids)
        totals["additional_unjudged_evidence_records"]+=len({r["id"] for r in evidence}-matched_records-false_ids)
        stats=telemetry(root,run_id)
        totals["frontier_delivery_tokens"]+=stats["estimated_total_frontier_research_tokens"]
        totals["generation_calls"]+=stats["qwen_generations"]
        totals["incomplete_cases"]+=int(result["status"]=="incomplete")
        cases.append({"question":case["question"],"search_id":run_id,"returned":len(evidence),
                      "expected_recovered":len(matched),"expected_in_fetched_context":len(contextual),
                      "status":result["status"],"coverage":result["coverage"]})
    known=totals["known_evidence"]
    return {"schema_version":"3.0","model":model or load_config(root)["model"],"metrics":totals,
            "known_evidence_recall":totals["known_evidence_recovered"]/known if known else None,
            "fetched_context_recall":totals["known_evidence_in_fetched_context"]/known if known else None,
            "cases":cases,"runtime_seconds":round(time.monotonic()-started,3),
            "python_process_peak_rss_mb":round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/(1024 if sys.platform!="darwin" else 1024*1024),2),
            "measurement_note":"Peak RSS covers this Python process. Delivery estimates include overview pages, bundles, and explicit context fetches; model memory and final agent source reads are separate."}


def main(argv=None):
    parser=argparse.ArgumentParser(description="Benchmark bounded local evidence research")
    parser.add_argument("spec"); parser.add_argument("--model")
    args=parser.parse_args(argv)
    print(json.dumps(run_benchmark(args.spec,args.model),indent=2))


if __name__=="__main__": main()
