"""Reproduce the recent-change comparison without modifying either source tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import yaml


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SNAPSHOTS = ROOT / "build/recent-changes-20261031/sources"
FAULTS = [
    ("valid-output", "Accept valid evidence"),
    ("invented-quotation", "Reject invented quotations"),
    ("invented-source", "Reject invented source IDs"),
    ("missing-acknowledgements", "Flag missing processing acknowledgements"),
    ("unacknowledged-empty-output", "Flag an unacknowledged empty response"),
    ("truncated-output", "Flag output cut off by the model limit"),
    ("context-overflow", "Block a request beyond the context budget"),
    ("genuine-no-match", "Allow a genuine no-match result"),
]


def config(root, *, fault=None):
    cfg = {"model": "qwen3.5:9b-mlx", "temperature": 0,
           "context_tokens": 32768, "max_output_tokens": 1800,
           "candidate_limit": 8, "request_timeout_seconds": 120,
           "frontier_output_budget": "compact", "max_calls_per_run": 24,
           "max_calls_per_proposition": {"quick": 8, "standard": 16, "deep": 32},
           "embedding_model": None if fault else "qwen3-embedding:0.6b"}
    if fault == "context-overflow":
        cfg.update(context_tokens=128, max_output_tokens=256, context_safety_margin_tokens=256)
    state = root / ".codex-research"
    state.mkdir(parents=True, exist_ok=True)
    (state / "config.yaml").write_text(yaml.safe_dump(cfg))


def invoke(out, version, case_id, phase, question, fault=None):
    folder = out / "jobs" / case_id / version
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{phase}-job.json"
    root = out / "corpora" / case_id / version
    job = {"root": str(root), "question": question, "fault": fault,
           "output": str(folder / f"{phase}-result.json")}
    path.write_text(json.dumps(job, indent=2) + "\n")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SNAPSHOTS / version / "src")
    started = time.perf_counter()
    completed = subprocess.run([sys.executable, str(HERE / "worker.py"), str(path)], env=env,
                               capture_output=True, text=True, timeout=420)
    if completed.returncode:
        raise RuntimeError(completed.stderr or completed.stdout)
    payload = json.loads(Path(job["output"]).read_text())
    assert str(SNAPSHOTS / version / "src") in payload["module"], payload["module"]
    payload["subprocess_elapsed_seconds"] = time.perf_counter() - started
    return payload


def normalize(text):
    return " ".join(text.casefold().split())


def score(case, payload):
    delivered = []
    for unit in payload["units"]:
        text = unit.get("excerpt", "") + "\n" + "\n".join(
            block["text"] for block in unit.get("context_blocks", []))
        delivered.append((unit["source_path"], normalize(text)))
    checks = [{**anchor, "found": any(path == anchor["source_path"] and
               normalize(anchor["contains"]) in text for path, text in delivered)}
              for anchor in case["required"]]
    correct_empty = not payload["units"] if case.get("expected_empty") else None
    success = payload["completed"] and (correct_empty if case.get("expected_empty")
                                        else all(c["found"] for c in checks))
    return {"case_id": case["id"], "seconds": payload["elapsed_seconds"],
            "model_calls": payload["model_calls"], "completed": payload["completed"],
            "required_evidence_found": sum(c["found"] for c in checks),
            "required_evidence_total": len(checks), "checks": checks,
            "correct_empty": correct_empty, "task_success": success, "error": payload["error"]}


def safety_score(fault, payload):
    flagged = payload["error"] is not None or not payload["completed"]
    if fault == "valid-output":
        return bool(payload["units"]) and payload["completed"]
    if fault == "genuine-no-match":
        return not payload["units"] and payload["completed"]
    if fault in {"invented-quotation", "invented-source"}:
        return not payload["units"]
    if fault == "context-overflow":
        return flagged and payload["model_calls"] == 0
    return flagged


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["all", "live", "safety"], default="all")
    parser.add_argument("--cases")
    parser.add_argument("--output", type=Path, default=ROOT / "build/recent-changes-20261031/measured")
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    case_bytes = (HERE / "cases.json").read_bytes()
    cases = json.loads(case_bytes)
    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [case for case in cases if case["id"] in wanted]
        assert len(cases) == len(wanted)
    report = {"cases_sha256": hashlib.sha256(case_bytes).hexdigest(),
              "source_snapshots": str(SNAPSHOTS.parent / "source-snapshots.json"),
              "live": [], "safety": [], "notes": [
                  "Before is git HEAD; after is a pinned snapshot of the current uncommitted Python sources.",
                  "Same local generation model, temperature, questions, document bytes and common limits.",
                  "The after pipeline uses its new embedding retrieval and evidence-bundle delivery.",
                  "Models are warmed before measurement. Timing includes research plus standard evidence delivery, but excludes Python interpreter startup.",
                  "Repeated searches reuse each case's local research workspace. Initial workspaces are empty.",
                  "Safety checks inject fixed model failures and include valid-output and genuine-no-match controls.",
                  "These are functional tests and local timings; no final answer hallucination rate is measured."]}
    report_path = out / f"{args.phase}-report.json"

    def save():
        report_path.write_text(json.dumps(report, indent=2) + "\n")

    if args.phase in {"all", "live"}:
        for i, case in enumerate(cases):
            for version in ("before", "after"):
                root = out / "corpora" / case["id"] / version
                if root.exists():
                    raise RuntimeError(f"Refusing to reuse a supposedly fresh corpus: {root}")
                root.mkdir(parents=True)
                for name, text in case["documents"].items():
                    (root / name).write_text(text)
                config(root)
            versions = ["before", "after"] if i % 2 == 0 else ["after", "before"]
            for phase in ("fresh", "repeat"):
                for version in versions if phase == "fresh" else reversed(versions):
                    payload = invoke(out, version, case["id"], phase, case["question"])
                    entry = {"version": version, "phase": phase, **score(case, payload)}
                    report["live"].append(entry)
                    print(json.dumps({k: entry[k] for k in ["case_id", "version", "phase", "seconds", "model_calls", "completed", "task_success"]}), flush=True)
                    save()
    if args.phase in {"all", "safety"}:
        for fault, label in FAULTS:
            for version in ("before", "after"):
                root = out / "corpora" / f"safety-{fault}" / version
                if root.exists():
                    raise RuntimeError(f"Safety workspace already exists: {root}")
                root.mkdir(parents=True)
                (root / "falcon.txt").write_text("The Falcon approval was signed on 15 April 2025.")
                config(root, fault=fault)
                payload = invoke(out, version, f"safety-{fault}", "safety", "When was the Falcon approval signed?", fault)
                entry = {"version": version, "test": fault, "label": label,
                         "passed": safety_score(fault, payload), "model_calls": payload["model_calls"],
                         "completed": payload["completed"], "error": payload["error"],
                         "returned_evidence": len(payload["units"])}
                report["safety"].append(entry)
                print(json.dumps(entry), flush=True)
                save()
    summary = {}
    for version in ("before", "after"):
        summary[version] = {}
        for phase in ("fresh", "repeat"):
            rows = [r for r in report["live"] if r["version"] == version and r["phase"] == phase]
            if rows:
                summary[version][phase] = {"n": len(rows), "median_seconds": statistics.median(r["seconds"] for r in rows),
                    "total_model_calls": sum(r["model_calls"] for r in rows),
                    "completed": sum(r["completed"] for r in rows),
                    "task_success": sum(r["task_success"] for r in rows)}
        rows = [r for r in report["safety"] if r["version"] == version]
        if rows:
            summary[version]["safety"] = {"passed": sum(r["passed"] for r in rows), "n": len(rows)}
    report["summary"] = summary
    save()
    print(json.dumps({"report": str(report_path), "summary": summary}, indent=2), flush=True)


if __name__ == "__main__":
    main()
