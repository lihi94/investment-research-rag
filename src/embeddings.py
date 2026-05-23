"""
embeddings.py - OpenAI text-embedding-3-small wrapper.

Batches multiple texts into one API call (much faster + cheaper per item).
Counts tokens up-front for cost estimation.
"""

from __future__ import annotations

import time
from typing import Iterable

import tiktoken
from openai import OpenAI

from src.config import CONFIG


# Per-million-token pricing in USD for text-embedding-3-small (as of 2024-2025).
# Update if OpenAI changes pricing.
PRICE_PER_MILLION_TOKENS = 0.02

# OpenAI limits: max 8191 tokens per input, max ~300k tokens per batch request.
# We use conservative limits to stay safe.
MAX_TOKENS_PER_INPUT = 8000
MAX_BATCH_TOKENS = 250_000
MAX_BATCH_ITEMS = 100


_client: OpenAI | None = None
_encoder = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=CONFIG.openai_api_key)
    return _client


def _get_encoder():
    """Get the tokenizer for the embedding model. cl100k_base covers text-embedding-3-*."""
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


def count_tokens(text: str) -> int:
    """Exact token count for cost estimation and batch sizing."""
    return len(_get_encoder().encode(text))


def estimate_cost(total_tokens: int) -> float:
    """Convert a token count to USD cost."""
    return (total_tokens / 1_000_000) * PRICE_PER_MILLION_TOKENS


def embed_batch(texts: list[str], max_retries: int = 3) -> list[list[float]]:
    """
    Embed a single batch of texts in one API call.
    The caller is responsible for keeping the batch under the API limits;
    use `embed_many` for arbitrary-size input.
    """
    if not texts:
        return []

    client = _get_client()
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = client.embeddings.create(
                model=CONFIG.embedding_model,
                input=texts,
            )
            return [item.embedding for item in resp.data]
        except Exception as e:
            last_err = e
            # Exponential backoff: 2s, 4s, 8s
            time.sleep(2 ** (attempt + 1))

    raise RuntimeError(f"Embedding API failed after {max_retries} retries: {last_err}")


def embed_many(texts: list[str]) -> list[list[float]]:
    """
    Embed any number of texts, splitting into safe-sized batches automatically.
    Returns embeddings in the same order as the input.
    """
    if not texts:
        return []

    results: list[list[float]] = []
    enc = _get_encoder()

    batch: list[str] = []
    batch_tokens = 0

    for text in texts:
        n_tokens = len(enc.encode(text))
        if n_tokens > MAX_TOKENS_PER_INPUT:
            # Truncate over-long inputs (rare for our ~650-word chunks, but safe).
            tokens = enc.encode(text)[:MAX_TOKENS_PER_INPUT]
            text = enc.decode(tokens)
            n_tokens = MAX_TOKENS_PER_INPUT

        would_overflow = (
            len(batch) >= MAX_BATCH_ITEMS
            or (batch_tokens + n_tokens) > MAX_BATCH_TOKENS
        )
        if would_overflow and batch:
            results.extend(embed_batch(batch))
            batch = []
            batch_tokens = 0

        batch.append(text)
        batch_tokens += n_tokens

    if batch:
        results.extend(embed_batch(batch))

    return results
