"""Durable, bounded research orchestration. Model calls never own research memory."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import replace
from pathlib import Path
from jsonschema import validate, ValidationError

from .budgeting import partition_by_budget, request_token_count
from .bundles import context_blocks, overview, save_bundles
from .config import ensure_repo_config, load_config
from .index import Index
from .ollama import OllamaBackend, OutputExhausted
from .prompts import (
    PROMPT_VERSION,
    RELATION_SYSTEM,
    SYSTEM,
    extraction_schema,
    relation_schema,
)
from .research import (
    MODES,
    _candidate_prompt,
    _candidate_spans,
    _evidence_from_finding,
    _inventory,
    _normalize_worker_finding,
    _snapshot_identity,
    _terms,
)
from .store import Store
from .workspace import Workspace, identity


def _propositions(question, propositions):
    if propositions is None:
        if question is not None and (
            not isinstance(question, str) or not question.strip()
        ):
            raise ValueError("question must be a nonempty string")
        return [{"id": "P001", "text": question}] if question else []
    if not isinstance(propositions, list) or not propositions:
        raise ValueError("propositions must be a nonempty list of {id,text} objects")
    result = []
    seen = set()
    for item in propositions:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(k), str) and item[k].strip() for k in ("id", "text")
        ):
            raise ValueError("Every proposition requires nonempty string id and text")
        if item["id"] in seen:
            raise ValueError("Proposition IDs must be unique")
        if len(item["id"]) > 128:
            raise ValueError("Proposition IDs must fit within 128 characters")
        seen.add(item["id"])
        result.append({"id": item["id"], "text": item["text"]})
    return result


def _model_metadata(worker, cfg):
    metadata = {
        "name": worker.model,
        "temperature": cfg["temperature"],
        "context_tokens": cfg["context_tokens"],
        "max_output_tokens": cfg["max_output_tokens"],
        "prompt_version": PROMPT_VERSION,
    }
    if hasattr(worker, "health"):
        metadata.update(worker.health())
    if cfg.get("embedding_model"):
        if not hasattr(worker, "embed"):
            raise ValueError("Hybrid retrieval requires an embedding-capable backend")
        try:
            info = (
                worker.model_info(cfg["embedding_model"])
                if hasattr(worker, "model_info")
                else {"model": cfg["embedding_model"], "digest": worker.model}
            )
        except Exception as exc:
            raise ValueError(
                f"Embedding model unavailable. Install {cfg['embedding_model']} or explicitly configure embedding_model: null: {exc}"
            ) from exc
        metadata["embedding_model_info"] = info
    return metadata


def _derived_index(root, store, ledger, state, worker):
    index = Index(
        root,
        store,
        database=store.cache / f"snapshot-{state['snapshot_id']}-v3.sqlite3",
    )
    index.populate(ledger.chunks(state["snapshot_id"]))
    cfg = state["config"]
    if cfg.get("embedding_model") and state["mode"] != "inventory":
        index.embedding_identity = identity(
            state["model"]["embedding_model_info"],
            "source-text-v3",
            cfg["embedding_query_instruction"],
        )
        index.embedding_workspace = ledger
        index.query_instruction = cfg["embedding_query_instruction"]
        index.ensure_embeddings(
            cfg["embedding_model"], worker, int(cfg["embedding_batch_size"])
        )
    return index


class Scheduler:
    def __init__(self, root, store, ledger, index, state, worker):
        self.root, self.store, self.ledger, self.index, self.state, self.worker = (
            root,
            store,
            ledger,
            index,
            state,
            worker,
        )
        self.cfg = state["config"]
        self.run = state["run_id"]
        self.chunks = ledger.chunks(state["snapshot_id"])
        self.by_id = {c.chunk_id: c for c in self.chunks}
        self.calls = 0
        self.per_prop = {p["id"]: 0 for p in state["propositions"]}
        self.started = time.monotonic()

    def checkpoint(self):
        self.ledger.save_state(self.run, self.state)

    def remaining(self, pid):
        return min(
            int(self.cfg["max_calls_per_run"]) - self.calls,
            int(self.cfg["max_calls_per_proposition"][self.state["depth"]])
            - self.per_prop[pid],
        )

    def call(self, pid, task, system, prompt, schema, chunk_ids):
        counted = request_token_count(system, prompt, schema, self.cfg)
        prompt_hash = hashlib.sha256(
            (
                system + "\x1f" + prompt + "\x1f" + json.dumps(schema, sort_keys=True)
            ).encode()
        ).hexdigest()
        task["cache_hash"] = prompt_hash
        cache = (
            self.ledger.cached_call(prompt_hash, self.state["model"])
            if not self.state["fresh"]
            else None
        )
        call_id = uuid.uuid4().hex
        row = {
            "call_id": call_id,
            "run_id": self.run,
            "strategy": task["kind"],
            "prompt_hash": prompt_hash,
            "prompt_version": PROMPT_VERSION,
            "token_method": counted.method,
            "preflight_input_tokens": counted.tokens,
            "context_tokens": self.cfg["context_tokens"],
            "output_reserved_tokens": self.cfg["max_output_tokens"],
            "safety_margin_tokens": self.cfg["context_safety_margin_tokens"],
            "chunk_ids": chunk_ids,
            "model": self.state["model"],
            "request": {
                "system": system,
                "prompt": prompt,
                "schema": schema,
                "task_id": task["task_id"],
            },
        }
        self.ledger.start_call(row)
        if cache:
            response, usage = cache
            self.ledger.finish_call(
                call_id, status="cached", usage=usage, response=response
            )
            return response, call_id
        before = len(getattr(self.worker, "usage_log", []))
        if hasattr(self.worker, "cfg"):
            self.worker.cfg["max_attempts"] = min(2, self.remaining(pid))
        error = None
        response = None
        try:
            response = self.worker.generate_json(system, prompt, schema)
            usage = (getattr(self.worker, "usage_log", [])[before:] or [{}])[-1]
            if usage.get("done_reason") in {"length", "max_tokens"}:
                raise OutputExhausted("Model exhausted its output budget")
            if int(usage.get("input_tokens") or 0) + int(
                self.cfg["max_output_tokens"]
            ) + int(self.cfg["context_safety_margin_tokens"]) > int(
                self.cfg["context_tokens"]
            ):
                raise ValueError("Backend reported a context-budget violation")
            if not isinstance(response, dict):
                raise ValueError("Model response must be an object")
            try:
                validate(response, schema)
            except ValidationError as exc:
                raise ValueError(
                    f"Model response violated schema: {exc.message}"
                ) from exc
            self.ledger.cache_call(prompt_hash, self.state["model"], response, usage)
            attempts = max(1, len(getattr(self.worker, "usage_log", [])) - before)
            return (
                response,
                call_id if attempts == 1 else f"{call_id}-retry-{attempts - 1}",
            )
        except Exception as exc:
            error = exc
            raise
        finally:
            usages = getattr(self.worker, "usage_log", [])[before:] or [{}]
            charge = max(1, len(usages))
            self.calls += charge
            self.per_prop[pid] += charge
            self.state["total_calls"] += charge
            for attempt, usage in enumerate(usages):
                aid = call_id if attempt == 0 else f"{call_id}-retry-{attempt}"
                if attempt:
                    retry_row = dict(row, call_id=aid)
                    retry_row["preflight_input_tokens"] = usage.get(
                        "preflight_input_tokens", counted.tokens
                    )
                    self.ledger.start_call(retry_row)
                if usage.get("request"):
                    with self.ledger.db:
                        self.ledger.db.execute(
                            "UPDATE model_calls SET request_json=? WHERE call_id=?",
                            (
                                json.dumps(
                                    {
                                        "payload": usage["request"],
                                        "task_id": task["task_id"],
                                    }
                                ),
                                aid,
                            ),
                        )
                self.ledger.finish_call(
                    aid,
                    status="failed"
                    if error or attempt < len(usages) - 1
                    else "complete",
                    usage=usage,
                    response=response
                    if response is not None and attempt == len(usages) - 1
                    else {"raw": usage["response_content"]}
                    if "response_content" in usage
                    else None,
                    error=str(error) if error else None,
                )
            for failure in getattr(self.worker, "malformed_log", [])[
                self.state.get("malformed_logged", 0) :
            ]:
                self.store.append(
                    "qwen_failures.jsonl",
                    {"search_id": self.run, "call_id": call_id, **failure},
                )
                with self.ledger.db:
                    self.ledger.db.execute(
                        "INSERT OR IGNORE INTO model_failures VALUES(?,?,?,?)",
                        (
                            identity(self.run, call_id, failure),
                            self.run,
                            call_id,
                            json.dumps(failure),
                        ),
                    )
            self.state["malformed_logged"] = len(
                getattr(self.worker, "malformed_log", [])
            )
            self.checkpoint()

    def enqueue_extract(
        self, pid, chunks, strategy, kind="extract", split_retry=0, span_ids=None
    ):
        def render(batch):
            return self.render(pid, batch, strategy)

        try:
            batches = partition_by_budget(
                chunks,
                render,
                system=SYSTEM,
                prefix="",
                context_tokens=int(self.cfg["context_tokens"]),
                output_tokens=int(self.cfg["max_output_tokens"]),
                safety_margin_tokens=int(self.cfg["context_safety_margin_tokens"]),
                schema=lambda batch: self.extraction_schema(pid, batch),
                config=self.cfg,
            )
        except ValueError:
            batches = [[c] for c in chunks]
        for batch in batches:
            self.ledger.enqueue(
                self.run,
                pid,
                kind,
                {
                    "chunk_ids": [c.chunk_id for c in batch],
                    "strategy": strategy,
                    "split_retry": split_retry,
                    "span_ids": span_ids or {},
                },
            )

    def extraction_schema(self, pid, chunks):
        return extraction_schema(
            {
                c.chunk_id: c.metadata.get("selected_span_ids")
                or [s["span_id"] for s in _candidate_spans(c)]
                for c in chunks
            },
            pid,
        )

    def render(self, pid, chunks, strategy):
        text = next(p["text"] for p in self.state["propositions"] if p["id"] == pid)
        mode = (
            "contradictions"
            if strategy == "contradiction and qualification"
            else self.state["mode"]
        )
        return _candidate_prompt(
            text,
            mode,
            strategy,
            chunks,
            self.state.get("existing_evidence"),
            [(pid, text)],
        )

    def retrieve(self, task):
        pid = task["proposition_id"]
        progress = self.state["progress"][pid]
        prop = next(p for p in self.state["propositions"] if p["id"] == pid)
        number = task["payload"]["round"]
        count = len(self.ledger.evidence(self.run, pid))
        if number:
            progress["empty_rounds"] = (
                progress.get("empty_rounds", 0) + 1
                if count == progress.get("last_count", 0)
                else 0
            )
        progress["last_count"] = count
        if (
            self.state["mode"] != "exhaustive"
            and number >= 2
            and progress.get("empty_rounds", 0) >= 2
        ):
            progress["stop_reason"] = "saturated"
            return
        if self.state["mode"] != "exhaustive" and number >= int(
            self.cfg["max_research_rounds"][self.state["depth"]]
        ):
            progress["stop_reason"] = "round_budget_reached"
            return
        strategies = [
            "direct terminology",
            "contradiction and qualification",
            "source tracing",
            "date and chronology",
        ]
        strategy = strategies[number % len(strategies)]
        if self.state["mode"] == "contradictions" and number == 0:
            strategy = "contradiction and qualification"
        seen = set(progress.get("seen", []))
        limit = int(self.cfg["candidate_limit"])
        if self.state["mode"] == "exhaustive":
            ranked = self.chunks
            candidates = [c for c in ranked if c.chunk_id not in seen][:limit]
        else:
            query = prop["text"]
            terms = _terms(query, strategy) + progress.get("suggested_terms", [])[:12]
            total = max(1, len(self.chunks))
            ranked = (
                self.index.hybrid_search(
                    terms, query, total, self.cfg["embedding_model"], self.worker
                )
                if self.cfg.get("embedding_model")
                else self.index.search(terms, total)
            )
            candidates = [
                c
                for c in ranked
                if strategy == "contradiction and qualification"
                or c.chunk_id not in seen
            ][:limit]
        selected = {c.chunk_id for c in candidates}
        qid = identity(pid, number, strategy)
        self.ledger.record_candidates(
            {
                "run_id": self.run,
                "strategy": strategy,
                "query_id": qid,
                "rank": rank,
                "chunk_id": c.chunk_id,
                "source_path": c.source_path,
                "score": c.metadata.get("retrieval_score"),
                "stage": "retrieved" if c.chunk_id in selected else "not_scheduled",
                "reason": None
                if c.chunk_id in selected
                else "outside this retrieval page or already screened",
                "metadata": {k: v for k, v in c.metadata.items() if k != "parent_text"},
            }
            for rank, c in enumerate(ranked, 1)
        )
        if strategy == "contradiction and qualification":
            progress["challenge_searched"] = True
        if candidates:
            self.enqueue_extract(pid, candidates, strategy)
            progress["seen"] = sorted(seen | selected)
        elif self.state["mode"] == "exhaustive":
            progress.update(scope_exhausted=True, stop_reason="scope_exhausted")
            return
        progress["round"] = number + 1
        self.ledger.enqueue(self.run, pid, "retrieve", {"round": number + 1})

    def split(self, task, chunks):
        payload = task["payload"]
        if payload.get("split_retry", 0) >= 1:
            return False
        pid = task["proposition_id"]
        if len(chunks) > 1:
            mid = len(chunks) // 2
            for batch in (chunks[:mid], chunks[mid:]):
                self.enqueue_extract(
                    pid,
                    batch,
                    payload["strategy"],
                    task["kind"],
                    1,
                    payload.get("span_ids"),
                )
        else:
            c = chunks[0]
            ids = payload.get("span_ids", {}).get(c.chunk_id) or [
                s["span_id"] for s in _candidate_spans(c)
            ]
            if len(ids) < 2:
                return False
            mid = len(ids) // 2
            for subset in (ids[:mid], ids[mid:]):
                self.ledger.enqueue(
                    self.run,
                    pid,
                    task["kind"],
                    dict(payload, split_retry=1, span_ids={c.chunk_id: subset}),
                )
        return True

    def extract(self, task):
        pid = task["proposition_id"]
        payload = task["payload"]
        progress = self.state["progress"][pid]
        chunks = [self.by_id[cid] for cid in payload["chunk_ids"]]
        rendered = [
            replace(
                c,
                metadata={
                    **c.metadata,
                    "selected_span_ids": payload["span_ids"][c.chunk_id],
                },
            )
            if c.chunk_id in payload.get("span_ids", {})
            else c
            for c in chunks
        ]
        prompt = self.render(pid, rendered, payload["strategy"])
        schema = self.extraction_schema(pid, rendered)
        counted = request_token_count(SYSTEM, prompt, schema, self.cfg)
        if (
            counted.tokens
            + self.cfg["max_output_tokens"]
            + self.cfg["context_safety_margin_tokens"]
            > self.cfg["context_tokens"]
        ):
            if self.split(task, chunks):
                return
            raise ValueError(
                "Single evidence unit exceeds context budget; original-source inspection required"
            )
        try:
            response, call_id = self.call(
                pid, task, SYSTEM, prompt, schema, [c.chunk_id for c in chunks]
            )
        except OutputExhausted:
            if self.split(task, chunks):
                return
            raise
        if not isinstance(response.get("findings"), list) or not isinstance(
            response.get("dispositions"), list
        ):
            raise ValueError("Model omitted findings or candidate dispositions")
        by_id = {c.chunk_id: c for c in chunks}
        found = {}
        invalid = set()
        old = {r["fingerprint"]: r for r in self.ledger.evidence(self.run, pid)}
        for raw in response["findings"]:
            if not isinstance(raw, dict):
                raise ValueError("Malformed finding")
            f = _normalize_worker_finding(raw)
            c = by_id.get(f.get("chunk_id"))
            if c is None:
                raise ValueError("Finding cites an unsupplied chunk")
            selected = payload.get("span_ids", {}).get(c.chunk_id)
            if selected is not None and f.get("span_id") not in selected:
                raise ValueError("Finding cites a span outside this task")
            if f.get("proposition_id") not in (None, pid):
                raise ValueError("Finding cites another proposition")
            ev = _evidence_from_finding(
                f,
                c,
                self.state["question"],
                self.worker.model,
                self.run,
                c.text,
                call_id=call_id,
                proposition_id=pid,
            )
            if ev is None:
                invalid.add(c.chunk_id)
                continue
            rec = ev.dict()
            rec["source_span_verified"] = True
            rec["interpretation_status"] = "candidate"
            rec["context_blocks"] = context_blocks(rec, self.chunks)
            rec["context"] = "\n\n".join(b["text"] for b in rec["context_blocks"])
            if rec["fingerprint"] in old:
                rec["annotations"] = old[rec["fingerprint"]].get("annotations", []) + [
                    {"call_id": call_id, "evidence_type": rec["evidence_type"]}
                ]
                # Preserve adverse classifications from independent passes.
                previous = old[rec["fingerprint"]]
                if previous.get("evidence_type") in {
                    "contradictory",
                    "qualifying",
                } and rec["evidence_type"] not in {"contradictory", "qualifying"}:
                    for key in ("evidence_type", "qualification", "contradiction"):
                        rec[key] = previous.get(key, "")
            rec = self.ledger.assign_ids([rec])[0]
            self.ledger.record_evidence(self.run, [rec])
            old[rec["fingerprint"]] = rec
            found.setdefault(c.chunk_id, []).append(rec["id"])
            kind = {"qualifying": "qualifies", "contradictory": "conflicts_with"}.get(
                rec["evidence_type"], "supports"
            )
            self.ledger.add_relation(
                self.run,
                pid,
                kind,
                [rec["id"]],
                "Worker classification; final judgment pending",
            )
        dispositions = {
            d.get("chunk_id"): d
            for d in response["dispositions"]
            if isinstance(d, dict)
        }
        if len(dispositions) != len(response["dispositions"]) or set(
            dispositions
        ) != set(by_id):
            raise ValueError("Each supplied candidate requires exactly one disposition")
        for c in chunks:
            if c.chunk_id in found:
                status = "evidence_extracted"
            elif (
                c.chunk_id not in invalid
                and dispositions.get(c.chunk_id, {}).get("status")
                == "no_relevant_evidence"
            ):
                status = "no_relevant_evidence"
            else:
                status = "unacknowledged"
            self.ledger.record_dispositions(
                [
                    {
                        "run_id": self.run,
                        "call_id": call_id,
                        "chunk_id": c.chunk_id,
                        "status": status,
                        "reason": "source span validated"
                        if c.chunk_id in found
                        else dispositions.get(c.chunk_id, {}).get(
                            "reason", "model returned no valid disposition"
                        ),
                        "evidence_ids": found.get(c.chunk_id, []),
                    }
                ]
            )
            self.ledger.record_candidates(
                [
                    {
                        "run_id": self.run,
                        "strategy": payload["strategy"],
                        "query_id": task["task_id"],
                        "rank": 1,
                        "chunk_id": c.chunk_id,
                        "source_path": c.source_path,
                        "score": None,
                        "stage": "scheduled",
                        "reason": None,
                        "metadata": {},
                    }
                ]
            )
            if status == "unacknowledged":
                raise ValueError(f"Candidate {c.chunk_id} was not acknowledged")
        suggestions = response.get("suggested_terms", [])
        progress["suggested_terms"] = list(
            dict.fromkeys(
                progress.get("suggested_terms", [])
                + [s for s in suggestions if isinstance(s, str)]
            )
        )[:24]
        for request in response.get("needs_context", []):
            self.followup(pid, request, by_id)

    def followup(self, pid, request, by_id):
        progress = self.state["progress"][pid]
        if not isinstance(request, dict) or request.get("chunk_id") not in by_id:
            raise ValueError("Context request must cite a supplied chunk")
        anchor = by_id[request["chunk_id"]]
        peers = [c for c in self.chunks if c.source_path == anchor.source_path]
        if request.get("kind") == "parent":
            targets = [
                c
                for c in peers
                if c.metadata.get("parent_id") == anchor.metadata.get("parent_id")
            ]
        elif request.get("kind") == "neighbors":
            pos = peers.index(anchor)
            targets = peers[max(0, pos - 1) : pos + 2]
        elif request.get("kind") == "reference" and isinstance(
            request.get("reference"), str
        ):
            terms = _terms(request["reference"], "source tracing")
            targets = self.index.search(terms, int(self.cfg["candidate_limit"]))
        else:
            raise ValueError("Unsupported context request")
        if not targets:
            progress.setdefault("unresolved", []).append(
                "Unresolved document reference: " + request.get("reference", "")
            )
            return
        key = identity(request)
        requests = progress.setdefault("context_requests", [])
        if key in requests:
            return
        if len(requests) >= self.cfg["max_context_followups"]:
            progress.setdefault("unresolved", []).append(
                "Context follow-up budget reached"
            )
            return
        requests.append(key)
        self.enqueue_extract(pid, targets, "source tracing", kind="context")

    def schedule_comparisons(self, pid):
        records = self.ledger.evidence(self.run, pid)
        if len(records) < 2:
            return
        # Repeat a source anchor across groups; every comparison sees exact evidence.
        anchor = records[0]
        for start in range(1, len(records), 4):
            ids = [anchor["id"]] + [r["id"] for r in records[start : start + 4]]
            self.ledger.enqueue(self.run, pid, "compare", {"evidence_ids": ids})
        if len(records) > 5:
            # Adjacent windows connect evidence on opposite sides of group boundaries.
            for start in range(2, len(records) - 1, 4):
                self.ledger.enqueue(
                    self.run,
                    pid,
                    "compare",
                    {"evidence_ids": [r["id"] for r in records[start : start + 5]]},
                )
            self.state["progress"][pid].setdefault("unresolved", []).append(
                "Comparison groups overlap, but not every evidence pair was compared; Codex must reconcile remaining relationships."
            )

    def split_comparison(self, task, selected):
        if task["payload"].get("split_retry", 0) >= 1 or len(selected) < 3:
            return False
        for left in range(len(selected)):
            for right in range(left + 1, len(selected)):
                self.ledger.enqueue(
                    self.run,
                    task["proposition_id"],
                    "compare",
                    {
                        "evidence_ids": [selected[left]["id"], selected[right]["id"]],
                        "split_retry": 1,
                    },
                )
        return True

    def compare(self, task):
        pid = task["proposition_id"]
        records = {r["id"]: r for r in self.ledger.evidence(self.run, pid)}
        selected = [records[eid] for eid in task["payload"]["evidence_ids"]]
        prompt = "RELATION_CHECK\nCompare dates, conditions, amounts, stages, and possible supersession. Cite only supplied evidence IDs. Preserve unresolved relationships.\n"
        prompt += json.dumps(
            [
                {
                    "id": r["id"],
                    "source_path": r["source_path"],
                    "excerpt": r["excerpt"],
                    "context_blocks": r.get("context_blocks", []),
                }
                for r in selected
            ],
            ensure_ascii=False,
        )
        schema = relation_schema([r["id"] for r in selected])
        count = request_token_count(RELATION_SYSTEM, prompt, schema, self.cfg)
        if (
            count.tokens
            + self.cfg["max_output_tokens"]
            + self.cfg["context_safety_margin_tokens"]
            > self.cfg["context_tokens"]
        ):
            if self.split_comparison(task, selected):
                return
            raise ValueError(
                "Related source passages exceed comparison context; Codex source inspection required"
            )
        try:
            response, _ = self.call(
                pid,
                task,
                RELATION_SYSTEM,
                prompt,
                schema,
                [r["chunk_id"] for r in selected],
            )
        except OutputExhausted:
            if self.split_comparison(task, selected):
                return
            raise
        allowed = {r["id"] for r in selected}
        if not isinstance(response.get("relations"), list) or not isinstance(
            response.get("unresolved"), list
        ):
            raise ValueError("Comparison omitted relationships or unresolved issues")
        for rel in response["relations"]:
            if not set(rel.get("evidence_ids", [])) <= allowed:
                raise ValueError("Relationship cites evidence outside comparison")
            self.ledger.add_relation(
                self.run, pid, rel["kind"], rel["evidence_ids"], rel["note"]
            )
        self.state["progress"][pid].setdefault("unresolved", []).extend(
            str(x) for x in response["unresolved"]
        )

    def run_tasks(self):
        for task in self.ledger.tasks(
            self.run, statuses=("running", "deferred", "failed")
        ):
            self.ledger.task_status(task["task_id"], "pending")
        while True:
            advanced = False
            for prop in self.state["propositions"]:
                pid = prop["id"]
                pending = self.ledger.tasks(self.run, pid, ("pending",))
                if not pending:
                    evidence_revision = identity(
                        [
                            (r["id"], r.get("evidence_type"))
                            for r in self.ledger.evidence(self.run, pid)
                        ]
                    )
                    if (
                        self.state["progress"][pid].get("comparisons_revision")
                        != evidence_revision
                    ):
                        self.schedule_comparisons(pid)
                        self.state["progress"][pid]["comparisons_revision"] = (
                            evidence_revision
                        )
                        pending = self.ledger.tasks(self.run, pid, ("pending",))
                    if not pending:
                        continue
                task = pending[0]
                if task["kind"] != "retrieve" and self.remaining(pid) <= 0:
                    self.state["progress"][pid]["stop_reason"] = "call_budget_reached"
                    continue
                advanced = True
                self.ledger.task_status(task["task_id"], "running")
                try:
                    getattr(
                        self,
                        {
                            "retrieve": "retrieve",
                            "extract": "extract",
                            "context": "extract",
                            "compare": "compare",
                        }[task["kind"]],
                    )(task)
                    self.ledger.task_status(task["task_id"], "complete")
                except Exception as exc:
                    if task.get("cache_hash"):
                        self.ledger.discard_call_cache(
                            task["cache_hash"], self.state["model"]
                        )
                    self.ledger.task_status(task["task_id"], "failed", str(exc))
                    for cid in task["payload"].get("chunk_ids", []):
                        self.ledger.record_dispositions(
                            [
                                {
                                    "run_id": self.run,
                                    "call_id": "task-" + task["task_id"],
                                    "chunk_id": cid,
                                    "status": "failed",
                                    "reason": str(exc),
                                    "evidence_ids": [],
                                }
                            ]
                        )
                self.checkpoint()
            if not advanced:
                break
        for task in self.ledger.tasks(self.run, statuses=("pending",)):
            self.ledger.task_status(
                task["task_id"], "deferred", "generation call budget reached"
            )
        pending = self.ledger.tasks(
            self.run, statuses=("failed", "deferred", "running")
        )
        self.state["stop_reason"] = (
            "budget_reached"
            if any(t["status"] == "deferred" for t in pending)
            else (
                "error"
                if pending
                else (
                    "scope_exhausted"
                    if self.state["mode"] == "exhaustive"
                    else "round_budget_reached"
                    if any(
                        p.get("stop_reason") == "round_budget_reached"
                        for p in self.state["progress"].values()
                    )
                    else "saturated"
                )
            )
        )
        self.state["revision"] += 1
        self.checkpoint()


def run_research(
    path,
    question=None,
    mode="inventory",
    depth="standard",
    *,
    fresh=False,
    challenge_existing=False,
    existing_evidence=None,
    config_path=None,
    backend=None,
    rebuild=False,
    output_budget=None,
    propositions=None,
    resume=None,
):
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Not a directory: {root}")
    if mode not in MODES:
        raise ValueError(f"Unknown mode: {mode}")
    if depth not in {"quick", "standard", "deep"}:
        raise ValueError(f"Unknown depth: {depth}")
    store = Store(root)
    ledger = Workspace(store.state)
    index = None
    try:
        if resume:
            if (
                question
                or propositions is not None
                or rebuild
                or fresh
                or challenge_existing
            ):
                raise ValueError(
                    "Resume uses the saved question, propositions, snapshot, and research settings"
                )
            state = ledger.state(resume)
            cfg = state["config"]
            state["malformed_logged"] = 0
            worker = backend or OllamaBackend(cfg)
            if (
                state["mode"] != "inventory"
                and _model_metadata(worker, cfg) != state["model"]
            ):
                raise ValueError(
                    "Model identity changed; start a new run instead of resuming this snapshot"
                )
        else:
            pairs = _propositions(question, propositions)
            if mode != "inventory" and not pairs:
                raise ValueError("--question or --propositions is required")
            question = question or "; ".join(p["text"] for p in pairs)
            cfg = load_config(root, config_path)
            for name in (
                "candidate_limit",
                "max_calls_per_run",
                "bundle_budget_tokens",
                "context_tokens",
                "max_output_tokens",
            ):
                if not isinstance(cfg[name], int) or cfg[name] <= 0:
                    raise ValueError(f"{name} must be a positive integer")
            for name in ("max_research_rounds", "max_calls_per_proposition"):
                if int(cfg[name][depth]) <= 0:
                    raise ValueError(f"{name}.{depth} must be positive")
            budget_name = output_budget or cfg["frontier_output_budget"]
            if budget_name not in cfg["frontier_output_budgets"]:
                raise ValueError(f"Unknown output budget: {budget_name}")
            ensure_repo_config(root, cfg)
            worker = backend or (OllamaBackend(cfg) if mode != "inventory" else None)
            metadata = _model_metadata(worker, cfg) if worker else {}
            live = Index(root, store)
            try:
                stats = live.build(cfg, force=rebuild)
                chunks = live.all_chunks()
                snapshot, manifest_hash, policy = _snapshot_identity(
                    root, cfg, stats["file_manifest"]
                )
                inventory = (
                    _inventory(live, store, stats) if mode == "inventory" else None
                )
            finally:
                live.db.close()
            run_id = uuid.uuid4().hex
            ledger.start_run(
                run_id,
                question=question,
                mode=mode,
                depth=depth,
                config=cfg,
                prompt_version=PROMPT_VERSION,
                model=metadata,
            )
            ledger.record_snapshot(
                run_id, snapshot, root, policy, manifest_hash, stats["file_manifest"]
            )
            ledger.save_chunks(snapshot, chunks)
            omissions = [
                x
                for x in stats["file_manifest"]
                if x["status"] in {"failed", "oversized"}
                or (x["status"] == "indexed" and x.get("reason"))
            ]
            state = {
                "run_id": run_id,
                "question": question,
                "propositions": pairs,
                "mode": mode,
                "depth": depth,
                "config": cfg,
                "snapshot_id": snapshot,
                "model": metadata,
                "fresh": fresh or challenge_existing,
                "existing_evidence": existing_evidence,
                "budget_name": budget_name,
                "budget_tokens": int(cfg["frontier_output_budgets"][budget_name]),
                "inventory": inventory,
                "extraction_omissions": omissions,
                "revision": 0,
                "total_calls": 0,
                "errors": [],
                "progress": {
                    p["id"]: {"round": 0, "seen": [], "unresolved": []} for p in pairs
                },
            }
            ledger.save_state(run_id, state)
            for p in pairs:
                ledger.enqueue(run_id, p["id"], "retrieve", {"round": 0})
        index = _derived_index(root, store, ledger, state, worker)
        scheduler = Scheduler(root, store, ledger, index, state, worker)
        if state["mode"] != "inventory":
            scheduler.run_tasks()
        else:
            state["stop_reason"] = "inventory"
            ledger.save_state(state["run_id"], state)
        run_id = state["run_id"]
        save_bundles(ledger, run_id, state)
        reasons = [
            t["error"] or t["status"]
            for t in ledger.tasks(run_id, statuses=("failed", "deferred", "running"))
        ]
        reasons.extend(
            "Extraction omission: " + x["source_path"]
            for x in state["extraction_omissions"]
        )
        state["errors"] = reasons
        ledger.save_state(run_id, state)
        ledger.finish_run(
            run_id, "incomplete" if reasons else "complete_for_retrieved_scope", reasons
        )
        # Rebuild exports from authoritative records; never read them to resume tasks.
        all_records = [
            json.loads(r[0])
            for r in ledger.db.execute(
                "SELECT record_json FROM evidence_records ORDER BY run_id,evidence_id"
            )
        ]
        store.replace("evidence.jsonl", all_records)
        store.replace(
            "evidence_ids.jsonl",
            [
                {"fingerprint": r[0], "evidence_id": r[1]}
                for r in ledger.db.execute("SELECT * FROM evidence_ids")
            ],
        )
        records = ledger.evidence(run_id)
        candidate_paths = {
            r[0]
            for r in ledger.db.execute(
                "SELECT DISTINCT source_path FROM retrieval_candidates WHERE run_id=? AND stage='retrieved'",
                (run_id,),
            )
        }
        hashes = sorted(
            {
                c.source_hash
                for c in scheduler.chunks
                if c.source_path in candidate_paths
            }
        )
        public = overview(root, run_id)
        store.append(
            "telemetry.jsonl",
            {
                "search_id": run_id,
                "source_files_indexed": len({c.source_path for c in scheduler.chunks}),
                "qwen_generations": scheduler.calls,
                "total_generation_calls": state["total_calls"],
                "estimated_local_input_tokens_processed": sum(
                    r[0] or 0
                    for r in ledger.db.execute(
                        "SELECT reported_input_tokens FROM model_calls WHERE run_id=? AND status!='cached'",
                        (run_id,),
                    )
                ),
                "evidence_records_persisted": len(records),
                "evidence_records_promoted": 0,
                "qwen_failures": len(getattr(worker, "malformed_log", [])),
                "candidate_files": len(candidate_paths),
                "candidate_source_hashes": len(hashes),
                "candidate_source_hash_list": hashes,
                "estimated_promoted_tokens": 0,
                "overview_estimated_tokens": public["estimated_tokens"],
                "additional_source_expansions": 0,
                "estimated_expansion_tokens": 0,
                "elapsed_job_seconds": round(time.monotonic() - scheduler.started, 3),
            },
        )
        return public
    finally:
        if index is not None:
            index.db.close()
        ledger.db.close()
