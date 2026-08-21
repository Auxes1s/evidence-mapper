"""Fixed, cost-oriented SMEP benchmark. This is not a full audit."""
from __future__ import annotations

import json
from pathlib import Path

from repo_research.research import research

ROOT = Path("/Users/marcignacio/Library/CloudStorage/OneDrive-NationalEconomicandDevelopmentAuthority/&IPG-ceu - Documents")
OUTPUT = Path("/private/tmp/smep-local-benchmark/economics-current.jsonl")

PROPOSITIONS = [
    {"id":"P01", "type":"simple-date", "claim":"The SMEP Partnership Agreement was signed in December 2017."},
    {"id":"P02", "type":"simple-amount", "claim":"SMEP began as a PHP 190 million four-component initiative."},
    {"id":"P03", "type":"amendments", "claim":"Three funding amendments in 2018, 2019, and 2022 increased SMEP to PHP 319.6 million."},
    {"id":"P04", "type":"amendment-primary", "claim":"The third Partnership Agreement amendment was signed on 29 March 2022 and transferred PHP 25.6 million."},
    {"id":"P05", "type":"delivery", "claim":"By December 2025, SMEP had delivered more than 95 percent of its resources."},
    {"id":"P06", "type":"proposal-v-execution", "claim":"At the November 2021 Board meeting, DEPDev requested a five-person CEU nucleus by the end of the first quarter of 2022; this was a request, not proof it was staffed by then."},
    {"id":"P07", "type":"execution", "claim":"By 2022, SMEP supported the establishment and operation of the Central Evaluation Unit."},
    {"id":"P08", "type":"extension", "claim":"SMEP was extended through the end of the second quarter of 2026 for the financial audit only."},
    {"id":"P09", "type":"policy-primary", "claim":"The revised National Evaluation Policy Framework was signed in April 2025."},
    {"id":"P10", "type":"institutional-transition", "claim":"The Strategic Outcome Evaluation Division was established under the Monitoring and Evaluation Staff and adopted the CEU model."},
    {"id":"P11", "type":"conflict-delay", "claim":"A failed 2022 procurement for PPMS Phase 2 pushed implementation into 2023."},
    {"id":"P12", "type":"expected-no-result", "claim":"SMEP doubled DEPDev's evaluation capacity by 2023."},
]

def main() -> None:
    OUTPUT.unlink(missing_ok=True)
    for spec in PROPOSITIONS:
        try:
            result = research(ROOT, spec["claim"], mode="evidence", depth="quick",
                              fresh=True, output_budget="compact")
            row = {**spec, "status":"completed", "result":result}
        except Exception as exc:
            row = {**spec, "status":"failed", "error":repr(exc)}
        with OUTPUT.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(json.dumps({"id":spec["id"], "status":row["status"],
                          "search_id":row.get("result",{}).get("search_id")} ), flush=True)

if __name__ == "__main__":
    main()
