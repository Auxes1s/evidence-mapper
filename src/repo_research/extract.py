from __future__ import annotations

import csv
import hashlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path

from docx import Document
from openpyxl import load_workbook
from pypdf import PdfReader

from .models import Chunk

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".json", ".jsonl", ".yaml", ".yml", ".toml",
    ".xml", ".html", ".htm", ".css", ".scss", ".js", ".jsx", ".ts", ".tsx", ".py",
    ".r", ".rmd", ".qmd", ".do", ".ado", ".java", ".c", ".h", ".cpp", ".hpp", ".go",
    ".rs", ".rb", ".php", ".sh", ".zsh", ".bash", ".sql", ".tex", ".bib", ".ini",
    ".cfg", ".conf", ".env.example", ".log", ".gitignore", ".dockerfile", ".makefile",
}
SUPPORTED = TEXT_EXTENSIONS | {".pdf", ".docx", ".xlsx", ".xlsm", ".csv", ".tsv"}

def file_hash(path: Path, timeout: int | float | None = None) -> str:
    # A child process gives us a hard timeout for cloud-backed reads. Python
    # signals cannot reliably interrupt a kernel filesystem call on macOS.
    shasum = shutil.which("shasum")
    if timeout and shasum:
        result = subprocess.run([shasum, "-a", "256", str(path)], capture_output=True,
                                text=True, check=True, timeout=timeout)
        return result.stdout.split()[0]
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""): h.update(block)
    return h.hexdigest()

def stable_id(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(str(x) for x in parts).encode()).hexdigest()[:24]

def extraction_policy(cfg: dict) -> str:
    return stable_id("extractors-v3",[(key,cfg.get(key)) for key in (
        "chunk_chars","chunk_overlap_chars","ocr_image_only_pdfs","ocr_max_pages","ocr_dpi")])

def _windows(text: str, size: int, overlap: int) -> Iterable[tuple[int, str]]:
    if len(text) <= size:
        yield 0, text
        return
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            split = max(text.rfind("\n\n", start, end), text.rfind("\n", start, end))
            if split > start + size // 2: end = split
        yield start, text[start:end]
        if end == len(text): break
        start = max(start + 1, end - overlap)

def _chunk_text(path: Path, rel: str, source_hash: str, text: str, cfg: dict,
                **location) -> list[Chunk]:
    out = []
    base_line = location.pop("line_start", None)
    for ordinal, (offset, part) in enumerate(_windows(text, cfg["chunk_chars"], cfg["chunk_overlap_chars"])):
        ls = base_line + text[:offset].count("\n") if base_line is not None else None
        le = ls + part.count("\n") if ls is not None else None
        loc = dict(location, line_start=ls, line_end=le)
        logical_id=stable_id(rel,location,ordinal,offset,cfg["chunk_chars"],cfg["chunk_overlap_chars"])
        parent_id=stable_id(rel,source_hash,location,"parent-v3")
        out.append(Chunk(stable_id(rel, source_hash, location, ordinal, offset,extraction_policy(cfg)), rel,
                         path.suffix.lower().lstrip(".") or "text", source_hash,
                         part.strip(), ordinal, metadata={"logical_chunk_id":logical_id,
                         "chunker_version":"structure-v3","parent_id":parent_id,
                         "parser_policy":extraction_policy(cfg),
                         **({"parent_text":text} if ordinal == 0 else {}),
                         "extracted_char_start":offset,
                         "extracted_char_end":offset+len(part)}, **loc))
    return [x for x in out if x.text]

