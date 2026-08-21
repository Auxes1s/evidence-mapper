# repo-research

**Let a small local AI search your files so your main AI agent does not have to.**

`repo-research` is a local research assistant for Codex and other AI agents. Give it a folder and a question. It searches the folder on your computer, checks useful passages against the source text, and returns a small evidence packet for the stronger agent to reason about.

```text
large folder of files
        |
        v
repo-research: local search + local model
        |
        v
small set of checked evidence
        |
        v
Codex: closer inspection and final judgment
```

The local model does the searching. The stronger model makes the final judgment.

## Why this exists

Research across a large folder can consume much of an AI agent's context before the real reasoning begins. The agent has to open files, search them, compare passages, and retain all that material just to find the few sources that matter.

`repo-research` moves most of that discovery work to local compute. It can:

- read common documents, spreadsheets, structured text, and source code;
- OCR image-only PDFs when enabled;
- build a local full-text search index;
- ask a small model running through Ollama to inspect likely sources;
- verify quoted findings against the extracted source text; and
- send only the most useful evidence to the main agent.

The original evidence remains available. Codex can retrieve a full record, add surrounding context, or open the source location when a finding needs closer inspection.

## A simple example

Suppose a folder contains 300 project documents and you want to know:

> When was the project's completion date extended, and which document authorized the extension?

Instead of asking the main agent to inspect all 300 documents, run:

```sh
repo-research --path ./project-documents \
  --mode evidence \
  --question "When was the completion date extended, and what authorized it?"
```

`repo-research` extracts and searches the files, asks the local model to examine likely passages, validates the quotations, and returns a concise report with source locations. The main agent can then focus its context on evaluating the answer rather than finding the documents.

## Why not just use RAG?

You can. This project is aimed at a narrower workflow: one-off research over a folder that may not deserve a permanent knowledge base.

You might have downloaded a collection of papers, scraped a set of pages, or received a project archive that you need to investigate once. `repo-research` creates a local index for that material and acts as a research assistant for another AI agent. It also keeps stable evidence records and validates quotations, which makes it easier to inspect how an answer was assembled.

## Privacy and requirements

Extraction, indexing, OCR, and model inference run locally by default. Repository content is sent only to Ollama at `http://127.0.0.1:11434`; the package has no external model API integration. Changing `ollama_url` or choosing a cloud-backed model changes that privacy boundary.

You need:

- Python 3.10 or newer;
- Ollama; and
- a local model.

The defaults target a 24 GB Apple Silicon Mac using `qwen3.5:9b-mlx`, a 32K context window, one generation at a time, and temperature 0.1.

## Install

Start the Ollama service and download the default model:

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
ollama pull qwen3.5:9b-mlx
```

Then install the command-line tool and bundled Codex skill:

```sh
./install.sh
repo-research --health
```

`ollama serve` starts the HTTP service used by `repo-research`. `ollama run` opens an interactive model session and is not a substitute for that service.

The installer accepts `REPO_RESEARCH_INSTALL_ROOT`, `REPO_RESEARCH_BIN_DIR`, and `REPO_RESEARCH_SKILLS_DIR` if you want to change its user-local destinations.

## Common uses

First inspect what the tool can read:

```sh
repo-research --path . --mode inventory
```

Then ask a focused question:

```sh
repo-research --path . --mode evidence \
  --question "What evidence supports X?" \
  --depth standard
```

Add `--json` for machine-readable output or `--output-budget compact` for a smaller packet. Other research modes cover distinct tasks:

```sh
# Look specifically for evidence against a claim
repo-research --path . --mode contradictions \
  --question "X happened in June 2025" --depth deep --fresh

# Reconstruct a sequence of events
repo-research --path . --mode chronology \
  --question "Reconstruct the history of X"

