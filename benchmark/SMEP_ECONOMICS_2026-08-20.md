# SMEP local-worker economics benchmark (2026-08-20)

This is a cost benchmark, not a full SMEP audit. It used ten fixed propositions,
`quick` depth, the 750-token `compact` packet, Qwen 3.5 9B MLX, and the existing
310-file narrow SMEP index. No larger model was used.

## Result

| Measure | Observed |
|---|---:|
| Claim-file screenings by Qwen | 126 |
| Unique candidate files screened by Qwen | 90 |
| Estimated local input tokens | 251,422 |
| Local candidate text characters | 674,588 |
| Local model output tokens | 13,766 |
| Evidence records promoted | 39 |
| Promoted/frontier packet tokens | 6,940 |
| Qwen malformed-JSON failures | 0 |
| Validation rejections | 8 |
| Codex original files/snippet sets opened | 26 unique |
| Estimated total frontier research tokens, including checks/recovery | about 33,000 |
| Propositions adjudicated | 8 of 10 |
| Propositions unresolved | 2 of 10 |
| Local job time | 2,020 seconds |
| Local inference time | 1,115 seconds |

The conservative direct-Codex comparator is the same retrieved candidate text:
674,588 characters, approximately 168,647 source tokens, across 90 unique
candidate files. Direct search could cost more because these figures exclude
frontier search/triage prompts and expansion beyond retrieved chunks.

On that comparator, the local worker avoided approximately 136,000 frontier
tokens (about 80%) and 64 file opens (about 71%). Actual frontier research
context was about 5.1 times smaller. The savings did not disappear after source
checks, but selective checking—not packet delivery—became the dominant frontier
cost: packets were only 6,940 tokens, while checks and one noisy recovery search
added roughly 26,000 tokens.

## Claim outcomes

| ID | Type | Packet-only | After checks | Important result |
|---|---|---|---|---|
| P01 | agreement date | adjudicable | supported | Original agreement confirms last signature on 8 Dec 2017. |
| P02 | initial amount/components | not sufficient | supported | Agreement confirms PHP 190m; Amendment 1 confirms a new fifth component. |
| P03 | amendment chronology/total | not sufficient | rejected | Three 2018/2019/2022 amendments total PHP 299.6m, not PHP 319.6m; the latter includes later funding. |
| P04 | signed amendment/amount | not sufficient | supported | Amendment 3 confirms the March 2022 signatures and revised contribution. |
| P05 | delivery percentage | misleading | rejected | Packet promoted annual 96%, not cumulative delivery; the financial report shows 93% expenses and about 94.9% including commitments. |
| P06 | request versus execution | not sufficient | unresolved | Qwen did not retrieve the quoted five-person/Q1 request strongly enough to adjudicate. |
| P07 | CEU operation by 2022 | not sufficient | unresolved | Retrieved material showed proposal/hiring arrangements, not a clean operational-date proof. |
| P08 | audit-only extension | conflicting | rejected/superseded | 2024 minutes support Q2-2026 audit-only, but 2025 minutes approve extension to 31 Dec 2026 with activities. |
| P09 | revised NEPF date | not sufficient | supported | Official JMC is dated 14 Apr 2025; annual report records signing/issuance. |
| P10 | SOED transition | not sufficient | supported | 2025 annual report states SOED was established under MES and adopted the CEU model. |

## Delegation boundary

Cheap and reasonably safe for Qwen: single-document factual claims, ordinary
amounts/status statements, and locating a known phrase in routine progress
reports—provided the promoted span is complete enough to read.

Route directly to Codex/source inspection: signatures and legal effect,
amendment arithmetic, proposal-versus-execution or planned-versus-operational
claims, claims whose truth changes over time, conflicts between board decisions,
and consequential no-result claims. Qwen remains useful as a locator for these,
but its packet should not be the adjudication basis.

## Smallest next change

Within the existing compact budget, promote the selected deterministic span
with one adjacent sentence (and the document classification/date) instead of
promoting isolated sentence fragments. This is smaller than more retrieval
tuning and directly targets the dominant frontier cost: reopening a source just
to recover the amount, denominator, stage, or subject omitted from a fragment.

Do not resume the full SMEP audit solely on packet-only evidence. The architecture
is economically worthwhile as a screening layer, but authority- and
stage-sensitive claims need direct routing or selective checks.
