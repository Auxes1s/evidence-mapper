# repo-research

**Let a local evidence scout search your files while the main agent makes the final judgment.**

`repo-research` processes large folders through bounded local-model tasks. Research
memory lives in a persistent, source-linked workspace, so each call can work with
a small context window. Codex receives a compact coverage overview and fetches
coherent evidence bundles when needed.

```text
documents → versioned text and structure → keyword + embedding indexes
                                               ↓
                                      research scheduler
                                       ↕             ↕
                              bounded local tasks   evidence ledger
                                                         ↓
                                              proposition bundles
                                                         ↓
                                             overview → Codex fetch
```

## Install

Requires Python 3.10+, Ollama, and two local models:

```sh
ollama serve
# In another terminal:
ollama pull qwen3.5:9b-mlx
ollama pull qwen3-embedding:0.6b
./install.sh
repo-research --health
```

The installer supports `REPO_RESEARCH_INSTALL_ROOT`, `REPO_RESEARCH_BIN_DIR`, and
`REPO_RESEARCH_SKILLS_DIR`. The default model names can be changed in configuration.
Set `embedding_model: null` explicitly to use lexical retrieval alone. Hybrid mode
reports a setup error if the embedding model is unavailable.

Extraction, indexes, embeddings, and inference use the local Ollama endpoint by
default. Custom endpoints or cloud-backed models change that privacy boundary.

## Research and inspect

```sh
repo-research --path ./documents --mode inventory --json
repo-research --path ./documents --mode evidence \
  --question "What authorized the project extension?" --depth standard --json
```

The response uses schema **3.0**: a coverage overview, paginated propositions,
bundle IDs, and explicit unresolved counts. Source passages stay in the local
ledger until requested. A question-only call represents one proposition. For
several questions, write a JSON list to `claims.json`:

```json
[
  {"id":"P01","text":"What authorized the extension?"},
  {"id":"P02","text":"Which conditions apply to the extension?"}
]
```

```sh
repo-research --path ./documents --mode evidence --propositions @claims.json --json
repo-research --path ./documents --overview RUN_ID --cursor CURSOR --json
repo-research --path ./documents --get-bundle BUNDLE_ID --run-id RUN_ID --json
repo-research --path ./documents --get-bundle BUNDLE_ID --run-id RUN_ID --cursor CURSOR --json
repo-research --path ./documents --resume RUN_ID --json
```

Bundles preserve full excerpts, relevant paragraphs, conditions, table headers,
dates, source hashes, and locations. They expose supporting, qualifying, and adverse
evidence together. Complete units paginate without character clipping. If one unit
exceeds the delivery budget, the response identifies the evidence and required
source inspection.

```sh
repo-research --path ./documents --get-evidence E0001 --run-id RUN_ID --json
repo-research --path ./documents --expand-evidence-context E0001 --run-id RUN_ID --json
repo-research --path ./documents --open-source-location E0001 --run-id RUN_ID --json
repo-research --path ./documents --coverage-report RUN_ID --json
repo-research --path ./documents --run-manifest RUN_ID --json
repo-research --path ./documents --telemetry RUN_ID --json
```

The existing related-evidence and contradiction-evidence getters remain available.
Legacy `E####` identifiers survive migration. The initial `evidence` array is empty
in schema 3.0; consumers should follow proposition bundle IDs.

## Python API

```python
from repo_research import research, get_bundle, overview

result = research(
    path="./documents", mode="evidence",
    propositions=[{"id": "P01", "text": "What authorized the extension?"}],
)
bundle = get_bundle("./documents", result["search_id"],
                    result["propositions"][0]["bundle_id"])
# Follow next_cursor until the relevant overview/bundle has been inspected.
```

## How bounded research works

The scheduler gives each proposition work fairly. It retrieves fresh candidates,
extracts exact spans, performs a challenge search, follows requested context, and
compares related original passages. Workers return candidate interpretations;
Codex retains final judgment. Every relationship cites persisted evidence IDs.

Processing stops after saturation, scope exhaustion, a budget, or a recorded
failure. Pending work is resumable against the original corpus snapshot even if
source files later change. Changed model identities require a new run.

| Depth | Maximum retrieval rounds per proposition | Generation calls per proposition |
|---|---:|---:|
| quick | 2 | 8 |
| standard | 4 | 16 |
| deep | 7 | 32 |

Defaults allow 12 candidates per retrieval page, 96 generation calls per invocation,
eight context follow-ups per proposition, and one local worker. Embedding work is
cached separately. Limits are configurable.

`compact`, `standard`, and `expanded` delivery budgets target approximately 750,
1,500, and 4,000 tokens for the entire response. Overview delivery defaults to
compact; bundle fetching defaults to expanded. Estimates are not exact Codex usage.
Every model request preflights its schema, prompt, output reserve, and safety
margin. Retries are preflighted; output exhaustion splits work once before an
explicit failure. Oversized inputs are never silently discarded.

## Coverage and interpretation

Coverage reports separate:

- **Processing:** whether scheduled tasks were completed and acknowledged.
- **Search scope:** bounded retrieval or all eligible extracted passages screened.
- **Evidence sufficiency:** findings and remaining gaps for each proposition.

`complete_for_retrieved_scope` establishes processing completion. It does not prove
that every relevant passage was discovered. `exhaustive` enumerates every eligible
extracted passage in the selected snapshot; budgets can defer remaining work.
Even full screening can contain model classification errors. Exact-span validation
establishes source fidelity, while interpretation remains provisional.

Modes: `inventory`, `evidence`, `contradictions`, `chronology`, `exhaustive`,
`gap-search`, and `source-trace`. Use `--fresh` or `--challenge-existing` to bypass
model-output reuse, and `--rebuild` to force extraction.

## Storage, configuration, and supported files

Each researched folder receives `.codex-research/`. Its authoritative SQLite ledger
stores snapshots, source structure, stable IDs, tasks, calls, evidence, relationships,
and bundles. Search indexes and JSONL files are derived artifacts. Migration backs
up existing audit databases before upgrading and preserves historical runs.

Configuration precedence is built-in defaults, `~/.config/repo-research/config.yaml`,
explicit `--config`, then repository configuration. Embedding cache identities include
source versions, model digests, and preprocessing versions. Optional exact local
chat tokenization uses `pip install 'repo-research[tokenizer]'` and `tokenizer_path`.

Supported inputs include PDF, DOCX, XLSX/XLSM, CSV/TSV, plain text, Markdown,
structured text, HTML, LaTeX, and common source formats. Image-only PDF OCR is opt-in
and requires Poppler and Tesseract. Legacy `.xls` is unsupported. Extraction cannot
establish signatures or legal effect; consequential claims require source inspection.

## Validation and benchmarks

```sh
python3 -m pip install -e '.[dev]'
python3 -m pytest
repo-research-benchmark benchmark/synthetic-spec.example.json
```

Tests cover cross-document context, qualifiers and denominators, whole-unit
pagination, fair proposition coverage, pinned-snapshot resume, model cache
invalidation, migrations, failed dispositions, and generation budgets.

The older [SMEP economics benchmark](benchmark/SMEP_ECONOMICS_2026-08-20.md) describes
the previous packet architecture. Its measurements are historical and are not
performance guarantees for this release.
