"""
chunker.py - Sentence-aware text chunking.

Goal: produce chunks of ~CHUNK_WORDS words with CHUNK_OVERLAP_WORDS overlap,
without ever splitting a sentence in half. Each chunk keeps the page number
of where it started, so we can cite "book X, page Y" in answers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.config import CONFIG
from src.pdf_utils import PageText


# Sentence boundary: end-of-sentence punctuation followed by whitespace.
# Works for English. For Hebrew, period/question/exclamation marks behave the same.
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")

# Word splitter that handles Unicode (Hebrew + English).
_WORD_SPLIT = re.compile(r"\S+")


@dataclass
class Chunk:
    chunk_index: int    # 0-based, position within the document
    page_number: int    # 1-based page where this chunk *starts*
    text: str
    word_count: int


def build_chunks(pages: list[PageText]) -> list[Chunk]:
    """
    Build chunks from a list of PageText (in page order).

    Algorithm:
      1. Concatenate all page text, but track the page boundaries so we know
         which page each sentence started on.
      2. Split into sentences.
      3. Greedily pack sentences into chunks of ~CHUNK_WORDS words.
      4. When closing a chunk, prepend the last CHUNK_OVERLAP_WORDS words of
         the previous chunk to the next one (continuity at boundaries).
    """
    target = CONFIG.chunk_words
    overlap = CONFIG.chunk_overlap_words

    # Step 1: flatten pages into sentences, tagged with their starting page.
    sentences = _flatten_to_sentences(pages)
    if not sentences:
        return []

    # Step 2: greedy packing
    chunks: list[Chunk] = []
    current_words: list[str] = []
    current_start_page: int | None = None
    chunk_index = 0

    for sent_text, sent_page in sentences:
        words = _WORD_SPLIT.findall(sent_text)
        if not words:
            continue
        if current_start_page is None:
            current_start_page = sent_page

        # If adding this sentence would overshoot AND we already have something,
        # close the current chunk.
        if current_words and len(current_words) + len(words) > target:
            chunks.append(
                Chunk(
                    chunk_index=chunk_index,
                    page_number=current_start_page,
                    text=" ".join(current_words),
                    word_count=len(current_words),
                )
            )
            chunk_index += 1
            # Seed next chunk with overlap from the tail of the closed one.
            tail = current_words[-overlap:] if overlap > 0 else []
            current_words = list(tail)
            current_start_page = sent_page

        current_words.extend(words)

    # Flush the trailing chunk
    if current_words:
        chunks.append(
            Chunk(
                chunk_index=chunk_index,
                page_number=current_start_page or 1,
                text=" ".join(current_words),
                word_count=len(current_words),
            )
        )

    return chunks


def _flatten_to_sentences(pages: list[PageText]) -> list[tuple[str, int]]:
    """
    Return a list of (sentence_text, page_number) tuples.
    A sentence is attributed to the page where it begins.
    """
    out: list[tuple[str, int]] = []
    for page in pages:
        if not page.text.strip():
            continue
        # Normalize whitespace: collapse newlines / multiple spaces.
        normalized = re.sub(r"\s+", " ", page.text).strip()
        if not normalized:
            continue
        sentences = _SENT_SPLIT.split(normalized)
        for s in sentences:
            s = s.strip()
            if s:
                out.append((s, page.page_number))
    return out
