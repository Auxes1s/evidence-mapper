"""Local repository research API."""

from .research import research
from .bundles import get_bundle, overview
from .retrieval import (
    contradiction_evidence,
    coverage_report,
    expand_evidence_context,
    get_evidence,
    open_source_location,
    related_evidence,
    run_manifest,
    telemetry,
)

__all__ = [
    "contradiction_evidence", "coverage_report", "expand_evidence_context", "get_evidence",
    "open_source_location", "related_evidence", "research", "run_manifest", "telemetry", "get_bundle", "overview",
]
__version__ = "0.5.1"
