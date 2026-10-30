from __future__ import annotations

import json
import sqlite3

import pytest
import yaml

from repo_research import (
    research,
    get_bundle,
    overview,
    run_manifest,
    expand_evidence_context,
)
from repo_research.budgeting import request_token_count
from repo_research.config import DEFAULTS
from repo_research.ollama import MockBackend, OllamaBackend
from repo_research.workspace import Workspace


def config(root, **values):
    state = root / ".codex-research"
    state.mkdir(exist_ok=True)
    (state / "config.yaml").write_text(yaml.safe_dump(values))


def all_units(root, result):
    units = []
    page = result
    while True:
        for prop in page["propositions"]:
            cursor = None
            while True:
                bundle = get_bundle(
                    root, result["search_id"], prop["bundle_id"], cursor
                )
                units.extend(bundle["units"])
                cursor = bundle["next_cursor"]
                if cursor is None:
                    break
        if page["next_cursor"] is None:
            break
        page = overview(root, result["search_id"], page["next_cursor"])
    return units


def test_condition_and_denominator_survive_first_span_selection(tmp_path):
    class FirstSpan(MockBackend):
        def generate_json(self, *args, **kwargs):
            out = super().generate_json(*args, **kwargs)
            if "findings" in out:
                out["findings"] = out["findings"][:1]
            return out

    text = (
        "Atlas approved the extension. "
        + "The implementation schedule remains attached. " * 8
    )
    text += "Approval takes effect only after funding is secured. Annual delivery is 96%; cumulative delivery is 93%."
    (tmp_path / "approval.txt").write_text(text)
    result = research(tmp_path, "Atlas extension", mode="evidence", backend=FirstSpan())
    units = all_units(tmp_path, result)
    assert units and all(u["source_span_verified"] for u in units)
    context = " ".join(b["text"] for u in units for b in u["context_blocks"])
    assert "only after funding" in context
    assert "Annual delivery is 96%; cumulative delivery is 93%" in context


def test_cross_document_reconciliation_uses_original_evidence(tmp_path):
    class Comparing(MockBackend):
        def __init__(self):
            super().__init__()
            self.comparisons = []

        def generate_json(self, system, prompt, schema=None):
            out = super().generate_json(system, prompt, schema)
            if "RELATION_CHECK" in prompt:
                self.comparisons.append(prompt)
                data = json.loads(prompt.split("\n", 2)[2])
                out["relations"] = [
                    {
                        "kind": "possible_supersession",
                        "evidence_ids": [r["id"] for r in data],
                        "note": "Compare the original decisions.",
                    }
                ]
            return out

    documents = {
        "approval.txt": "Atlas approval was issued on 1 June 2025.",
        "condition.txt": "Atlas approval requires counterpart funding.",
        "cancellation.txt": "Atlas approval was cancelled on 2 July 2025.",
    }
    for name, text in documents.items():
        (tmp_path / name).write_text(text)
    config(tmp_path, candidate_limit=1)
    worker = Comparing()
    out = research(
        tmp_path, "Atlas approval", mode="exhaustive", depth="deep", backend=worker
    )
    manifest = run_manifest(tmp_path, out["search_id"])
    assert {u["source_path"] for u in all_units(tmp_path, out)} == set(documents)
    assert all(text in " ".join(worker.comparisons) for text in documents.values())
    assert any(
        r["kind"] == "possible_supersession" for r in manifest["evidence_relations"]
    )
    assert all(
        r["interpretation_status"] == "candidate"
        for r in manifest["evidence_relations"]
    )


def test_overview_paginates_every_proposition_and_entire_response(tmp_path):
    props = [
        {"id": f"P{i}", "text": f"What happened to the distinct project number {i}?"}
        for i in range(30)
    ]
    out = research(tmp_path, mode="evidence", propositions=props, backend=MockBackend())
    seen = []
    page = out
    while True:
        assert page["estimated_tokens"] <= 750
        assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":"))) <= 3000
        seen.extend(p["id"] for p in page["propositions"])
        if page["next_cursor"] is None:
            break
        page = overview(tmp_path, out["search_id"], page["next_cursor"])
    assert seen == [p["id"] for p in props]


def test_oversized_unit_requires_explicit_inspection_and_preserves_snapshot(tmp_path):
    original = (
        "Atlas approval. "
        + "The attached implementation details remain applicable. " * 500
        + "Only after funding is secured."
    )
    source = tmp_path / "long.txt"
    source.write_text(original)
    config(tmp_path, max_calls_per_run=2)
    out = research(tmp_path, "Atlas approval", mode="evidence", backend=MockBackend())
    bundle = get_bundle(tmp_path, out["search_id"], out["propositions"][0]["bundle_id"])
    assert bundle["inspection_requirements"] and not bundle["units"]
    eid = bundle["inspection_requirements"][0]["evidence_id"]
    source.write_text("This file now contains something else.")
    expanded = expand_evidence_context(tmp_path, eid, run_id=out["search_id"])
    assert "Only after funding is secured" in expanded["context"]
    assert original == expanded["context"]


