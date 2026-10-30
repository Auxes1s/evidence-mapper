from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import load_config
from .ollama import OllamaBackend
from .research import MODES, research
from .bundles import get_bundle, overview
from .retrieval import (
    contradiction_evidence,
    coverage_report,
    expand_evidence_context,
    get_evidence,
    open_source_location,
    related_evidence,
    run_manifest,
    telemetry,
)

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="repo-research", description="Local-first repository evidence retrieval with Ollama")
    p.add_argument("--path", default=".", help="Repository or document tree to inspect")
    p.add_argument("--mode", choices=sorted(MODES), default="inventory")
    p.add_argument("--question", help="Focused research question or proposition")
    p.add_argument("--propositions",help="JSON list of explicit {id,text} propositions (literal or @file)")
    p.add_argument("--resume",metavar="RUN_ID",help="Resume saved work against the original corpus snapshot")
    p.add_argument("--get-bundle",metavar="BUNDLE_ID",help="Fetch whole evidence units for one proposition")
    p.add_argument("--run-id",help="Run owning a bundle or evidence annotation")
    p.add_argument("--overview",metavar="RUN_ID",help="Fetch a paginated research overview")
    p.add_argument("--cursor",help="Opaque continuation cursor for overview or bundle fetching")
    p.add_argument("--depth", choices=["quick","standard","deep"], default="standard")
    p.add_argument("--output-budget", choices=["compact","standard","expanded"], default=None,
                   help="Frontier-facing overview budget (default: compact)")
    p.add_argument("--config", help="Additional YAML configuration")
    p.add_argument("--fresh", action="store_true", help="Ignore reusable prior results and search independently")
    p.add_argument("--challenge-existing", action="store_true", help="Run an independent challenge search")
    p.add_argument("--existing-evidence", help="Evidence register or gap description (literal text or @file)")
    p.add_argument("--rebuild", action="store_true", help="Re-extract all supported sources")
    p.add_argument("--get-evidence", metavar="EVIDENCE_ID", help="Retrieve one persisted evidence record by E#### ID")
    p.add_argument("--expand-evidence-context", metavar="EVIDENCE_ID", help="Retrieve surrounding indexed context by ID")
    p.add_argument("--open-source-location", metavar="EVIDENCE_ID", help="Resolve original source path and locator by ID")
    p.add_argument("--show-related-evidence", metavar="EVIDENCE_ID", help="Show related persisted evidence by ID")
    p.add_argument("--show-contradiction-evidence", metavar="EVIDENCE_ID", help="Show contradictory evidence for the same question")
    p.add_argument("--telemetry", metavar="SEARCH_ID", help="Show updated research telemetry for a job")
    p.add_argument("--coverage-report", metavar="RUN_ID", help="Show reconciled corpus, retrieval, processing, and synthesis coverage")
    p.add_argument("--run-manifest", metavar="RUN_ID", help="Export exact corpus, retrieval, call, evidence, and claim provenance")
    p.add_argument("--radius", type=int, default=2, help="Context radius for --expand-evidence-context")
    p.add_argument("--json", action="store_true", help="Emit stable machine-readable JSON (default is a concise report)")
    p.add_argument("--health", action="store_true", help="Check Ollama and configured model, then exit")
    p.add_argument("--version", action="version", version=f"repo-research {__version__}")
    return p

def _human(result: dict) -> str:
    lines=[f"Search {result['search_id']} — {result['mode']} ({result['depth']})",
           f"Run status: {result['status']}",
           f"Search scope: {result['coverage']['search_scope']}; stop: {result['coverage']['stop_reason']}"]
    for prop in result.get("propositions",[]):
        lines.append(f"[{prop['id']}] {prop.get('text','Fetch bundle for full proposition')}: {prop['status']} — {prop['evidence_count']} evidence units; bundle {prop['bundle_id']}")
    if result.get("next_cursor"): lines.append(f"More propositions: --overview {result['search_id']} --cursor {result['next_cursor']}")
    lines.append("Fetch evidence with --get-bundle BUNDLE_ID --run-id RUN_ID. Interpretations remain candidates.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        cfg = load_config(Path(args.path).resolve(), args.config)
        if args.health:
            worker=OllamaBackend(cfg); health=worker.health()
            if cfg.get("embedding_model"): health["embedding_model"]=worker.model_info(cfg["embedding_model"])
            print(json.dumps(health, indent=2)); return 0
        retrieval = None
        budget=cfg["frontier_output_budgets"][args.output_budget] if args.output_budget else None
        if args.get_bundle:
            if not args.run_id: raise ValueError("--get-bundle requires --run-id")
            retrieval=get_bundle(args.path,args.run_id,args.get_bundle,args.cursor,budget)
        elif args.overview: retrieval=overview(args.path,args.overview,args.cursor,budget)
        elif args.get_evidence: retrieval = get_evidence(args.path, args.get_evidence,args.run_id)
        elif args.expand_evidence_context: retrieval = expand_evidence_context(args.path, args.expand_evidence_context, args.radius,args.run_id)
        elif args.open_source_location: retrieval = open_source_location(args.path, args.open_source_location,args.run_id)
        elif args.show_related_evidence: retrieval = related_evidence(args.path, args.show_related_evidence)
        elif args.show_contradiction_evidence: retrieval = contradiction_evidence(args.path, args.show_contradiction_evidence)
        elif args.telemetry: retrieval = telemetry(args.path, args.telemetry)
        elif args.coverage_report: retrieval = coverage_report(args.path, args.coverage_report)
        elif args.run_manifest: retrieval = run_manifest(args.path, args.run_manifest)
        if retrieval is not None:
            print(json.dumps(retrieval, separators=(",",":"), ensure_ascii=False)); return 0
        existing = args.existing_evidence
        if existing and existing.startswith("@"):
            existing = Path(existing[1:]).read_text(encoding="utf-8")
        props=args.propositions
        if props and props.startswith("@"): props=Path(props[1:]).read_text(encoding="utf-8")
        if props: props=json.loads(props)
        result = research(args.path, args.question, args.mode, args.depth, fresh=args.fresh,
            challenge_existing=args.challenge_existing, existing_evidence=existing,
            config_path=args.config, rebuild=args.rebuild, output_budget=args.output_budget,
            propositions=props,resume=args.resume)
        print(json.dumps(result, separators=(",",":"), ensure_ascii=False) if args.json else _human(result))
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"repo-research: {exc}", file=sys.stderr); return 2

if __name__ == "__main__": raise SystemExit(main())
