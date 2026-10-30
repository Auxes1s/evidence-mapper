from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import json
from functools import lru_cache


@dataclass(frozen=True)
class TokenCount:
    tokens: int
    method: str


def conservative_token_count(*messages: str) -> TokenCount:
    """Safe tokenizer-independent upper bound for local byte-tokenized models.

    Every token consumes at least one encoded byte. The fixed allowance covers
    message-role and chat-template framing. This intentionally under-fills models
    without a registered exact tokenizer rather than risking silent truncation.
    """
    encoded_bytes = sum(len(message.encode("utf-8")) for message in messages)
    envelope = 64 + 16 * len(messages)
    return TokenCount(encoded_bytes + envelope, "utf8_byte_upper_bound_v1")


@lru_cache(maxsize=4)
def _tokenizer(path: str):
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise ValueError(
            "tokenizer_path requires the optional 'tokenizer' dependencies"
        ) from exc
    return AutoTokenizer.from_pretrained(
        path, local_files_only=True, trust_remote_code=False
    )


def request_token_count(
    system: str, prompt: str, schema=None, config=None
) -> TokenCount:
    schema_text = (
        json.dumps(schema, ensure_ascii=False, sort_keys=True) if schema else ""
    )
    path = (config or {}).get("tokenizer_path")
    if path:
        tokenizer = _tokenizer(path)
        count = len(
            tokenizer.apply_chat_template(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                tokenize=True,
                add_generation_prompt=True,
            )
        )
        # Reserve schema bytes independently of backend grammar/template behavior.
        return TokenCount(
            count + len(schema_text.encode()),
            "local_chat_tokenizer_plus_schema_bytes_v1",
        )
    return conservative_token_count(system, prompt, schema_text)


def call_fits(
    *,
    input_tokens: int,
    context_tokens: int,
    output_tokens: int,
    safety_margin_tokens: int,
) -> bool:
    return input_tokens + output_tokens + safety_margin_tokens <= context_tokens


def partition_by_budget(
    items: Iterable[object],
    render,
    *,
    system: str,
    prefix: str,
    context_tokens: int,
    output_tokens: int,
    safety_margin_tokens: int,
    schema=None,
    config=None,
) -> list[list[object]]:
    batches: list[list[object]] = []
    current: list[object] = []
    for item in items:
        trial = current + [item]
        prompt = render(trial)
        counted = request_token_count(
            system, prompt, schema(trial) if callable(schema) else schema, config
        )
        if call_fits(
            input_tokens=counted.tokens,
            context_tokens=context_tokens,
            output_tokens=output_tokens,
            safety_margin_tokens=safety_margin_tokens,
        ):
            current = trial
            continue
        if current:
            batches.append(current)
            current = []
        prompt = render([item])
        counted = request_token_count(
            system, prompt, schema([item]) if callable(schema) else schema, config
        )
        if not call_fits(
            input_tokens=counted.tokens,
            context_tokens=context_tokens,
            output_tokens=output_tokens,
            safety_margin_tokens=safety_margin_tokens,
        ):
            raise ValueError(
                "A single candidate exceeds the model context budget and must be rechunked"
            )
        current = [item]
    if current:
        batches.append(current)
    return batches