def _ocr_pdf(path: Path, rel: str, digest: str, page_numbers: Iterable[int], cfg: dict) -> tuple[list[Chunk], list[int]]:
    if not cfg.get("ocr_image_only_pdfs"):
        return [], list(page_numbers)
    pdftoppm, tesseract = shutil.which("pdftoppm"), shutil.which("tesseract")
    if not pdftoppm or not tesseract:
        raise RuntimeError("image-only PDF requires pdftoppm and tesseract for OCR")
    requested = list(page_numbers)
    selected = requested[:int(cfg.get("ocr_max_pages", 120))]
    dpi = str(int(cfg.get("ocr_dpi", 200)))
    timeout = int(cfg.get("ocr_timeout_per_page_seconds", 90))
    out: list[Chunk] = []
    empty_pages=[]
    with tempfile.TemporaryDirectory(prefix="repo-research-ocr-") as temp:
        for page_no in selected:
            stem = str(Path(temp) / f"page-{page_no}")
            subprocess.run([pdftoppm, "-f", str(page_no), "-l", str(page_no), "-r", dpi,
                            "-png", "-singlefile", str(path), stem], check=True,
                           capture_output=True, timeout=timeout)
            image = stem + ".png"
            result = subprocess.run([tesseract, image, "stdout", "-l", "eng"], check=True,
                                    capture_output=True, text=True, timeout=timeout)
            text = result.stdout.strip()
            if text:
                out += _chunk_text(path, rel, digest, text, cfg, page=page_no, section="OCR")
            else: empty_pages.append(page_no)
    return out, empty_pages+requested[len(selected):]

def extract_file(path: Path, root: Path, cfg: dict, source_hash: str | None = None) -> tuple[list[Chunk], dict | None]:
    rel, ext, digest = str(path.relative_to(root)), path.suffix.lower(), source_hash or ""
    try:
        digest = digest or file_hash(path, cfg.get("file_timeout_seconds"))
        if ext == ".pdf":
            reader, out, missing_pages = PdfReader(str(path)), [], []
            for page_no, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                if text.strip():
                    out += _chunk_text(path, rel, digest, text, cfg, page=page_no)
                else:
                    missing_pages.append(page_no)
            omitted_ocr_pages: list[int] = []
            if missing_pages:
                ocr, omitted_ocr_pages = _ocr_pdf(path, rel, digest, missing_pages, cfg)
                out += ocr
            if not out: raise ValueError("PDF has no usable extractable text (OCR unavailable or produced no text)")
            unresolved = omitted_ocr_pages or ([p for p in missing_pages] if not cfg.get("ocr_image_only_pdfs") else [])
            diagnostic = None
            if unresolved:
                diagnostic = {"source_path":rel,"source_hash":digest,"source_type":"pdf",
                              "severity":"warning","error":"PDF pages had no extracted text",
                              "omitted_pages":unresolved}
            return out, diagnostic
        if ext == ".docx":
            from docx.table import Table
            from docx.text.paragraph import Paragraph
            doc, out, heading, para, table_no = Document(str(path)), [], None, 0, 0
            for child in doc.element.body:
                if child.tag.endswith("}p"):
                    p = Paragraph(child, doc); para += 1
                    if p.style and p.style.name.startswith("Heading"):
                        heading = p.text.strip()
                    if p.text.strip():
                        out += _chunk_text(path,rel,digest,p.text,cfg,section=heading,
                                           paragraph_start=para,paragraph_end=para)
                elif child.tag.endswith("}tbl"):
                    table_no += 1
                    table = Table(child, doc)
                    rows = [" | ".join(c.text.strip() for c in row.cells) for row in table.rows]
                    if rows:
                        for row_no, row in enumerate(rows[1:] or rows, 2 if len(rows)>1 else 1):
                            out += _chunk_text(path,rel,digest,"HEADERS: "+rows[0]+"\nROW "+str(row_no)+": "+row,
                                               cfg,section=f"{heading or ''} / Table {table_no}",
                                               row_start=row_no,row_end=row_no)
            return out, None
        if ext in {".xlsx", ".xlsm"}:
            wb, out = load_workbook(path, read_only=True, data_only=True), []
            for ws in wb.worksheets:
                rows = list(ws.iter_rows(values_only=True))
                if not rows: continue
                headers = [str(x) if x is not None else "" for x in rows[0]]
                for start in range(1, len(rows), 40):
                    subset = rows[start:start + 40]
                    rendered = ["HEADERS: " + " | ".join(headers)]
                    for rn, row in enumerate(subset, start + 1):
                        rendered.append(f"ROW {rn}: " + " | ".join("" if x is None else str(x) for x in row))
                    out += _chunk_text(path, rel, digest, "\n".join(rendered), cfg,
                                       sheet=ws.title, row_start=start + 1,
                                       row_end=start + len(subset))
            return out, None
        if ext in {".csv", ".tsv"}:
            delimiter = "\t" if ext == ".tsv" else ","
            with path.open(encoding="utf-8-sig", errors="replace", newline="") as fh:
                rows = list(csv.reader(fh, delimiter=delimiter))
            if not rows: return [], None
            out, headers = [], rows[0]
            for start in range(1, len(rows), 60):
                rendered = ["HEADERS: " + " | ".join(headers)] + [
                    f"ROW {rn}: " + " | ".join(row) for rn, row in enumerate(rows[start:start+60], start+1)]
                out += _chunk_text(path, rel, digest, "\n".join(rendered), cfg,
                                   row_start=start + 1, row_end=min(len(rows), start + 60))
            return out, None
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
            diagnostic = None
        except UnicodeDecodeError as exc:
            text = raw.decode("utf-8", errors="replace")
            diagnostic = {"source_path":rel,"source_hash":digest,"source_type":ext.lstrip("."),
                          "severity":"warning","error":"Invalid UTF-8 bytes were replaced",
                          "decode_error_start":exc.start}
        return _chunk_text(path, rel, digest, text, cfg, line_start=1), diagnostic
    except Exception as exc:
        return [], {"source_path": rel, "source_hash": digest, "error": str(exc), "source_type": ext.lstrip(".")}

