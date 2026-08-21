---
name: local-repo-research
description: Reduce frontier file-reading by delegating repository-scale evidence screening to a local Ollama worker while retaining source provenance and selective Codex adjudication. Use for many files, unknown evidence locations, repeated searches, chronology, contradictions, or source tracing. Work directly for narrow known-file inspection or edits.
---

# Local Repository Research

Use `repo-research` as a cost-saving local evidence scout. Qwen screens indexed
repository text; Codex receives a bounded evidence packet and remains responsible
for adjudication. Repository contents stay local.

## Route by economics and risk

Delegate repository-wide discovery, repeated factual searches, ordinary amounts
or status statements, chronology reconnaissance, and finding known language in
routine reports. Prefer `quick` plus the `compact` packet when frontier-context
savings are the main objective.

Work directly from the likely primary source when one or two files are known, or
when the claim concerns signatures or legal effect, amendment arithmetic,
proposal versus execution, planned versus operational status, superseding
decisions, causal/counterfactual interpretation, or a consequential no-result.
Qwen can locate candidates for these claims but should not adjudicate them.

## Workflow

1. Express the objective as focused, assessable propositions.
2. Start with `quick --output-budget compact`; escalate depth or packet size only
   when the expected audit value justifies more local work or frontier context.
3. Judge promoted `E####` records first. Retrieve adjacent context or open the
   original only when the packet lacks the subject, amount, denominator, stage,
   date, authority, or conflicting decision needed for adjudication.
4. Reuse a source check across propositions and count unique files opened when
   evaluating context savings.
5. Treat deterministic span validation as proof that text exists, not that the
   claim is true. Separate what a document says from what the record establishes.
6. Stop delegating when selective checks approach the likely direct-search
   workload. The local worker is defeated if Codex rereads the corpus.

Use a separate contradiction or source-trace pass only for consequential claims.
Preserve gaps and the searched scope behind any no-result conclusion.

Read [references/cli-and-schema.md](references/cli-and-schema.md) for commands,
budgets, persistence, telemetry, OCR, and progressive disclosure.