def test_resume_uses_original_snapshot_and_preserves_ids(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text(
        "Atlas approval occurred in June. Atlas funding was subsequently cancelled."
    )
    (tmp_path / "z-condition.txt").write_text(
        "Atlas approval requires counterpart funding."
    )
    config(tmp_path, max_calls_per_run=1, candidate_limit=1)
    out = research(tmp_path, "Atlas approval", mode="exhaustive", backend=MockBackend())
    first = run_manifest(tmp_path, out["search_id"])
    ids = {e["id"] for e in first["evidence_records"]}
    assert out["coverage"]["pending_tasks"] > 0
    source.write_text("Revised text must never enter the old research snapshot.")
    for _ in range(10):
        out = research(tmp_path, resume=out["search_id"], backend=MockBackend())
        if not out["coverage"]["pending_tasks"]:
            break
    manifest = run_manifest(tmp_path, out["search_id"])
    assert out["status"] == "complete_for_retrieved_scope"
    assert out["corpus_snapshot_id"] == first["run"]["corpus_snapshot_id"]
    assert ids <= {e["id"] for e in manifest["evidence_records"]}
    assert "Revised text" not in json.dumps(manifest["evidence_records"])
    assert all(t["status"] == "complete" for t in manifest["tasks"])


def test_exhaustive_screens_unmatched_passages_and_failure_blocks_full_scope(tmp_path):
    for i in range(4):
        (tmp_path / f"file{i}.txt").write_text(f"Unrelated information {i}.")
    config(tmp_path, embedding_model=None, candidate_limit=1)
    out = research(
        tmp_path, "No matching phrase", mode="exhaustive", backend=MockBackend()
    )
    manifest = run_manifest(tmp_path, out["search_id"])
    assert len({d["chunk_id"] for d in manifest["candidate_dispositions"]}) == 4
    assert out["coverage"]["search_scope"] == "all_eligible_passages_screened"

    class Broken(MockBackend):
        def generate_json(self, *args, **kwargs):
            return {"findings": [], "dispositions": []}

    failed = research(
        tmp_path, "No matching phrase", mode="exhaustive", fresh=True, backend=Broken()
    )
    assert failed["coverage"]["search_scope"] == "bounded"
    assert not failed["coverage"]["processing_complete"]


def test_legacy_id_migration_is_backed_up_and_transactional(tmp_path):
    state = tmp_path / ".codex-research"
    state.mkdir()
    with sqlite3.connect(state / "audit.sqlite3") as db:
        db.execute("CREATE TABLE legacy(value TEXT)")
        db.execute("INSERT INTO legacy VALUES('retained')")
    (state / "evidence_ids.jsonl").write_text(
        json.dumps({"fingerprint": "old", "evidence_id": "E0042"}) + "\n"
    )
    ledger = Workspace(state)
    assert ledger.assign_ids([{"id": "new"}])[0]["id"] == "E0043"
    assert ledger.assign_ids([{"id": "old"}])[0]["id"] == "E0042"
    ledger.db.close()
    assert (state / "audit.before-v3.sqlite3").exists()
    with sqlite3.connect(state / "audit.before-v3.sqlite3") as db:
        assert db.execute("SELECT value FROM legacy").fetchone()[0] == "retained"


def test_embedding_cache_is_invalidated_by_model_digest(tmp_path):
    class Versioned(MockBackend):
        def __init__(self, digest):
            super().__init__()
            self.digest = digest
            self.embedded = []

        def model_info(self, model):
            return {"model": model, "digest": self.digest}

        def embed(self, texts, model=None):
            self.embedded.extend(texts)
            return super().embed(texts, model)

    (tmp_path / "a.txt").write_text("Atlas approval happened in June.")
    a = Versioned("one")
    out = research(tmp_path, "Atlas approval", mode="evidence", backend=a)
    b = Versioned("one")
    research(tmp_path, "Atlas approval", mode="evidence", backend=b)
    c = Versioned("two")
    research(tmp_path, "Atlas approval", mode="evidence", backend=c)
    assert "Atlas approval happened in June." in a.embedded
    assert not b.embedded
    assert "Atlas approval happened in June." in c.embedded
    with pytest.raises(ValueError, match="Model identity changed"):
        research(tmp_path, resume=out["search_id"], backend=Versioned("two"))


def test_malformed_retry_is_repreflighted_without_growing_context(monkeypatch):
    requests = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "message": {"content": "{" if len(requests) == 1 else '{"ok":true}'},
                "prompt_eval_count": 30,
                "eval_count": 5,
                "done_reason": "stop",
            }

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, json):
            requests.append(json.copy())
            return Response()

    monkeypatch.setattr("repo_research.ollama.httpx.Client", Client)
    worker = OllamaBackend(dict(DEFAULTS))
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    assert worker.generate_json("System", "Bounded task", schema) == {"ok": True}
    assert len(requests) == 2
    assert requests[0]["messages"] == requests[1]["messages"]
    assert all(len(r["messages"]) == 2 for r in requests)
    expected = request_token_count("System", "Bounded task", schema, DEFAULTS).tokens
    assert all(u["preflight_input_tokens"] == expected for u in worker.usage_log)


