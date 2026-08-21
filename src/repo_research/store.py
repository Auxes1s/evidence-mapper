from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

FILES = ["source_index.jsonl", "evidence.jsonl", "chronology.jsonl", "entities.jsonl",
         "searches.jsonl", "unresolved.jsonl", "extraction_failures.jsonl",
         "evidence_ids.jsonl", "packets.jsonl", "telemetry.jsonl", "expansions.jsonl",
         "qwen_failures.jsonl"]

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
        for line in path.read_text(encoding="utf-8").splitlines():
            try: out.append(json.loads(line))
            except json.JSONDecodeError: continue
        return out

    def replace(self, name: str, records: Iterable[dict[str, Any]]) -> None:
        path = self.state / name
        with path.open("w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def assign_evidence_ids(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Assign repository-stable human-facing E#### IDs to evidence fingerprints."""
        mappings = self.read("evidence_ids.jsonl")
        by_fingerprint = {x["fingerprint"]: x["evidence_id"] for x in mappings
                          if x.get("fingerprint") and x.get("evidence_id")}
        used = [int(x[1:]) for x in by_fingerprint.values()
                if isinstance(x, str) and x.startswith("E") and x[1:].isdigit()]
        next_id = max(used, default=0) + 1
        created = []
        out = []
        for original in records:
            rec = dict(original)
            fingerprint = rec.get("fingerprint") or rec.get("id", "")
            evidence_id = by_fingerprint.get(fingerprint)
            if not evidence_id:
                evidence_id = f"E{next_id:04d}"
                next_id += 1
                by_fingerprint[fingerprint] = evidence_id
                created.append({"fingerprint": fingerprint, "evidence_id": evidence_id})
            rec["fingerprint"] = fingerprint
            rec["id"] = evidence_id
            out.append(rec)
        if created:
            self.append_many("evidence_ids.jsonl", created)
        return out
