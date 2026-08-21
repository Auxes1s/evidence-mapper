# Evidence Mapper / Local Repository Research Handoff

## Finalized package (2026-08-20)

Version `0.3.0` is packaged and installed. The repository now contains the
canonical `skill/local-repo-research/` artifact and `install.sh`, which installs
the dedicated virtual environment, `repo-research` CLI link, and Codex skill.
The installed runtime and skill were validated after installation; 26 tests pass.

The requested economics benchmark stopped after ten fixed SMEP propositions.
Results and the bounded runner are in:

- `benchmark/SMEP_ECONOMICS_2026-08-20.md`
- `benchmark/smep_economics.py`

The benchmark conclusion supersedes the earlier recommendation to optimize for
maximum recall: use Qwen as a cost-saving screening layer, route authority- and
stage-sensitive claims to selective source inspection, and do not resume the full
SMEP audit on packet-only evidence.

## Current state

`evidence-mapper` is a local-first documentary research CLI backed by Ollama and
`qwen3.5:9b-mlx`. Version `0.2.0` is installed at:

- CLI: `~/.local/bin/repo-research`
- virtual environment: `~/.local/share/repo-research/venv`
- Codex skill: `~/.codex/skills/local-repo-research/`

The current source tree passes all tests:

```bash
cd /Users/marcignacio/GitHub/evidence-mapper
PYTHONPATH=src pytest -q
# 21 passed
```

This directory is not currently a Git working tree. There is no commit history
or branch containing the changes described below.

## What was implemented

### Frontier-context budgeting

Research output is separated from local model workload. Qwen may inspect and
generate much more material locally, while Codex receives a promoted evidence
packet.

Configured packet modes:

| Mode | Approximate frontier-facing budget |
|---|---:|
| `compact` | 750 tokens |
| `standard` | 1,500 tokens |
| `expanded` | 4,000 tokens |

`standard` is the default. Promotion is capped at 12 evidence records. The
ranking favors authoritative direct evidence while reserving space for adverse
evidence, so contradictions and qualifications are not crowded out by support.

Relevant implementation:

- `src/repo_research/promotion.py`
- `src/repo_research/config.py`
- `src/repo_research/research.py`

### Persistent evidence and stable IDs

All validated findings are persisted locally before promotion. Stable IDs use
the form `E0001`, `E0002`, and so on. Persistent research state lives under the
target corpus's `.codex-research/` directory:

- `evidence.jsonl`: full validated evidence records
- `evidence_ids.jsonl`: stable ID mapping
- `packets.jsonl`: promoted packets
- `searches.jsonl`: search strategies, candidates, validation rejections, and gaps
- `telemetry.jsonl`: per-job workload and compression estimates
- `expansions.jsonl`: progressive-disclosure requests
- `extraction_failures.jsonl`: files that could not be extracted
- `index.sqlite3`: content and FTS index

### Progressive disclosure

The CLI supports selective retrieval without returning the whole evidence
store:

```bash
repo-research --path CORPUS --get-evidence E0014 --json
repo-research --path CORPUS --expand-evidence-context E0014 --radius 1 --json
repo-research --path CORPUS --open-source-location E0014 --json
repo-research --path CORPUS --show-related-evidence E0014 --json
repo-research --path CORPUS --show-contradiction-evidence E0014 --json
repo-research --path CORPUS --telemetry SEARCH_ID --json
```

Expansion requests update the corresponding telemetry record. Codex skill
instructions now tell the frontier model not to reread large numbers of files
already screened locally. Direct source inspection is reserved for
consequential, conflicting, authority-sensitive, causal, counterfactual, quoted,
or important no-result claims.

Relevant implementation:

- `src/repo_research/retrieval.py`
- `src/repo_research/store.py`
- `src/repo_research/cli.py`

### Telemetry

Every completed job estimates and records:

- source files indexed;
- candidate files;
- files substantively inspected locally;
- indexed and processed text characters/tokens;
- Qwen generations and input/output tokens;
- local inference time;
- evidence generated, persisted, and promoted;
- promoted and expansion tokens;
- corpus-to-promoted and corpus-to-total-frontier compression.

All token and compression values are explicitly labeled as estimates, not API
billing or exact Codex usage.

### Resilience for cloud-backed corpora

The SMEP run exposed OneDrive failure modes. The indexer was hardened to:

- catch directory, stat, hashing, and extraction failures per file;
- persist failure paths and messages;
- commit every 25 newly indexed files so a late failure does not roll back hours
  of work;