def test_failed_span_validation_does_not_poison_resume_cache(tmp_path):
    class InvalidSpan(MockBackend):
        def generate_json(self, *args, **kwargs):
            out = super().generate_json(*args, **kwargs)
            for finding in out.get("findings", []):
                finding["span_id"] = "S999999"
            return out

    (tmp_path / "a.txt").write_text("Atlas approval was issued in June.")
    failed = research(
        tmp_path, "Atlas approval", mode="evidence", backend=InvalidSpan()
    )
    assert failed["status"] == "incomplete"
    worker = MockBackend()
    resumed = research(tmp_path, resume=failed["search_id"], backend=worker)
    assert resumed["status"] == "complete_for_retrieved_scope"
    assert worker.usage_log
    assert all_units(tmp_path, resumed)


def test_context_requests_are_durable_and_bounded(tmp_path):
    class Asking(MockBackend):
        def generate_json(self, *args, **kwargs):
            out = super().generate_json(*args, **kwargs)
            if out.get("dispositions"):
                out["needs_context"] = [
                    {
                        "chunk_id": out["dispositions"][0]["chunk_id"],
                        "kind": "parent",
                        "reason": "Check approval conditions.",
                    }
                ]
            return out

    (tmp_path / "a.txt").write_text(
        "Atlas approval was issued. It remains subject to funding."
    )
    out = research(tmp_path, "Atlas approval", mode="evidence", backend=Asking())
    tasks = run_manifest(tmp_path, out["search_id"])["tasks"]
    context = [t for t in tasks if t["kind"] == "context"]
    assert len(context) == 1 and context[0]["status"] == "complete"


def test_pdf_context_crosses_page_boundary(tmp_path):
    from reportlab.pdfgen.canvas import Canvas

    pdf = Canvas(str(tmp_path / "approval.pdf"))
    pdf.drawString(
        72, 720, "Atlas approval is granted subject to the following condition:"
    )
    pdf.showPage()
    pdf.drawString(
        72, 720, "Counterpart funding must be secured before implementation."
    )
    pdf.save()
    out = research(tmp_path, "Atlas approval", mode="exhaustive", backend=MockBackend())
    first = next(u for u in all_units(tmp_path, out) if u["location"].get("page") == 1)
    assert {b["page"] for b in first["context_blocks"]} == {1, 2}
    assert "Counterpart funding" in " ".join(b["text"] for b in first["context_blocks"])


def test_lexical_exclusions_do_not_block_eligible_exhaustive_scope(tmp_path):
    (tmp_path / "a.txt").write_text("Atlas approval.")
    (tmp_path / "image.bin").write_bytes(b"unsupported")
    (tmp_path / "excluded.txt").write_text("Excluded by policy.")
    config(tmp_path, embedding_model=None, exclude=["excluded.txt"])
    out = research(tmp_path, "Atlas approval", mode="exhaustive", backend=MockBackend())
    assert out["coverage"]["search_scope"] == "all_eligible_passages_screened"
    assert {u["source_path"] for u in all_units(tmp_path, out)} == {"a.txt"}


def test_long_proposition_is_fetchable_without_clipping(tmp_path):
    text = "Explain the historical research question in full. " * 500
    out = research(
        tmp_path,
        mode="evidence",
        propositions=[{"id": "P01", "text": text}],
        backend=MockBackend(),
    )
    assert out["estimated_tokens"] <= 750
    assert "text" not in out["propositions"][0]
    bundle = get_bundle(tmp_path, out["search_id"], out["propositions"][0]["bundle_id"])
    assert bundle["proposition"]["text_available_in"] == "run_manifest"
    assert (
        run_manifest(tmp_path, out["search_id"])["research_state"]["propositions"][0][
            "text"
        ]
        == text
    )


