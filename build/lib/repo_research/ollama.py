from __future__ import annotations

import json, re
from typing import Any, Protocol
import httpx

class Backend(Protocol):
    model: str
    def generate_json(self, system: str, prompt: str, schema: dict | None = None) -> Any: ...

class OllamaBackend:
    def __init__(self, cfg: dict):
        self.model, self.url, self.cfg = cfg["model"], cfg["ollama_url"].rstrip("/"), cfg
        self.usage_log: list[dict[str, int | float]] = []
        self.malformed_log: list[dict[str, Any]] = []

    def health(self) -> dict:
        with httpx.Client(timeout=10) as client:
            version = client.get(self.url + "/api/version"); version.raise_for_status()
            show = client.post(self.url + "/api/show", json={"model": self.model}); show.raise_for_status()
            return {"version": version.json().get("version"), "model": self.model,
                    "details": show.json().get("details", {}), "capabilities": show.json().get("capabilities", [])}

    def generate_json(self, system: str, prompt: str, schema: dict | None = None) -> Any:
        payload = {
            "model": self.model, "stream": False, "think": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "format": schema or "json",
            "options": {"temperature": self.cfg["temperature"], "num_ctx": self.cfg["context_tokens"],
                        "num_predict": self.cfg["max_output_tokens"]},
            "keep_alive": "10m",
        }
        last = None
        for attempt in range(2):
            with httpx.Client(timeout=self.cfg["request_timeout_seconds"]) as client:
                response = client.post(self.url + "/api/chat", json=payload)
                response.raise_for_status()
            body = response.json()
            content = body.get("message", {}).get("content", "")
            self.usage_log.append({
                "input_tokens": int(body.get("prompt_eval_count") or max(1, len(prompt) // 4)),
                "output_tokens": int(body.get("eval_count") or max(1, len(content) // 4)),
                "elapsed_seconds": round(float(body.get("total_duration") or 0) / 1_000_000_000, 6),
            })
            try: return json.loads(content)
            except json.JSONDecodeError as exc:
                last = exc
                match = re.search(r"\{.*\}|\[.*\]", content, re.S)
                if match:
                    try: return json.loads(match.group())
                    except json.JSONDecodeError: pass
                self.malformed_log.append({"attempt":attempt + 1, "raw":content,
                    "error":str(exc), "retry":attempt == 0})
                if attempt == 0:
                    # Exactly one retry, with a smaller generation ceiling and a
                    # constrained reminder. The original task remains available.
                    payload["options"]["num_predict"] = min(900, self.cfg["max_output_tokens"])
                    payload["messages"].append({"role":"assistant","content":content[:2000]})
                    payload["messages"].append({"role":"user","content":
                        "Retry once. Return only one complete JSON object matching the schema. Select at most 4 strongest span IDs; no prose."})
        raise ValueError(f"Ollama returned malformed JSON: {last}")

class MockBackend:
    """Deterministic backend for tests; emits exact matching sentences."""
    model = "mock-retriever"
    def __init__(self): self.usage_log = []
    def generate_json(self, system: str, prompt: str, schema: dict | None = None) -> Any:
        if "DECOMPOSE" in prompt:
            claim = prompt.split("CLAIM:", 1)[-1].strip()
            parts = [x.strip(" .") for x in re.split(r",|\band\b", claim) if len(x.strip()) > 5]
            return {"claims": parts or [claim]}
        findings = []
        for block in prompt.split("--- CANDIDATE ")[1:]:
            header, _, text = block.partition("\nTEXT:\n")
            try: meta = json.loads(header.split(" ---", 1)[0])
            except Exception: continue
            supplied = []
            for line in text.strip().splitlines():
                match = re.match(r"\[(S\d+)\]\s*(.*)", line)
                if match: supplied.append((match.group(1), match.group(2)))
            sentences = [x[1] for x in supplied] or re.split(r"(?<=[.!?])\s+|\n", text.strip())
            keywords = set(re.findall(r"[a-z0-9]{4,}", prompt.split("CANDIDATES:",1)[0].lower()))
            ranked = sorted(sentences, key=lambda s: len(keywords & set(re.findall(r"[a-z0-9]{4,}", s.lower()))), reverse=True)
            if ranked and ranked[0] and len(keywords & set(re.findall(r"[a-z0-9]{4,}", ranked[0].lower()))) >= 1:
                span_id = next((sid for sid, sentence in supplied if sentence == ranked[0]), None)
                findings.append({"chunk_id": meta["chunk_id"], "excerpt": ranked[0][:1000],
                    **({"span_id":span_id} if span_id else {}),
                    "topic": "candidate evidence", "document_date": None, "event_date": None,
                    "evidence_type": "contradictory" if "contradiction" in prompt.lower() or "incorrect" in ranked[0].lower() else "direct",
                    "stage": "other", "relevance": "direct", "qualification": "", "contradiction": "",
                    "short_note": "Exact candidate passage."})
        result = {"findings": findings, "no_evidence_found": not findings, "suggested_terms": []}
        self.usage_log.append({"input_tokens": max(1, len(prompt)//4),
                               "output_tokens": max(1, len(json.dumps(result))//4),
                               "elapsed_seconds": 0.0})
        return result
