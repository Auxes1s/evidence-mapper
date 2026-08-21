# CLI and evidence contract

Start or verify the local Ollama CLI service before searching:

```sh
if ! ollama list >/dev/null 2>&1; then
  nohup ollama serve >/tmp/repo-research-ollama.log 2>&1 &
  startup_attempt=0
  until ollama list >/dev/null 2>&1; do
    startup_attempt=$((startup_attempt + 1))
    if [ "$startup_attempt" -ge 30 ]; then
      tail -n 20 /tmp/repo-research-ollama.log >&2
      exit 1
    fi
    sleep 1
  done
fi
repo-research --health
```

Use `ollama serve`, not `ollama run`: `serve` provides the HTTP endpoint that
`repo-research` needs, while `run` starts an interactive model session. If the
server is available but health still fails, check that the configured model is
installed (the default is `qwen3.5:9b-mlx`) and inspect the reported error.

```sh
repo-research --health
repo-research --path . --mode inventory --json
repo-research --path . --mode evidence --depth quick --output-budget compact --question "Atomic proposition" --fresh --json
repo-research --path . --mode contradictions --depth standard --question "Claim to challenge" --fresh --json
repo-research --path . --mode source-trace --question "Trace this claim to its primary source" --json

repo-research --path . --get-evidence E0014 --json
repo-research --path . --expand-evidence-context E0014 --radius 1 --json
repo-research --path . --open-source-location E0014 --json
repo-research --path . --show-related-evidence E0014 --json
repo-research --path . --show-contradiction-evidence E0014 --json
repo-research --path . --telemetry SEARCH_ID --json
```

Modes are `inventory`, `evidence`, `contradictions`, `chronology`, `exhaustive`,
`gap-search`, and `source-trace`. Depths are `quick`, `standard`, and `deep`.

## Budgets and persistence

`compact`, `standard`, and `expanded` target approximately 750, 1,500, and 4,000
frontier-facing tokens. They do not cap local indexing or inference. Full validated
evidence remains in `.codex-research/evidence.jsonl`; promoted records keep stable
`E####` IDs, exact deterministic spans, source hashes, and format-aware locators.

Repository state also includes the SQLite/FTS index, searches, unresolved findings,
malformed Qwen responses, extraction failures, packets, and telemetry. One
constrained retry is used for malformed Qwen JSON, and each malformed response is
persisted in `qwen_failures.jsonl`.

Telemetry estimates candidate files, local input/output tokens, inference time,
promoted tokens, and later source expansions. These are research-context estimates,
not billing data. For an economics comparison, separately count unique originals
Codex opens and compare total frontier research context with candidate source text.

## Configuration

Configuration precedence is defaults, user config, explicit `--config`, then the
repository's `.codex-research/config.yaml`. Useful controls include
`candidate_limit`, `max_prompt_chars`, `frontier_output_budget`, include/exclude
patterns, and the single `model` setting.

Enable non-destructive OCR for image-only PDFs with:

```yaml
ocr_image_only_pdfs: true
ocr_max_pages: 120
ocr_dpi: 200
ocr_timeout_per_page_seconds: 90
```

Use `--fresh` to bypass reuse, `--challenge-existing` for an independent challenge,
and `--rebuild` after extraction/parser changes.