def test_cli_fetch_requires_run_and_handles_explicit_propositions(
    tmp_path, capsys, monkeypatch
):
    from repo_research.cli import main
    from repo_research import cli

    (tmp_path / "a.txt").write_text("Atlas approval.")
    actual = cli.research
    monkeypatch.setattr(
        cli, "research", lambda *a, **kw: actual(*a, **kw, backend=MockBackend())
    )
    assert main(["--path", str(tmp_path), "--get-bundle", "Bunknown"]) == 2
    assert "requires --run-id" in capsys.readouterr().err
    assert (
        main(
            [
                "--path",
                str(tmp_path),
                "--mode",
                "evidence",
                "--propositions",
                '[{"id":"A","text":"Atlas approval"}]',
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert (
        main(
            [
                "--path",
                str(tmp_path),
                "--get-bundle",
                result["propositions"][0]["bundle_id"],
                "--run-id",
                result["search_id"],
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["units"]


def test_worker_schema_rejects_empty_span_and_other_candidates(tmp_path):
    from jsonschema import validate, ValidationError

    class Inspecting(MockBackend):
        def generate_json(self, system, prompt, schema=None):
            out = super().generate_json(system, prompt, schema)
            if out.get("findings"):
                invalid = json.loads(json.dumps(out))
                invalid["findings"][0]["span_id"] = ""
                with pytest.raises(ValidationError):
                    validate(invalid, schema)
                invalid["findings"][0]["span_id"] = "S000"
                invalid["findings"][0]["chunk_id"] = "not-supplied"
                with pytest.raises(ValidationError):
                    validate(invalid, schema)
            return out

    (tmp_path / "a.txt").write_text("Atlas cumulative delivery is 93 percent.")
    result = research(tmp_path, "Atlas delivery", mode="evidence", backend=Inspecting())
    assert result["status"] == "complete_for_retrieved_scope"


def test_docx_condition_in_next_paragraph_is_delivered(tmp_path):
    from docx import Document

    doc = Document()
    doc.add_heading("Extension authority", 1)
    doc.add_paragraph("The Atlas extension is approved.")
    doc.add_paragraph("This approval applies only after funding is secured.")
    doc.save(tmp_path / "approval.docx")
    result = research(
        tmp_path, "Atlas extension approval", mode="evidence", backend=MockBackend()
    )
    approval = next(
        u
        for u in all_units(tmp_path, result)
        if "extension is approved" in u["excerpt"]
    )
    assert "only after funding" in " ".join(
        b["text"] for b in approval["context_blocks"]
    )


def test_chunk_policy_change_does_not_reuse_wrong_embedding(tmp_path):
    from repo_research.config import load_config
    from repo_research.extract import extract_file

    source = tmp_path / "a.txt"
    source.write_text("Atlas evidence. " * 100)
    cfg = load_config(tmp_path)
    cfg.update(chunk_chars=100, chunk_overlap_chars=0)
    first, _ = extract_file(source, tmp_path, cfg)
    cfg["chunk_chars"] = 200
    second, _ = extract_file(source, tmp_path, cfg)
    assert first[0].text != second[0].text
    assert first[0].chunk_id != second[0].chunk_id


def test_resume_schedules_comparison_after_initial_extraction_failure(tmp_path):
    class EmptySpan(MockBackend):
        def generate_json(self, *args, **kwargs):
            out = super().generate_json(*args, **kwargs)
            for finding in out.get("findings", []):
                finding["span_id"] = ""
            return out

    (tmp_path / "a.txt").write_text("Atlas approval was issued.")
    (tmp_path / "b.txt").write_text("Atlas approval was cancelled.")
    failed = research(tmp_path, "Atlas approval", mode="evidence", backend=EmptySpan())
    assert failed["status"] == "incomplete"
    resumed = research(tmp_path, resume=failed["search_id"], backend=MockBackend())
    tasks = run_manifest(tmp_path, resumed["search_id"])["tasks"]
    assert resumed["status"] == "complete_for_retrieved_scope"
    assert any(t["kind"] == "compare" and t["status"] == "complete" for t in tasks)


def test_legacy_jsonl_history_survives_new_run_exports(tmp_path):
    state = tmp_path / ".codex-research"
    state.mkdir()
    legacy = {
        "id": "E0042",
        "fingerprint": "old",
        "search_id": "historical",
        "source_path": "old.txt",
        "source_hash": "old-hash",
        "excerpt": "Original retained evidence.",
        "context": "Original retained context.",
        "quote_verified": True,
    }
    (state / "evidence_ids.jsonl").write_text(
        json.dumps({"fingerprint": "old", "evidence_id": "E0042"}) + "\n"
    )
    (state / "evidence.jsonl").write_text(json.dumps(legacy) + "\n")
    (tmp_path / "a.txt").write_text("Atlas approval was issued.")
    result = research(
        tmp_path, "Atlas approval", mode="evidence", backend=MockBackend()
    )
    exported = [
        json.loads(line) for line in (state / "evidence.jsonl").read_text().splitlines()
    ]
    assert legacy in exported
    assert all(int(unit["id"][1:]) > 42 for unit in all_units(tmp_path, result))
    from repo_research import get_evidence

    assert get_evidence(tmp_path, "E0042")["excerpt"] == legacy["excerpt"]
