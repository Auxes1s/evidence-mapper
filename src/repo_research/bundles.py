"""Whole evidence units, proposition coverage, and bounded progressive delivery."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .models import Chunk
from .promotion import estimate_tokens
from .workspace import Workspace, identity


def context_blocks(record: dict, chunks: list[Chunk]) -> list[dict]:
    """Recover enclosing paragraphs/rows from the pinned extraction, including page seams."""
    hit = next((c for c in chunks if c.chunk_id == record.get("chunk_id")), None)
    if not hit:
        return []
    peers = [
        c
        for c in chunks
        if c.source_path == hit.source_path and c.source_hash == hit.source_hash
    ]
    parent = hit.metadata.get("parent_id")
    parent_text = next(
        (
            c.metadata["parent_text"]
            for c in peers
            if c.metadata.get("parent_id") == parent and "parent_text" in c.metadata
        ),
        hit.text,
    )
    excerpt = record.get("excerpt", "")
    if parent_text.startswith("HEADERS:"):
        lines = parent_text.splitlines()
        selected = [line for line in lines[1:] if excerpt and excerpt in line]
        text = "\n".join([lines[0]] + (selected or lines[1:]))
    else:
        paragraphs = re.split(r"\n\s*\n", parent_text)
        index = next(
            (i for i, p in enumerate(paragraphs) if excerpt and excerpt in p), None
        )
        text = (
            "\n\n".join(paragraphs[max(0, index - 1) : index + 2])
            if index is not None
            else parent_text
        )
    blocks = [
        {
            "chunk_id": hit.chunk_id,
            "source_path": hit.source_path,
            "source_hash": hit.source_hash,
            "page": hit.page,
            "section": hit.section,
            "sheet": hit.sheet,
            "text": text,
        }
    ]
    if hit.page is not None:
        position = peers.index(hit)
        for offset in (-1, 1):
            j = position + offset
            if 0 <= j < len(peers) and peers[j].page != hit.page:
                neighbor = peers[j]
                blocks.append(
                    {
                        "chunk_id": neighbor.chunk_id,
                        "source_path": neighbor.source_path,
                        "source_hash": neighbor.source_hash,
                        "page": neighbor.page,
                        "text": neighbor.text,
                    }
                )
    elif hit.source_type == "docx" and hit.row_start is None:
        position = peers.index(hit)
        for offset in (-1, 1):
            j = position + offset
            if 0 <= j < len(peers) and peers[j].metadata.get("parent_id") != parent:
                neighbor = peers[j]
                blocks.append(
                    {
                        "chunk_id": neighbor.chunk_id,
                        "source_path": neighbor.source_path,
                        "source_hash": neighbor.source_hash,
                        "section": neighbor.section,
                        "text": neighbor.text,
                    }
                )
    return blocks


def save_bundles(workspace: Workspace, run_id: str, state: dict) -> None:
    chunks = workspace.chunks(state["snapshot_id"])
    for prop in state["propositions"]:
        pid = prop["id"]
        records = workspace.evidence(run_id, pid)
        units = []
        for rec in records:
            blocks = context_blocks(rec, chunks)
            units.append(
                {
                    "id": rec["id"],
                    "source_path": rec["source_path"],
                    "source_hash": rec["source_hash"],
                    "location": {
                        k: rec[k]
                        for k in (
                            "page",
                            "sheet",
                            "section",
                            "line_start",
                            "line_end",
                            "row_start",
                            "row_end",
                            "paragraph_start",
                            "paragraph_end",
                        )
                        if rec.get(k) is not None
                    },
                    "excerpt": rec["excerpt"],
                    "source_span_verified": rec.get("quote_verified", False),
                    "interpretation_status": "candidate",
                    "evidence_type": rec.get("evidence_type"),
                    "stage": rec.get("stage"),
                    "document_date": rec.get("document_date"),
                    "event_date": rec.get("event_date"),
                    "qualification": rec.get("qualification", ""),
                    "contradiction": rec.get("contradiction", ""),
                    "context_blocks": blocks,
                    "context_missing": not bool(blocks),
                }
            )
        progress = state["progress"][pid]
        pending = workspace.tasks(
            run_id, pid, ("pending", "running", "failed", "deferred")
        )
        unresolved = list(dict.fromkeys(progress.get("unresolved", [])))
        if pending:
            unresolved.append(f"{len(pending)} research tasks remain unresolved")
        bundle = {
            "bundle_id": "B" + identity(run_id, pid),
            "proposition": prop,
            "corpus_snapshot_id": state["snapshot_id"],
            "units": units,
            "relations": workspace.relations(run_id, pid),
            "unresolved": unresolved,
            "status": "evidence_found" if records else "no_evidence_in_searched_scope",
            "interpretation_status": "candidate",
            "stop_reason": progress.get("stop_reason", "pending"),
        }
        if pending:
            bundle["stop_reason"] = (
                "error"
                if any(t["status"] == "failed" for t in pending)
                else "work_pending"
            )
        if unresolved and not records:
            bundle["status"] = "unresolved"
        with workspace.db:
            workspace.db.execute(
                "INSERT OR REPLACE INTO bundles VALUES(?,?,?,?)",
                (
                    run_id,
                    bundle["bundle_id"],
                    pid,
                    json.dumps(bundle, ensure_ascii=False),
                ),
            )


def _offset(cursor: str | None, revision: str) -> int:
    if cursor is None:
        return 0
    try:
        version, index = cursor.rsplit(":", 1)
        if version != revision or int(index) < 0:
            raise ValueError
        return int(index)
    except (ValueError, AttributeError) as exc:
        raise ValueError("Invalid or stale cursor; fetch the first page again") from exc


def _finish(payload: dict, budget: int) -> dict:
    payload["budget_tokens"] = budget
    payload["estimate_note"] = "Approximate delivery tokens; not exact Codex usage."
    payload["estimated_tokens"] = 0
    for _ in range(3):
        payload["estimated_tokens"] = estimate_tokens(payload)
    if payload["estimated_tokens"] > budget:
        raise ValueError("Output budget cannot fit the response envelope")
    return payload


def overview(
    path: str | Path,
    run_id: str,
    cursor: str | None = None,
    budget_tokens: int | None = None,
) -> dict:
    workspace = Workspace(Path(path).resolve() / ".codex-research")
    try:
        state = workspace.state(run_id)
        budget = budget_tokens or state["budget_tokens"]
        revision = str(state.get("revision", 0))
        offset = _offset(cursor, revision)
        props = state["propositions"]
        if offset > len(props):
            raise ValueError("Cursor is past the overview")
        all_tasks = workspace.tasks(run_id)
        unresolved = sum(t["status"] != "complete" for t in all_tasks)
        processing_complete = not unresolved and not state.get("errors")
        full_scope = (
            processing_complete
            and state["mode"] == "exhaustive"
            and all(p.get("scope_exhausted", False) for p in state["progress"].values())
            and not state.get("extraction_omissions")
        )
        result = {
            "schema_version": "3.0",
            "search_id": run_id,
            "corpus_snapshot_id": state["snapshot_id"],
            "mode": state["mode"],
            "depth": state["depth"],
            "status": "complete_for_retrieved_scope"
            if processing_complete
            else "incomplete",
            "coverage": {
                "processing_complete": processing_complete,
                "search_scope": "all_eligible_passages_screened"
                if full_scope
                else "bounded",
                "pending_tasks": unresolved,
                "extraction_omissions": len(state.get("extraction_omissions", [])),
                "stop_reason": state.get("stop_reason", "pending"),
            },
            "proposition_count": len(props),
            "propositions": [],
            "next_cursor": None,
            "evidence": [],
            "interpretation_status": "candidate",
        }
        if state["mode"] == "inventory":
            result["inventory"] = {
                "file_families": state["inventory"]["file_families"],
                "extraction_failure_count": len(
                    state["inventory"]["extraction_failures"]
                ),
                "metadata_scan_complete": state["inventory"]["metadata_scan_complete"],
            }
        for i in range(offset, len(props)):
            prop = props[i]
            row = workspace.db.execute(
                "SELECT record_json FROM bundles WHERE run_id=? AND proposition_id=?",
                (run_id, prop["id"]),
            ).fetchone()
            bundle = json.loads(row[0]) if row else None
            entry = {
                **prop,
                "status": bundle["status"] if bundle else "unresolved",
                "bundle_id": bundle["bundle_id"] if bundle else None,
                "evidence_count": len(bundle["units"]) if bundle else 0,
                "unresolved_count": len(bundle["unresolved"]) if bundle else 1,
            }
            result["propositions"].append(entry)
            result["next_cursor"] = (
                f"{revision}:{i + 1}" if i + 1 < len(props) else None
            )
            try:
                _finish(result, budget)
            except ValueError:
                result["propositions"].pop()
                result["next_cursor"] = f"{revision}:{i}"
                if i == offset:
                    # A long proposition is fetched from its full bundle, never clipped.
                    result["propositions"].append(
                        {k: v for k, v in entry.items() if k != "text"}
                    )
                    result["next_cursor"] = (
                        f"{revision}:{i + 1}" if i + 1 < len(props) else None
                    )
                break
        payload = _finish(result, budget)
        workspace.record_delivery(run_id, "overview", payload)
        return payload
    finally:
        workspace.db.close()


def get_bundle(
    path: str | Path,
    run_id: str,
    bundle_id: str,
    cursor: str | None = None,
    budget_tokens: int | None = None,
) -> dict:
    workspace = Workspace(Path(path).resolve() / ".codex-research")
    try:
        state = workspace.state(run_id)
        row = workspace.db.execute(
            "SELECT record_json FROM bundles WHERE run_id=? AND bundle_id=?",
            (run_id, bundle_id),
        ).fetchone()
        if not row:
            raise ValueError(f"Unknown bundle {bundle_id} for run {run_id}")
        bundle = json.loads(row[0])
        budget = budget_tokens or state["config"]["bundle_budget_tokens"]
        revision = identity(bundle)
        offset = _offset(cursor, revision)
        # Relationships and gaps paginate too; no unbounded metadata outside the budget.
        items = (
            [("units", u) for u in bundle["units"]]
            + [("relations", r) for r in bundle["relations"]]
            + [("unresolved", g) for g in bundle["unresolved"]]
        )
        if offset > len(items):
            raise ValueError("Cursor is past the bundle")
        result = {
            "schema_version": "3.0",
            "search_id": run_id,
            "bundle_id": bundle_id,
            "proposition": bundle["proposition"],
            "corpus_snapshot_id": state["snapshot_id"],
            "status": bundle["status"],
            "stop_reason": bundle["stop_reason"],
            "units": [],
            "relations": [],
            "unresolved": [],
            "inspection_requirements": [],
            "next_cursor": None,
            "interpretation_status": "candidate",
            "total_units": len(bundle["units"]),
            "has_unresolved_context": bool(bundle["unresolved"]),
        }
        try:
            _finish(result, budget)
        except ValueError:
            result["proposition"] = {
                "id": bundle["proposition"]["id"],
                "text_available_in": "run_manifest",
            }
        for i in range(offset, len(items)):
            kind, item = items[i]
            result[kind].append(item)
            result["next_cursor"] = (
                f"{revision}:{i + 1}" if i + 1 < len(items) else None
            )
            try:
                _finish(result, budget)
            except ValueError:
                result[kind].pop()
                if i > offset:
                    result["next_cursor"] = f"{revision}:{i}"
                    break
                result["inspection_requirements"].append(
                    {
                        "kind": kind,
                        "evidence_id": item.get("id")
                        if isinstance(item, dict)
                        else None,
                        "required_action": "expand_evidence_context"
                        if kind == "units"
                        else "run_manifest",
                        "reason": "Complete unit exceeds delivery budget; retrieve the pinned full record",
                    }
                )
                result["next_cursor"] = (
                    f"{revision}:{i + 1}" if i + 1 < len(items) else None
                )
                break
        result["page_complete"] = (
            not result["next_cursor"] and not result["inspection_requirements"]
        )
        payload = _finish(result, budget)
        workspace.record_delivery(run_id, "get-bundle", payload)
        return payload
    finally:
        workspace.db.close()
