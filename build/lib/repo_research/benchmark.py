from __future__ import annotations

import argparse, json, resource, time
from pathlib import Path
from .config import load_config
from .research import research
from .store import Store

def run_benchmark(spec_path: str, model: str | None = None) -> dict:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8")); root = Path(spec["repository"]).resolve()
    override = root / ".codex-research/benchmark-config.yaml"
    if model:
        override.parent.mkdir(parents=True, exist_ok=True); override.write_text(f"model: {model}\n", encoding="utf-8")
    totals = {"known_evidence":0,"known_evidence_recovered":0,"false_evidence_records":0,
              "source_location_correct":0,"contradictions_expected":0,"contradictions_recovered":0}
    started = time.monotonic(); cases=[]
    for case in spec["cases"]:
        res = research(root, case["question"], case.get("mode","evidence"), case.get("depth",spec.get("depth","standard")),
                       fresh=True, config_path=override if model else None)
        ev = [x for x in Store(root).read("evidence.jsonl") if x.get("search_id")==res["search_id"]]
        expected = case.get("expected",[]); totals["known_evidence"] += len(expected)
        matched=set()
        for i,e in enumerate(expected):
            for got in ev:
                if got["source_path"] == e["source_path"] and e.get("contains","").lower() in got["excerpt"].lower():
                    matched.add(i); totals["source_location_correct"] += 1; break
        totals["known_evidence_recovered"] += len(matched); totals["false_evidence_records"] += max(0,len(ev)-len(matched))
        ce = sum(1 for x in expected if x.get("contradictory")); cr = sum(1 for x in ev if x["evidence_type"]=="contradictory")
        totals["contradictions_expected"] += ce; totals["contradictions_recovered"] += min(ce,cr)
        cases.append({"question":case["question"],"returned":len(ev),"expected_recovered":len(matched)})
    return {"schema_version":"1.0","model":model or load_config(root)["model"],"metrics":totals,"cases":cases,
            "runtime_seconds":round(time.monotonic()-started,3),"peak_rss_mb":round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/(1024 if sys_platform()!="darwin" else 1024*1024),2)}

def sys_platform():
    import sys; return sys.platform

def main(argv=None):
    p=argparse.ArgumentParser(description="Benchmark repo-research evidence retrieval")
    p.add_argument("spec"); p.add_argument("--model"); args=p.parse_args(argv)
    print(json.dumps(run_benchmark(args.spec,args.model),indent=2))

if __name__ == "__main__": main()