- hash cloud files in a timeout-controlled `shasum` child process;
- support a configurable per-file deadline.

The first full-corpus attempt had built a 1.6 GB database but lost the
uncommitted transaction after a OneDrive timeout. After periodic commits were
added, 2,250 files / 196,650 chunks survived an interrupted run.

### Optional OCR fallback

Image-only PDFs can now be OCRed non-destructively with `pdftoppm` and
Tesseract. OCR text is indexed against the original source path and page, with
`section="OCR"`. Original PDFs are not modified.

Configuration:

```yaml
ocr_image_only_pdfs: true
ocr_max_pages: 120
ocr_dpi: 200
ocr_timeout_per_page_seconds: 90
```

In the SMEP corpus, OCR reduced extraction failures from 34 to 3 and increased
indexed sources from 279 to 310. The remaining failures were `OC No. 06-2022`
and two copies of a long 2024 revised ProDoc, which exceeded the corpus-specific
30-second whole-file deadline.

Relevant implementation:

- `src/repo_research/extract.py`
- `src/repo_research/index.py`

### Small-model output controls and validation

Qwen sometimes returned truncated JSON when allowed to emit too many findings.
The prompt now requests at most eight strong findings per strategy, including
adverse evidence when present, with excerpts limited to one contiguous exact
span of at most 60 words.

The validator still requires exact source text. If Qwen ignores instructions
and joins real passages with `...` or `…`, the validator may retain only the
longest constituent segment that independently matches the candidate text
exactly. It does not accept fuzzy or OCR-corrected quotations.

Relevant implementation:

- `src/repo_research/prompts.py`
- `src/repo_research/research.py`
- `tests/test_validation.py`
- `tests/test_research.py`

## SMEP benchmark status

The benchmark was intentionally stopped when the practical priority returned
to finalizing the existing SMEP report comments. It is not complete and must
not be presented as a finished comparison with GPT-5.6.

Completed benchmark work:

- Original Stocktaking Report was extracted independently into 249 non-empty
  paragraph/table records (about 72,204 characters).
- A narrowly scoped SMEP index was built from SMEP operations, directly linked
  CEU/SOED records, six-year/SYEA records, transition records, and stocktaking
  reference materials.
- Final narrow corpus after OCR: 310 indexed files, approximately 2.27 million
  extracted-text tokens, and 3 extraction failures.
- A first broad chronology job completed before OCR:
  - search ID `1b07cf35f87e427a99af446b761d5d38`
  - 7 Qwen generations
  - about 87,481 local input tokens
  - about 9,459 local output tokens
  - 34 evidence records persisted; 9 promoted
  - about 1,413 promoted tokens
  - approximately 1,529x corpus-to-promoted compression
  - about 693.5 seconds local inference
- That job mostly returned retrospective/contextual records and rejected 38
  findings during quotation/provenance validation.
- An OCR-enriched amendment/funding job completed:
  - search ID `8a75095f504440ad9c80851aaaaa19a9`
  - 4 Qwen generations
  - 17 candidate files inspected
  - about 53,811 local input tokens
  - about 5,022 local output tokens
  - 27 findings rejected because excerpts were not exact candidate substrings
  - only one finding survived promotion: OCRed Fourth Amendment language setting
    the end date to 30 June 2026 (`E0035`)

The last rerun using the improved contiguous-quotation prompt/validator was
interrupted and did not produce a completed search record. Do not count it in
benchmark metrics.

Temporary benchmark artifacts are under:

```text
/private/tmp/smep-local-benchmark/
```

The earlier overscoped local index was moved intact to:

```text
/private/tmp/smep-local-benchmark/overscoped-state-20260820/
```

Do not use the earlier GPT-5.6 audit conclusions as prompts if the independent
benchmark is resumed. Freeze local findings before opening prior-audit outputs.

## Key lessons from the SMEP run

1. **Extraction was initially the binding constraint.** Important signed PDFs
   were invisible to Qwen until OCR was enabled.
2. **Validation then became the binding constraint.** Qwen often found the
   right passage but joined non-contiguous text or repaired OCR, causing exact
   quotation rejection.
3. **The 9B model can generate useful candidates, but broad compound questions
   produce noisy, repetitive, or oversized JSON.** Focused atomic propositions
   work better.
4. **Candidate retrieval was sometimes too narrow.** The first broad chronology
   search inspected only eight files from a 279-file corpus and favored a 2024
   briefer. This is partly an FTS/query-generation problem, not solely a model
   problem.
