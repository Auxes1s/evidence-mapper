# repo-research

`repo-research` is a local-first evidence retrieval subsystem. It deterministically extracts and indexes a repository, retrieves candidates with SQLite FTS, asks Ollama for exact evidence, validates quotations against source text, and persists an audit trail. Qwen scouts; Codex adjudicates.

## Architecture and requirements

```text
question -> format-aware extraction -> SQLite FTS candidates
         -> Ollama screening -> context expansion -> quote/location validation
         -> persistent local evidence -> ranked budgeted promotion
         -> selective Codex expansion and final judgment
```

No repository content is sent externally. Model access is isolated in `repo_research/ollama.py`. Requires Python 3.10+, Ollama, and a local model. Defaults for a 24 GB Apple Silicon Mac are `qwen3.5:9b-mlx`, 32K context, one generation, and temperature 0.1.

Supported inputs include PDF, DOCX, XLSX/XLSM, CSV/TSV, text, Markdown, JSON/YAML/TOML/XML/HTML, LaTeX/BibTeX, logs, and common source code. PDF pages, DOCX headings/paragraphs/tables, spreadsheet sheets/headers/rows, and source lines are retained. Optional Tesseract OCR can index image-only PDFs without modifying the originals.

## Installation

Install the CLI and bundled Codex skill:

```sh
./install.sh
repo-research --health
```

Set `REPO_RESEARCH_INSTALL_ROOT`, `REPO_RESEARCH_BIN_DIR`, or
`REPO_RESEARCH_SKILLS_DIR` to override their default user-local locations.

## CLI and Python API

```sh
repo-research --path . --mode inventory
repo-research --path . --mode evidence --question "What supports X?" --depth standard --json
repo-research --path . --mode contradictions --question "X happened in June 2025" --depth deep --fresh
repo-research --path . --mode chronology --question "Reconstruct the history of X"
repo-research --path . --mode exhaustive --question "Find all evidence about X" --depth deep
repo-research --path . --mode gap-search --question "What is missing for X?" --existing-evidence @evidence.json
repo-research --path . --mode source-trace --question "Find the original source for X"
repo-research --path . --mode evidence --question "What supports X?" --output-budget compact --json

# Progressive disclosure
repo-research --path . --get-evidence E0001 --json
repo-research --path . --expand-evidence-context E0001 --radius 2 --json
repo-research --path . --open-source-location E0001 --json
repo-research --path . --show-related-evidence E0001 --json
repo-research --path . --show-contradiction-evidence E0001 --json
repo-research --path . --telemetry SEARCH_ID --json
```

```python
from repo_research import research, get_evidence, expand_evidence_context
result = research(path=".", question="Find evidence about establishment and operation.", mode="evidence", depth="deep")
```

`quick` runs up to two strategies, `standard` four, and `deep` seven. `--fresh` bypasses reuse; `--challenge-existing` forces an independent challenge; `--rebuild` re-extracts everything.

Frontier-facing output budgets are `compact` (about 750 tokens), `standard` (about 1,500; default), and `expanded` (about 4,000). These do not constrain local indexing or Qwen inference. Full validated evidence stays local; at most 12 ranked records are promoted by default, with strong contradictory and qualifying evidence preserved alongside direct support.

## Store, configuration, and maintenance

Each repository receives `.codex-research/` with config, SQLite index/cache, and JSONL stores for sources, evidence, stable evidence IDs, promoted packets, telemetry, expansions, chronology, entities, searches, unresolved questions, and failures. In Git repositories with an existing `.gitignore`, `.codex-research/` is appended once.

Configuration precedence is defaults, `~/.config/repo-research/config.yaml`, explicit `--config`, then repository config. Configure include/exclude patterns, ignored directories, chunk sizes, candidates, source hierarchy, or the single `model` setting.

SHA-256 hashes make prior evidence reusable only while sources remain current. Human-facing `E####` IDs remain stable within the repository. Use `--rebuild` after parser changes. Search logs retain full candidate/inspection details locally; telemetry records approximate local and frontier-facing research-context volume. Those estimates are not exact billing or exact Codex usage.

## Benchmark and Codex skill

Copy `benchmark/synthetic-spec.example.json`, set the repository and expected passages, then run `repo-research-benchmark benchmark.json --model qwen3.5:9b-mlx`. It reports known evidence recovered, false evidence, source-location accuracy, contradictions, runtime, and peak RSS.

Invoke the user-wide skill as `$local-repo-research`, or let Codex select it for repository-scale work. Codex should inspect consequential originals and run contradiction searches before accepting important claims.

## Limitations

Lexical retrieval can miss passages with no shared terms. Model classifications remain fallible. Deterministic span validation proves text exists, not claim truth. OCR must be explicitly enabled. Legacy `.xls` is unsupported. Source hierarchy is configurable and not universal.

The SMEP economics benchmark found that compact local screening reduced a
conservative direct-frontier workload by about 80%, but authority-sensitive and
stage-sensitive claims still required selective source inspection. See
`benchmark/SMEP_ECONOMICS_2026-08-20.md` for the fixed ten-claim benchmark.
