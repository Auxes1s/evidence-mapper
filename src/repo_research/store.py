from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

FILES = ["source_index.jsonl", "evidence.jsonl", "chronology.jsonl", "entities.jsonl",
         "searches.jsonl", "unresolved.jsonl", "extraction_failures.jsonl",
         "evidence_ids.jsonl", "packets.jsonl", "telemetry.jsonl", "expansions.jsonl",
         "qwen_failures.jsonl"]
FILES.append("promotion_omissions.jsonl")

class Store:
    def __init__(self, root: Path):
        self.root = root
        self.state = root / ".codex-research"
        self.cache = self.state / "cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        for name in FILES:
            (self.state / name).touch(exist_ok=True)

    def append(self, name: str, record: dict[str, Any]) -> None:
        with (self.state / name).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def append_many(self, name: str, records: Iterable[dict[str, Any]]) -> None:
        with (self.state / name).open("a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def read(self, name: str) -> list[dict[str, Any]]:
        out = []
        path = self.state / name
        if not path.exists(): return out
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            try: out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Corrupt JSONL record in {path} at line {line_no}: {exc}") from exc
        return out

    def replace(self, name: str, records: Iterable[dict[str, Any]]) -> None:
        path = self.state / name
        with path.open("w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def assign_evidence_ids(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Assign repository-stable human-facing E#### IDs to evidence fingerprints."""
        from .workspace import Workspace
        ledger = Workspace(self.state)
        try:
            assigned = ledger.assign_ids(records)
            mappings = [{"fingerprint": r[0], "evidence_id": r[1]} for r in
                        ledger.db.execute("SELECT fingerprint,evidence_id FROM evidence_ids ORDER BY evidence_id")]
            self.replace("evidence_ids.jsonl", mappings)
            return assigned
        finally:
            ledger.db.close()
