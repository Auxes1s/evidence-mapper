from __future__ import annotations

import argparse, json, sys
from pathlib import Path
from . import __version__
from .config import load_config
from .ollama import OllamaBackend
from .research import MODES, research
from .retrieval import (contradiction_evidence, expand_evidence_context, get_evidence,
                        open_source_location, related_evidence, telemetry)

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="repo-research", description="Local-first repository evidence retrieval with Ollama")
    p.add_argument("--path", default=".", help="Repository or document tree to inspect")
    p.add_argument("--mode", choices=sorted(MODES), default="inventory")
    p.add_argument("--question", help="Focused research question or proposition")
    p.add_argument("--depth", choices=["quick","standard","deep"], default="standard")
    p.add_argument("--output-budget", choices=["compact","standard","expanded"], default=None,
                   help="Frontier-facing evidence-packet budget (default: standard)")
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
    p.add_argument("--radius", type=int, default=2, help="Context radius for --expand-evidence-context")
    p.add_argument("--json", action="store_true", help="Emit stable machine-readable JSON (default is a concise report)")
    p.add_argument("--health", action="store_true", help="Check Ollama and configured model, then exit")
    p.add_argument("--version", action="version", version=f"repo-research {__version__}")
    return p

def _human(result: dict) -> str:
    s = result["summary"]
    lines = [f"Search {result['search_id']} — {result['mode']} ({result['depth']})",
      f"Repository files indexed: {s['repository_files_indexed']}",
      f"Potentially relevant files: {s['potentially_relevant_files']}",
      f"Files substantively inspected: {s['files_substantively_inspected']}",
      f"Evidence records persisted/promoted: {s['evidence_records_persisted']}/{s['evidence_records_returned']}",
      f"Contradictory/qualifying records: {s['contradictory_or_qualifying_records']}",
      f"Extraction failures: {s['extraction_failures']}"]
    if "inventory" in result:
        lines += ["\nInventory:", json.dumps(result["inventory"], indent=2, ensure_ascii=False)]
    for e in result.get("evidence", []):
        loc = e["source_path"]
        for key, value in e.get("location", {}).items(): loc += f"; {key}={value}"
        lines += [f"\n[{e['evidence_type']}/{e['stage']}] {loc}", f'  “{e["excerpt"]}”',
                  f"  {e.get('note','')} (ID: {e['id']}; quote verified: {e['quote_verified']})"]
    packet=result.get("evidence_packet",{})
    if packet:
        lines += [f"\nPromoted packet: {len(packet.get('promoted_ids',[]))} records, ~{packet.get('estimated_tokens',0)} tokens ({packet.get('budget')})",
                  "Retrieve more with --get-evidence/--expand-evidence-context/--open-source-location/--show-related-evidence/--show-contradiction-evidence."]
    if result.get("reused_evidence"): lines.append(f"\nReusable current evidence surfaced: {len(result['reused_evidence'])}")
    return "\n".join(lines)

def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        cfg = load_config(Path(args.path).resolve(), args.config)
        if args.health:
            print(json.dumps(OllamaBackend(cfg).health(), indent=2)); return 0
        retrieval = None
        if args.get_evidence: retrieval = get_evidence(args.path, args.get_evidence)
        elif args.expand_evidence_context: retrieval = expand_evidence_context(args.path, args.expand_evidence_context, args.radius)
        elif args.open_source_location: retrieval = open_source_location(args.path, args.open_source_location)
        elif args.show_related_evidence: retrieval = related_evidence(args.path, args.show_related_evidence)
        elif args.show_contradiction_evidence: retrieval = contradiction_evidence(args.path, args.show_contradiction_evidence)
        elif args.telemetry: retrieval = telemetry(args.path, args.telemetry)
        if retrieval is not None:
            print(json.dumps(retrieval, indent=2, ensure_ascii=False)); return 0
        existing = args.existing_evidence
        if existing and existing.startswith("@"):
            existing = Path(existing[1:]).read_text(encoding="utf-8")
        result = research(args.path, args.question, args.mode, args.depth, fresh=args.fresh,
            challenge_existing=args.challenge_existing, existing_evidence=existing,
            config_path=args.config, rebuild=args.rebuild, output_budget=args.output_budget)
        print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else _human(result))
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"repo-research: {exc}", file=sys.stderr); return 2

if __name__ == "__main__": raise SystemExit(main())
