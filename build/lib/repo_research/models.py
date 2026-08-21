from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

@dataclass
class Chunk:
    chunk_id: str
    source_path: str
    source_type: str
    source_hash: str
    text: str
    ordinal: int
    page: int | None = None
    sheet: str | None = None
    section: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    row_start: int | None = None
    row_end: int | None = None
    paragraph_start: int | None = None
    paragraph_end: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    def dict(self) -> dict[str, Any]: return asdict(self)

@dataclass
class Evidence:
    id: str
    research_question: str
    topic: str
    source_path: str
    source_type: str
    excerpt: str
    context: str
    relevance: str
    worker_note: str
    worker_model: str
    retrieved_at: str
    source_hash: str
    search_id: str
    page: int | None = None
    sheet: str | None = None
    section: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    row_start: int | None = None
    row_end: int | None = None
    paragraph_start: int | None = None
    paragraph_end: int | None = None
    document_date: str | None = None
    event_date: str | None = None
    evidence_type: str = "contextual"
    stage: str = "other"
    qualification: str = ""
    contradiction: str = ""
    quote_verified: bool = False
    validation_flags: list[str] = field(default_factory=list)
    stale: bool = False
    fingerprint: str = ""
    def dict(self) -> dict[str, Any]: return asdict(self)
