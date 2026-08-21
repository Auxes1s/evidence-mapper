from __future__ import annotations

import json
import re
from typing import Any

CONTENT_AUTHORITY_PATTERNS = [
    (100, r"\b(signed|certified|republic act|law|jmc|joint memorandum|agreement|amendment|resolution)\b"),
    (90, r"\b(minutes|project board|official highlights)\b"),
    (78, r"\b(annual|progress|financial|audit|approved report)\b"),
    (72, r"\b(memorandum|memo|letter|correspondence|special order)\b"),
    (62, r"\b(contract|acceptance|work plan|workplan|operational|transition)\b"),
    (42, r"\b(presentation|deck|slides|briefer|transcript)\b"),
    (30, r"\b(summary|retrospective|consultant)\b"),
]

def estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return max(1, (len(text) + 3) // 4)

def authority_score(record: dict[str, Any]) -> int:
    # Classify the document represented by its selected text and nearby context.
    # A path is only weak corroboration: folders frequently contain derivative copies.
    hay = f"{record.get('excerpt','')} {record.get('context','')} {record.get('worker_note','')}".lower()
    score = 20
    for value, pattern in CONTENT_AUTHORITY_PATTERNS:
        if re.search(pattern, hay, re.I):
            score = max(score, value)
    path = record.get("source_path", "").lower()
    if re.search(r"\b(signed|certified|resolution)\b", path): score += 4
    if re.search(r"\b(briefer|presentation|summary)\b", path): score -= 4
    et = record.get("evidence_type")
    score += {"direct": 18, "contradictory": 17, "qualifying": 15,
              "indirect": 8, "contextual": 4, "retrospective": -8}.get(et, 0)
    if record.get("quote_verified"): score += 8
    if record.get("document_date") or record.get("event_date"): score += 3
    return score

def _locator(record: dict[str, Any]) -> dict[str, Any]:
    keys = ("page", "sheet", "section", "line_start", "line_end", "row_start", "row_end",
            "paragraph_start", "paragraph_end")
    return {k: record[k] for k in keys if record.get(k) is not None}

def _short(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[:max(1, limit-1)].rstrip() + "…"

def compact_record(record: dict[str, Any], detail: str = "standard") -> dict[str, Any]:
    limits = {"compact": (180, 90), "standard": (320, 140), "expanded": (900, 320)}
    excerpt_limit, note_limit = limits[detail]
    locator = _locator(record)
    out = {
        "id": record["id"],
        "evidence_type": record.get("evidence_type"),
        "stage": record.get("stage"),
        "source_path": record.get("source_path"),
        "source_type": record.get("source_type"),
        "source_hash": record.get("source_hash"),
        "location": locator,
        "excerpt": _short(record.get("excerpt", ""), excerpt_limit),
        "note": _short(record.get("worker_note", ""), note_limit),
        "qualification": _short(record.get("qualification", ""), note_limit),
        "contradiction": _short(record.get("contradiction", ""), note_limit),
        "quote_verified": bool(record.get("quote_verified")),
        "authority_score": authority_score(record),
    }
    out.update(locator)  # preserve the established flat provenance fields too
    return out

def _ordered_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = sorted(records, key=lambda r: (-authority_score(r), r.get("id", "")))
    contradictions = [r for r in ranked if r.get("evidence_type") == "contradictory" or r.get("contradiction")]
    qualifications = [r for r in ranked if r.get("evidence_type") == "qualifying" or r.get("qualification")]
    direct = [r for r in ranked if r.get("evidence_type") == "direct"]
    rest = [r for r in ranked if r not in contradictions and r not in qualifications and r not in direct]
    # Preserve adverse evidence even under compact budgets, then fill with strongest direct support.
    out = []
    for group in (contradictions[:1], qualifications[:1], direct, contradictions[1:], qualifications[1:], rest):
        for record in group:
            if record not in out: out.append(record)
    return out

def promote(records: list[dict[str, Any]], *, budget_name: str, budget_tokens: int,
            max_records: int, coverage: dict[str, Any], gaps: list[str]) -> dict[str, Any]:
    ordered = _ordered_records(records)
    packet = {
        "budget": budget_name,
        "budget_tokens": budget_tokens,
        "coverage": coverage,
        "best_support": [],
        "best_qualification": [],
        "best_contradiction": [],
        "other_promoted": [],
        "unresolved_gaps": gaps[:8],
    }
    base_tokens = estimate_tokens(packet)
    used = base_tokens
    promoted = []
    for record in ordered:
        if len(promoted) >= max_records: break
        compact = compact_record(record, budget_name)
        cost = estimate_tokens(compact)
        if promoted and used + cost > budget_tokens: continue
        # Always allow one record, but shrink it if the packet is extremely constrained.
        if not promoted and used + cost > budget_tokens:
            compact = compact_record(record, "compact")
            cost = estimate_tokens(compact)
        if used + cost > budget_tokens: break
        promoted.append(compact); used += cost
    for rec in promoted:
        if rec["evidence_type"] == "contradictory" or rec.get("contradiction"):
            packet["best_contradiction"].append(rec)
        elif rec["evidence_type"] == "qualifying" or rec.get("qualification"):
            packet["best_qualification"].append(rec)
        elif rec["evidence_type"] == "direct": packet["best_support"].append(rec)
        else: packet["other_promoted"].append(rec)
    packet["promoted_ids"] = [r["id"] for r in promoted]
    packet["estimated_tokens"] = estimate_tokens(packet)
    # Defensive hard stop: remove lowest-priority tail until serialization respects the budget.
    while packet["estimated_tokens"] > budget_tokens and packet["promoted_ids"]:
        rid = packet["promoted_ids"].pop()
        for key in ("best_support", "best_qualification", "best_contradiction", "other_promoted"):
            packet[key] = [r for r in packet[key] if r["id"] != rid]
        packet["estimated_tokens"] = estimate_tokens(packet)
    return packet
