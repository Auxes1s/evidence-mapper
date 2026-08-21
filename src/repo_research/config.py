from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any
import yaml

DEFAULTS: dict[str, Any] = {
    "model": "qwen3.5:9b-mlx",
    "backend": "ollama",
    "ollama_url": "http://127.0.0.1:11434",
    "context_tokens": 32768,
    "max_parallel_jobs": 1,
    "temperature": 0.1,
    "request_timeout_seconds": 600,
    "chunk_chars": 4000,
    "chunk_overlap_chars": 400,
    "candidate_limit": 12,
    "max_prompt_chars": 48000,
    "max_output_tokens": 1800,
    "frontier_output_budget": "standard",
    "frontier_output_budgets": {"compact": 750, "standard": 1500, "expanded": 4000},
    "max_promoted_records": 12,
    "max_file_bytes": 20_000_000,
    "file_timeout_seconds": 120,
    "ocr_image_only_pdfs": False,
    "ocr_max_pages": 120,
    "ocr_dpi": 200,
    "ocr_timeout_per_page_seconds": 90,
    "context_expansion": 1,
    "include": [],
    "exclude": [],
    "ignore_dirs": [
        ".git", ".hg", ".svn", ".codex-research", "node_modules", "venv",
        ".venv", "env", "dist", "build", "__pycache__", ".cache", "cache",
        ".mypy_cache", ".pytest_cache", ".tox", "coverage", "vendor",
    ],
    "source_hierarchy": [
        "signed primary records", "formal issuances", "contemporaneous official minutes",
        "approved reports", "official correspondence", "working records", "presentations",
        "retrospective summaries", "analyst interpretation",
    ],
}

def deep_merge(base: dict, override: dict) -> dict:
    out = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out

def load_config(root: Path, explicit: str | Path | None = None) -> dict[str, Any]:
    cfg = deepcopy(DEFAULTS)
    candidates = [Path.home() / ".config/repo-research/config.yaml"]
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(root / ".codex-research/config.yaml")
    for path in candidates:
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            cfg = deep_merge(cfg, data)
    return cfg

def ensure_repo_config(root: Path, cfg: dict[str, Any]) -> Path:
    state = root / ".codex-research"
    state.mkdir(parents=True, exist_ok=True)
    path = state / "config.yaml"
    if not path.exists():
        keys = ("model", "backend", "ollama_url", "context_tokens", "max_parallel_jobs",
                "temperature", "frontier_output_budget", "frontier_output_budgets",
                "max_promoted_records", "include", "exclude", "source_hierarchy")
        path.write_text(yaml.safe_dump({k: cfg[k] for k in keys}, sort_keys=False), encoding="utf-8")
    gitignore = root / ".gitignore"
    if (root / ".git").exists() and gitignore.exists():
        lines = gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
        if ".codex-research/" not in {x.strip() for x in lines}:
            with gitignore.open("a", encoding="utf-8") as fh:
                if lines and lines[-1] != "": fh.write("\n")
                fh.write(".codex-research/\n")
    return path
