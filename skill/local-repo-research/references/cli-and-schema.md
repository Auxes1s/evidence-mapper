# CLI and persistent workspace

## Setup and research

Both models run locally through Ollama:

```sh
ollama pull qwen3.5:9b-mlx
ollama pull qwen3-embedding:0.6b
repo-research --health
repo-research --path ./documents --mode inventory --json
repo-research --path ./documents --mode evidence \
  --question "What authorized the extension?" --depth standard --json
```

For several claims, supply explicit propositions:

```json
[
  {"id":"P01","text":"What authorized the extension?"},
  {"id":"P02","text":"What conditions apply to the extension?"}
]
```

```sh
repo-research --path ./documents --mode evidence --propositions @claims.json --json
repo-research --path ./documents --overview RUN_ID --cursor CURSOR --json
repo-research --path ./documents --get-bundle BUNDLE_ID --run-id RUN_ID --json
repo-research --path ./documents --get-bundle BUNDLE_ID --run-id RUN_ID --cursor CURSOR --json
repo-research --path ./documents --resume RUN_ID --json
```

A resume grants another bounded work allowance against the saved snapshot. It
rejects changed model identities and incompatible new research arguments. Bundle
cursors are tied to their content; overview cursors are invalidated by a resume.

## Schema 3.0 and progressive disclosure

`research()` returns a coverage overview with a paginated `propositions` list.
Each entry has `id`, `text`, `status`, `bundle_id`, `evidence_count`, and
`unresolved_count`. Very long proposition text is available in the bundle rather
than clipped into the overview. The initial `evidence` array is empty.

`get_bundle(path, run_id, bundle_id, cursor=None, budget_tokens=None)` returns
whole `units`, candidate `relations`, `unresolved` issues, and continuation cursors.
Every unit retains its exact excerpt, source hash and location, context blocks,
event/document dates when extracted, stage, and source-span verification status.
Interpretations remain candidates for Codex. A unit that cannot fit generates an
`inspection_requirements` entry; its text is never shortened to fit the budget.

```sh
repo-research --path ./documents --get-evidence E0001 --run-id RUN_ID --json
repo-research --path ./documents --expand-evidence-context E0001 --run-id RUN_ID --json
repo-research --path ./documents --open-source-location E0001 --run-id RUN_ID --json
repo-research --path ./documents --show-related-evidence E0001 --json
repo-research --path ./documents --show-contradiction-evidence E0001 --json
repo-research --path ./documents --coverage-report RUN_ID --json
repo-research --path ./documents --run-manifest RUN_ID --json
repo-research --path ./documents --telemetry RUN_ID --json
```

Evidence context is retained from the run's extraction. A current source file may
have changed; compare hashes before treating the current original as that version.
Run manifests expose tasks, exact model calls, source versions, evidence, bundles,
and provisional relationships. Legacy evidence IDs and getters remain supported.

## Budgets and coverage

`compact`, `standard`, and `expanded` target approximately 750, 1,500, and 4,000
delivery tokens for the complete response envelope. The initial default is compact;
bundle fetches default to 4,000. Estimates are not exact Codex usage. Full local
evidence and work state do not share this delivery cap.

Depth defaults are:

| Depth | Retrieval rounds per proposition | Generation calls per proposition |
|---|---:|---:|
| quick | 2 | 8 |
| standard | 4 | 16 |
| deep | 7 | 32 |

The run ceiling is 96 generation calls per invocation, with 12 candidates per
retrieval page and one local worker. Context follows up at most eight requests
per proposition. Deferred and failed work remains visible and resumable.

`coverage.processing_complete` describes scheduled work. `coverage.search_scope`
is `bounded` unless exhaustive processing actually screens every eligible extracted
passage without omissions. `coverage.stop_reason` distinguishes saturation,
budgets, errors, and scope exhaustion. An exhausted budget is never proof of absence.
`exhaustive` enumerates the selected snapshot instead of limiting screening to
retrieval hits; generation budgets still apply.

## Storage and configuration

The authoritative ledger is `.codex-research/audit.sqlite3`. It stores versioned
text, document/block links, stable evidence IDs, tasks, model calls, candidate
relationships, and bundles. Search indexes and JSONL exports can be rebuilt.
Migration makes an `audit.before-v3.sqlite3` backup and preserves existing IDs.

Configuration precedence remains defaults, user config, explicit `--config`, then
repository config. Hybrid retrieval defaults to `qwen3-embedding:0.6b`; model
availability is checked before research. Set `embedding_model: null` explicitly
for lexical-only research. Embedding cache identities include the model digest,
source version, and preprocessing version. Embedding inputs reject truncation.

Useful settings: `candidate_limit`, `max_research_rounds`,
`max_calls_per_proposition`, `max_calls_per_run`, `max_context_followups`,
`bundle_budget_tokens`, `context_tokens`, `max_output_tokens`, and
`context_safety_margin_tokens`. Every generation request, including retries,
counts the schema and reserves output and safety capacity. An optional local
`tokenizer_path` uses the `tokenizer` package extra; conservative byte-based
preflight remains the default.

Enable image-only PDF OCR with `ocr_image_only_pdfs: true`. `--rebuild` forces
re-extraction; parser-version changes also invalidate old extraction caches.
`--fresh` and `--challenge-existing` bypass generation reuse.