5. **Source authority cannot be inferred reliably from filenames alone.** A
   briefer stored beneath an amendment folder received a high authority score.
   Authority scoring needs content/metadata-aware document classification.
6. **Full repository indexing is wasteful for a claim-driven audit.** The useful
   workflow is: extract report claims, inventory filenames repository-wide,
   index directly relevant families, and selectively add a primary-source
   family when a consequential claim requires it.
7. **Local-output and frontier-output budgets must remain separate.** Setting
   Qwen's generation ceiling to 700 tokens caused malformed JSON; the promoted
   packet can remain at 1,500 tokens while Qwen emits more locally.

## Known issues

### High priority

1. **No automatic JSON recovery/retry.** A truncated or malformed Qwen response
   aborts the job and loses that job's telemetry. Add one constrained retry or a
   repair pass, while preserving the original malformed output locally.
2. **Search recall is weak for compound questions.** `_terms()` and FTS query
   construction can over-constrain phrases and repeatedly return the same
   retrospective source. Add proposition-level query planning and per-source
   diversity.
3. **Authority scoring overweights path keywords.** A document inside an
   `Amendment` directory can outrank an actual signed instrument. Classify the
   document itself and deduplicate identical hashes before scoring.
4. **Exact validation loses OCR-supported evidence.** The current ellipsis
   recovery is safe but narrow. A better design would return start/end offsets
   or have the worker select an exact candidate sentence ID instead of copying
   text.
5. **Whole-file deadline also covers OCR.** A 30-second corpus setting caused
   long legitimate OCR jobs to fail. Separate cloud-read/hash timeout from OCR
   processing timeout.

### Medium priority

6. Inventory output may include implausible years from noisy text (the SMEP
   inventory reported `1901`–`2085`). Date extraction needs plausibility and
   document-context filtering.
7. Extraction warnings emitted by `pypdf`/`openpyxl` are noisy and are not
   mapped cleanly to a per-source quality flag.
8. There is no first-class metadata-only repository inventory distinct from
   content indexing. Add one so total files can be counted without extracting
   every attachment.
9. `.codex-research/index.sqlite3` can become very large on OneDrive-backed
   roots. Support a configurable local state directory while retaining absolute
   source provenance.
10. The package version remained `0.2.0` through the later timeout/OCR changes.
    Bump the version before release.

## Recommended next implementation sequence

Keep the next pass small and test-driven:

1. Add malformed-JSON persistence and one retry with a shorter prompt/output.
2. Split every research question into atomic propositions before retrieval and
   run diversified FTS queries per proposition.
3. Add source-hash deduplication and content-aware authority classification.
4. Replace copied quotations with candidate sentence/span identifiers so exact
   validation is deterministic.
5. Separate `hash_timeout_seconds` from `ocr_timeout_seconds`.
6. Add `--state-dir` for a fast local index outside OneDrive.
7. Resume the SMEP benchmark on a small fixed set of report propositions before
   attempting the full report.

Do not add a larger local model until extraction, candidate diversity, and span
validation are measured again. The current evidence suggests retrieval and
validation are more important bottlenecks than the 9B model size.

## Useful commands

```bash
# Install current source into the dedicated environment
~/.local/share/repo-research/venv/bin/pip install --upgrade \
  /Users/marcignacio/GitHub/evidence-mapper

# Validate tests
cd /Users/marcignacio/GitHub/evidence-mapper
PYTHONPATH=src pytest -q

# Health check
repo-research --health

# Narrow evidence search
repo-research --path /path/to/corpus \
  --mode evidence --depth standard --output-budget standard \
  --fresh --question "Atomic proposition to verify" --json

# Inspect a completed job
repo-research --path /path/to/corpus --telemetry SEARCH_ID --json
```

## Files changed in this development pass

- `README.md`
- `pyproject.toml`
- `src/repo_research/__init__.py`
- `src/repo_research/benchmark.py`
- `src/repo_research/cli.py`
- `src/repo_research/config.py`
- `src/repo_research/extract.py`
- `src/repo_research/index.py`
- `src/repo_research/models.py`
- `src/repo_research/ollama.py`
- `src/repo_research/promotion.py` (new)
- `src/repo_research/prompts.py`
- `src/repo_research/research.py`
- `src/repo_research/retrieval.py` (new)
- `src/repo_research/store.py`
- `tests/test_budgeting.py` (new)
- `tests/test_research.py`
- `tests/test_validation.py`

The Codex skill documentation was also updated outside this repository at:

```text
~/.codex/skills/local-repo-research/SKILL.md
~/.codex/skills/local-repo-research/references/cli-and-schema.md
```