def discover_files(root: Path, cfg: dict, failures: list[dict] | None = None) -> tuple[list[Path], list[dict]]:
    from fnmatch import fnmatch
    out, manifest = [], []
    failures = failures if failures is not None else []
    ignores, includes, excludes = set(cfg["ignore_dirs"]), cfg.get("include", []), cfg.get("exclude", [])
    def walk_error(exc: OSError) -> None:
        failures.append({"source_path": getattr(exc, "filename", "") or "", "source_hash": "",
                         "error": str(exc), "source_type": "filesystem"})
    for current, dirnames, filenames in os.walk(root, onerror=walk_error):
        current_path = Path(current)
        dirnames[:] = [d for d in dirnames if d not in ignores]
        for name in filenames:
            p = current_path / name
            try:
                rel = str(p.relative_to(root))
                st = p.stat()
                item = {"source_path":rel,"source_type":p.suffix.lower().lstrip(".") or p.name.lower(),
                        "size":st.st_size,"mtime":st.st_mtime,"status":"eligible","reason":None,
                        "parser_version":"extractors-v3"}
                if includes and not any(fnmatch(rel, x) for x in includes):
                    item.update(status="excluded", reason="not matched by include policy")
                elif any(fnmatch(rel, x) for x in excludes):
                    item.update(status="excluded", reason="matched exclude policy")
                elif st.st_size > cfg["max_file_bytes"]:
                    item.update(status="oversized", reason=f"exceeds max_file_bytes={cfg['max_file_bytes']}")
                elif not (p.suffix.lower() in SUPPORTED or p.name.lower() in {"dockerfile", "makefile", "license"}):
                    item.update(status="unsupported", reason="unsupported file type")
                else:
                    out.append(p)
                manifest.append(item)
            except OSError as exc:
                record={"source_path":str(p),"source_hash":"","error":str(exc),
                        "source_type":p.suffix.lower().lstrip(".") or "filesystem"}
                failures.append(record)
                manifest.append({"source_path":str(p),"source_type":record["source_type"],
                                 "status":"failed","reason":str(exc),"size":None,"mtime":None})
    return sorted(out), sorted(manifest, key=lambda x:x["source_path"])


def eligible_files(root: Path, cfg: dict, failures: list[dict] | None = None) -> list[Path]:
    files, _ = discover_files(root, cfg, failures)
    return files