# Search broadly, identify gaps, or trace a claim to its original source
repo-research --path . --mode exhaustive --question "Find all evidence about X" --depth deep
repo-research --path . --mode gap-search --question "What is missing for X?" --existing-evidence @evidence.json
repo-research --path . --mode source-trace --question "Find the original source for X"
```

Search depth controls how many retrieval strategies run: `quick` uses up to two, `standard` four, and `deep` seven. `--fresh` ignores reusable results, `--challenge-existing` performs an independent challenge search, and `--rebuild` re-extracts every supported source.

## Inspecting evidence

Each saved finding receives a stable ID such as `E0001`. Use that ID to inspect what lies behind the concise report:

```sh
repo-research --path . --get-evidence E0001 --json
repo-research --path . --expand-evidence-context E0001 --radius 2 --json
repo-research --path . --open-source-location E0001 --json
repo-research --path . --show-related-evidence E0001 --json
repo-research --path . --show-contradiction-evidence E0001 --json
repo-research --path . --telemetry SEARCH_ID --json
```

The Python API exposes the same basic workflow:

```python
from repo_research import research, get_evidence, expand_evidence_context

result = research(
    path=".",
    question="Find evidence about establishment and operation.",
    mode="evidence",
    depth="deep",
)
```

## How it works

```text
question
   |
   v
extract files and preserve source locations
   |
   v
build a SQLite full-text search index
   |
   v
find likely passages for several search strategies
   |
   v
local Ollama model examines the candidates
   |
   v
quotations and locations are checked against source text
   |
   v
useful findings are ranked, stored, and returned within a budget
```

Output budgets apply only to the packet returned to the main agent: `compact` is about 750 tokens, `standard` about 1,500, and `expanded` about 4,000. Local indexing and model inference are not limited by this budget. Full validated evidence stays local; by default, no more than 12 ranked records are promoted, with important contradictions and qualifications retained alongside support.

## Supported files

Supported inputs include PDF, DOCX, XLSX/XLSM, CSV/TSV, plain text, Markdown, JSON, YAML, TOML, XML, HTML, LaTeX, BibTeX, logs, and common source-code formats.

The extractor preserves useful locations such as PDF pages, DOCX headings and paragraphs, spreadsheet sheets and rows, and source-code lines. Optional Tesseract OCR can read image-only PDFs without modifying the originals. Legacy `.xls` files are not supported.

## Local data and configuration

Each researched folder receives a `.codex-research/` directory containing its configuration, SQLite index and cache, evidence records, promoted packets, search logs, telemetry, and other local research state. In a Git repository with an existing `.gitignore`, `.codex-research/` is added once.

Configuration is applied in this order:

1. built-in defaults;
2. `~/.config/repo-research/config.yaml`;
3. a file passed with `--config`; and
4. repository-specific configuration.

You can configure the model, Ollama URL, include and exclude patterns, ignored directories, chunk sizes, candidate counts, and source hierarchy. SHA-256 hashes prevent stale source content from reusing old evidence. Search logs keep detailed candidate and inspection data locally; telemetry estimates local and frontier-facing research-context volume, not exact billing or Codex usage.

## Using it with Codex

The installer includes the `$local-repo-research` skill. Invoke it directly, or let Codex select it when a task involves many files, uncertain evidence locations, chronology, contradictions, or source tracing.

For consequential claims, the final agent should still inspect the original source and search for contradictory evidence. Quote validation proves that text exists at a location; it does not prove that the claim is true.

## Benchmarking

Copy `benchmark/synthetic-spec.example.json`, set the repository and expected passages, then run:

```sh
repo-research-benchmark benchmark.json --model qwen3.5:9b-mlx
```

The benchmark reports known evidence recovered, false evidence, source-location accuracy, contradictions, runtime, and peak memory use.

In the fixed ten-proposition SMEP economics benchmark, compact local screening reduced the conservative retrieved-candidate-text comparator by about 80%. Authority-sensitive and stage-sensitive claims still needed selective source inspection. This is one benchmark, not a general performance guarantee; see `benchmark/SMEP_ECONOMICS_2026-08-20.md` for its scope and measurements.

## Limitations

- Full-text retrieval can miss passages that share no terms with the question.
- Small-model classifications can be wrong.
- Quote validation confirms the source text, not the truth of an interpretation.
- OCR must be enabled explicitly.
- Source authority depends on the project and must be configured or judged.
