---
name: local-repo-research
description: Reduce frontier file-reading by delegating repository-scale evidence screening to a local Ollama worker while retaining source provenance and selective Codex adjudication. Use for many files, unknown evidence locations, repeated searches, chronology, contradictions, or source tracing. Work directly for narrow known-file inspection or edits.
---

# Local Repository Research

Use `repo-research` as a local evidence scout with persistent, source-linked memory.
Qwen extracts and compares bounded groups of passages. Codex receives a compact
coverage overview, fetches proposition bundles, and makes the final judgment.
With the default loopback Ollama endpoint and local models,
repository contents stay on the machine; custom endpoints or cloud-backed models
change that privacy boundary.

## Route by economics and risk

Delegate repository-wide discovery, repeated factual searches, ordinary amounts
or status statements, chronology reconnaissance, and finding known language in
routine reports. Use `standard` depth with a `compact` overview for balanced
research; use `quick` for a narrowly bounded screening task.

Work directly from the likely primary source when one or two files are known, or
when the claim concerns signatures or legal effect, amendment arithmetic,
proposal versus execution, planned versus operational status, superseding
decisions, causal/counterfactual interpretation, or a consequential no-result.
Qwen can locate candidates for these claims but should not adjudicate them.

## Workflow

1. Run `repo-research --health` before the first search. If it fails because the
   Ollama endpoint is unavailable, start the CLI server in the background with
   `nohup ollama serve >/tmp/repo-research-ollama.log 2>&1 &`, wait up to 30
   seconds for `ollama list` to succeed, and rerun the health check. Do not open
   the Ollama GUI. If the server starts but health still fails, report the model
   or configuration error instead of starting an interactive `ollama run` session.
2. Supply focused, assessable propositions as a JSON list of `{id,text}` through
   `--propositions @file`. A question-only call is one proposition; the worker
   does not infer a reliable decomposition of a broad question.
3. Read the overview's processing status, search scope, and per-proposition gaps.
   Follow `next_cursor` with `--overview RUN_ID --cursor CURSOR` until each
   proposition has been accounted for.
4. Fetch the relevant bundles using `--get-bundle BUNDLE_ID --run-id RUN_ID`.
   Follow all bundle pages needed for the judgment. Read original passages,
   conditions, dates, headers, and adverse evidence together. An inspection
   requirement points to an oversized complete unit; expand its `E####` record
   with the same `--run-id`, or inspect the original and check its source hash.
5. Reuse a source check across propositions and count unique files opened when
   evaluating context savings.
6. Treat deterministic span validation as proof that text exists, not that the
   claim is true. Separate what a document says from what the record establishes.
7. Stop delegating when selective checks approach the likely direct-search
   workload. The local worker is defeated if Codex rereads the corpus.
8. Resume deferred or interrupted work with `--resume RUN_ID`. Resume uses the
   saved corpus snapshot and settings; source changes belong to a new run.
   `complete_for_retrieved_scope` establishes processing completion only.
   `all_eligible_passages_screened` is explicit structural coverage, and still
   does not establish perfect semantic recall. Keep source-span verification
   distinct from candidate interpretations and final conclusions.

The scheduler performs a challenge search and bounded context follow-ups. Use an
additional independent search when consequential uncertainties remain. Preserve
the searched scope and outstanding tasks behind every no-result conclusion.

Read [references/cli-and-schema.md](references/cli-and-schema.md) for commands,
budgets, persistence, telemetry, OCR, and progressive disclosure.
