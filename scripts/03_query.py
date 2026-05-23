"""
03_query.py - Interactive CLI for asking questions against the knowledge base.

Flow per question:
  1. Embed the question with OpenAI (text-embedding-3-small).
  2. Retrieve the top-K most similar chunks from Supabase via match_chunks().
  3. Build a prompt that includes the chunks as context + the question.
  4. Send to Claude and stream the answer.
  5. Print citations (book + page) for the chunks used.

Run: python scripts/03_query.py
Type 'q' or 'quit' or 'exit' to leave.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

# Make `src` importable when running from project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anthropic import Anthropic

from src.config import CONFIG
from src.db import match_chunks
from src.embeddings import embed_many


# How many chunks to retrieve as context for each question.
TOP_K = 8

# Cosine similarity threshold. Below this, a chunk is considered irrelevant.
# 0.3 is permissive; 0.5 is strict. Start permissive — it's easier to filter
# noise after seeing the model's behavior than to discover you're missing context.
SIMILARITY_THRESHOLD = 0.3


SYSTEM_PROMPT = """You are an investment-research assistant grounded in a private library of finance books.

You answer in the user's language (Hebrew or English, matching the question).

You answer ONLY from the provided context. If the context does not contain the answer,
say so plainly — do not invent facts or rely on general training knowledge.

When you make a claim, attach a citation in the form [#N] referring to the source
numbers listed in the context. Multiple citations are fine: [#1][#3].

Be concise. Prefer a clear short answer over a long survey. If the user wants depth,
they will ask follow-ups.
""".strip()


def _format_context(chunks: list[dict]) -> str:
    """Turn retrieved chunks into a numbered, citation-ready context block."""
    lines = []
    for i, c in enumerate(chunks, start=1):
        page = c.get("page_number")
        page_str = f"p.{page}" if page else "p.?"
        title = c.get("source_title", "Unknown")
        author = c.get("source_author")
        author_str = f" — {author}" if author else ""
        sim = c.get("similarity", 0.0)
        header = f"[#{i}] {title}{author_str} ({page_str}, similarity={sim:.2f})"
        body = c.get("content", "").strip()
        lines.append(f"{header}\n{body}")
    return "\n\n---\n\n".join(lines)


def _print_citations(chunks: list[dict]) -> None:
    print()
    print("─" * 70)
    print("Sources used:")
    for i, c in enumerate(chunks, start=1):
        page = c.get("page_number")
        page_str = f"p.{page}" if page else "p.?"
        title = c.get("source_title", "Unknown")
        author = c.get("source_author")
        author_str = f" — {author}" if author else ""
        sim = c.get("similarity", 0.0)
        print(f"  [#{i}] {title}{author_str} ({page_str})   sim={sim:.2f}")
    print()


def _stream_answer(client: Anthropic, question: str, context: str) -> None:
    """Send to Claude and stream the response."""
    user_message = (
        f"Context (retrieved from the library):\n\n"
        f"{context}\n\n"
        f"---\n\n"
        f"Question: {question}\n\n"
        f"Answer using only the context above. Cite sources as [#N]."
    )

    with client.messages.stream(
        model=CONFIG.chat_model,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
    print()  # final newline


def _answer_question(client: Anthropic, question: str) -> None:
    # Step 1: embed the question
    [q_embedding] = embed_many([question])

    # Step 2: retrieve
    chunks = match_chunks(
        query_embedding=q_embedding,
        match_threshold=SIMILARITY_THRESHOLD,
        match_count=TOP_K,
    )

    if not chunks:
        print()
        print("[no relevant context found in the library for this question]")
        print("Try rephrasing, or ingest more books.")
        return

    # Step 3: build context + ask Claude
    context = _format_context(chunks)

    print()
    print("─" * 70)
    print("Answer:")
    print()
    _stream_answer(client, question, context)

    # Step 4: print citations
    _print_citations(chunks)


def main() -> int:
    print("=" * 70)
    print("  investment-research-rag — interactive query CLI")
    print("=" * 70)
    print(f"  Embedding model: {CONFIG.embedding_model}")
    print(f"  Chat model:      {CONFIG.chat_model}")
    print(f"  Retrieving:      top-{TOP_K} chunks above similarity {SIMILARITY_THRESHOLD}")
    print()
    print("  Type a question (Hebrew or English). 'q' to quit.")
    print()

    client = Anthropic(api_key=CONFIG.anthropic_api_key)

    while True:
        try:
            question = input("? ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not question:
            continue
        if question.lower() in {"q", "quit", "exit"}:
            break

        try:
            _answer_question(client, question)
        except Exception as e:
            print()
            print(f"[error] {e}")
            print("Try again with a different question, or check logs.")

    print("Bye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
