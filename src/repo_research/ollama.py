from __future__ import annotations

import json, re
from typing import Any, Protocol
import httpx
from .budgeting import request_token_count, call_fits

class OutputExhausted(ValueError):
    pass

class Backend(Protocol):
    model: str
    def generate_json(self, system: str, prompt: str, schema: dict | None = None) -> Any: ...

class OllamaBackend:
    def __init__(self, cfg: dict):
        self.model, self.url, self.cfg = cfg["model"], cfg["ollama_url"].rstrip("/"), cfg
        self.usage_log: list[dict[str, Any]] = []
        self.malformed_log: list[dict[str, Any]] = []

    def health(self) -> dict:
        with httpx.Client(timeout=10) as client:
            version = client.get(self.url + "/api/version"); version.raise_for_status()
        return {"version":version.json().get("version"),**self.model_info(self.model)}

    def model_info(self, model: str) -> dict:
        with httpx.Client(timeout=10) as client:
            show=client.post(self.url+"/api/show",json={"model":model}); show.raise_for_status()
            tags=client.get(self.url+"/api/tags"); tags.raise_for_status()
        shown=show.json()
        installed=next((x for x in tags.json().get("models",[])
                        if x.get("name")==model or x.get("model")==model),{})
        return {"model":model,"digest":installed.get("digest") or shown.get("digest"),
                "modified_at":installed.get("modified_at") or shown.get("modified_at"),
                "details":shown.get("details",{}),"capabilities":shown.get("capabilities",[])}

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
        attempts=int(self.cfg.get("max_attempts",2))
        for attempt in range(attempts):
            counted=request_token_count(system,payload["messages"][1]["content"],schema,self.cfg)
            if not call_fits(input_tokens=counted.tokens,context_tokens=int(self.cfg["context_tokens"]),
                             output_tokens=int(payload["options"]["num_predict"]),
                             safety_margin_tokens=int(self.cfg.get("context_safety_margin_tokens",1024))):
                raise ValueError("Model request exceeds its preflight context budget; split the task")
            with httpx.Client(timeout=self.cfg["request_timeout_seconds"]) as client:
                response = client.post(self.url + "/api/chat", json=payload)
                response.raise_for_status()
            body = response.json()
            content = body.get("message", {}).get("content", "")
            self.usage_log.append({
                "input_tokens": int(body.get("prompt_eval_count") or max(1, len(prompt) // 4)),
                "output_tokens": int(body.get("eval_count") or max(1, len(content) // 4)),
                "elapsed_seconds": round(float(body.get("total_duration") or 0) / 1_000_000_000, 6),
                "done": bool(body.get("done", False)),
                "done_reason": body.get("done_reason"),
                "preflight_input_tokens":counted.tokens,"token_method":counted.method,
                "request":json.loads(json.dumps(payload)),"response_content":content,
            })
            if body.get("done") is False:
                raise OutputExhausted("Backend did not finish the generation")
            if body.get("done_reason") in {"length","max_tokens"}:
                raise OutputExhausted("Model exhausted its output budget; split the task")
            try: return json.loads(content)
            except json.JSONDecodeError as exc:
                last = exc
                match = re.search(r"\{.*\}|\[.*\]", content, re.S)
                if match:
                    try: return json.loads(match.group())
                    except json.JSONDecodeError: pass
                self.malformed_log.append({"attempt":attempt + 1, "raw":content,
                    "error":str(exc), "retry":attempt+1 < attempts})
                if attempt+1 < attempts:
                    # Repeat the original bounded request. Malformed output never
                    # inflates retry context or shrinks the disposition budget.
                    payload["messages"]=[{"role":"system","content":system},{"role":"user","content":prompt}]
        raise ValueError(f"Ollama returned malformed JSON: {last}")

    def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        """Generate local embeddings without permitting backend truncation."""
        payload={"model":model or self.cfg.get("embedding_model"),"input":texts,
                 "truncate":False,"keep_alive":"10m"}
        if not payload["model"]: raise ValueError("embedding_model is not configured")
        with httpx.Client(timeout=self.cfg["request_timeout_seconds"]) as client:
            response=client.post(self.url+"/api/embed",json=payload); response.raise_for_status()
        vectors=response.json().get("embeddings",[])
        if len(vectors)!=len(texts):
            raise ValueError(f"Embedding backend returned {len(vectors)} vectors for {len(texts)} inputs")
        return [[float(x) for x in vector] for vector in vectors]

class MockBackend:
    """Deterministic backend for tests; emits exact matching sentences."""
    model = "mock-retriever"
    def __init__(self): self.usage_log = []
    def model_info(self,model): return {"model":model,"digest":"deterministic-test-v1"}
    def embed(self,texts,model=None):
        import hashlib
        vectors=[]
        for text in texts:
            vec=[0.0]*64
            for word in re.findall(r"\w+",text.lower()):
                vec[int(hashlib.sha256(word.encode()).hexdigest()[:8],16)%64]+=1
            vectors.append(vec)
        return vectors
    def generate_json(self, system: str, prompt: str, schema: dict | None = None) -> Any:
        if "RELATION_CHECK" in prompt:
            self.usage_log.append({"input_tokens":len(prompt)//4,"output_tokens":20,"elapsed_seconds":0,"done_reason":"stop"})
            return {"relations":[],"unresolved":[]}
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
        evidence_chunks={x["chunk_id"] for x in findings}
        candidate_ids=[]
        for block in prompt.split("--- CANDIDATE ")[1:]:
            header=block.partition("\nTEXT:\n")[0]
            try: candidate_ids.append(json.loads(header.split(" ---",1)[0])["chunk_id"])
            except Exception: pass
        result = {"findings": findings,
                  "dispositions":[{"chunk_id":cid,"status":"evidence_extracted" if cid in evidence_chunks else "no_relevant_evidence","reason":"deterministic mock disposition"} for cid in candidate_ids],
                  "no_evidence_found": not findings, "suggested_terms": []}
        self.usage_log.append({"input_tokens": max(1, len(prompt)//4),
                               "output_tokens": max(1, len(json.dumps(result))//4),
                               "elapsed_seconds": 0.0,"done":True,"done_reason":"stop"})
        return result
