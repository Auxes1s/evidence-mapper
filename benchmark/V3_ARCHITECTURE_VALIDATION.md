# Persistent evidence workspace validation

Release: **0.5.1**. Response schema: **3.0**. Validated on 29 October 2026.

The implementation moves research state into SQLite, schedules bounded local
extraction and comparison tasks, and delivers paginated evidence bundles by
proposition. The initial response is a coverage overview; callers retrieve full
source-linked units with `get_bundle()` or `--get-bundle`.

## Verification

| Check | Result |
|---|---|
| Automated suite | 59 tests passed |
| Static analysis | Ruff undefined-name and unused-import checks passed |
| Patch whitespace | `git diff --check` passed |
| Skill validation | Repository and installed skill validated |
| Package | 0.5.1 wheel built with the new workspace, scheduler, and bundle modules |
| Installation | Installed Python modules and skill files match workspace bytes |
| Local models | Qwen 3.5 9B MLX plus Qwen3 Embedding 0.6B |
| Installed live recovery | Failed tasks survived an Ollama restart; resume completed both tasks and returned a verified 93.5 percent passage with zero pending tasks |

The suite covers snapshot-pinned resume after source changes; stable legacy IDs;
legacy JSONL evidence surviving new exports; context across PDF pages and Word
paragraphs; table headers; conditions and denominators; decimal amounts; per-call
context reservations; constrained span IDs; failed dispositions; bounded follow-ups;
cache invalidation; whole-unit pagination; and coverage of every proposition.

## Controlled delivery comparison

A deterministic fixture was run against committed baseline
`e0a2930f052a694a08aa5f43b6ad48f713b7bf7f` and the initial v3 implementation during
this change. Both persisted three source-verified records. The old compact delivery
omitted a funding condition, while the fetched v3 bundle retained it. Generation
calls increased from two to three. Estimated delivery volume increased from 912
tokens for the baseline response to 1,365 tokens for the v3 overview plus fetched
bundle. This illustrates the intended context-preservation tradeoff; these are
mock-backend architecture measurements, not model-quality or speed benchmarks.

The updated benchmark harness was also exercised on two deterministic cases.
Both expected anchors were present in fetched context, while one of two was
selected as an exact evidence span. The harness reports these two recall measures
separately, and leaves additional unlabeled findings unjudged instead of counting
them as false evidence.

## Live acceptance and limits

The earlier three-document live fixture recovered approval, its funding condition,
a later cancellation, and annual/cumulative delivery figures. A deliberately small
four-call budget deferred comparison tasks visibly; resume completed them against
the original snapshot. The final installed smoke check separately verified decimal
span fidelity and recovery from an actual local-service restart.

A live check exposed an empty model-selected span ID. Task-specific schema enums
now restrict generation to visible chunk and span IDs, and regressions cover the
failure. Model interpretations remain candidates. Larger comparison sets disclose
when every pair has not been compared; Codex retains final judgment.

These small fixtures establish functional behavior, not general corpus recall,
throughput, or hardware-independent performance. Processing completion, search
scope, and evidence sufficiency remain separate. Full screening does not establish
perfect semantic recall.

Machine-readable run reports, delivery comparisons, package output, and source
hashes are retained locally under `build/validation/v3/`. The original installed
package and skill backup is under `build/release-backups/20261028T162731Z/`.
