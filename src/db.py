"""
db.py - Thin wrapper around the Supabase client.

Single source of truth for how we talk to the database. Keeps SQL/RPC details
in one place so the rest of the codebase stays clean.
"""

from __future__ import annotations

from typing import Any, Iterable
from uuid import UUID

from supabase import Client, create_client

from src.config import CONFIG


def _client() -> Client:
    """Build a Supabase client using the service_role key (bypasses RLS)."""
    return create_client(CONFIG.supabase_url, CONFIG.supabase_service_role_key)


# Module-level singleton — Supabase client is cheap to hold open.
SB: Client = _client()


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------

def find_source_by_hash(file_hash: str) -> dict | None:
    """Return the source row matching file_hash, or None. Used to skip already-ingested files."""
    res = SB.table("sources").select("*").eq("file_hash", file_hash).limit(1).execute()
    return res.data[0] if res.data else None


def insert_source(
    *,
    source_type: str,
    title: str,
    author: str | None,
    file_path: str | None,
    file_hash: str | None,
    metadata: dict | None = None,
) -> dict:
    """Create a new source row and return it (including the generated id)."""
    payload = {
        "source_type": source_type,
        "title": title,
        "author": author,
        "file_path": file_path,
        "file_hash": file_hash,
        "metadata": metadata or {},
    }
    res = SB.table("sources").insert(payload).execute()
    return res.data[0]


def mark_source_ingested(source_id: str | UUID) -> None:
    """Stamp ingested_at = now() on a source row to mark it complete."""
    SB.table("sources").update({"ingested_at": "now()"}).eq("id", str(source_id)).execute()


# ---------------------------------------------------------------------------
# chunks
# ---------------------------------------------------------------------------

def insert_chunks(rows: Iterable[dict]) -> int:
    """
    Bulk-insert chunks. Each row must include:
      source_id, chunk_index, content, word_count, page_number (nullable), embedding (nullable)
    Returns the number of rows inserted.
    """
    rows = list(rows)
    if not rows:
        return 0
    res = SB.table("chunks").insert(rows).execute()
    return len(res.data or [])


def count_chunks_for_source(source_id: str | UUID) -> int:
    res = (
        SB.table("chunks")
        .select("id", count="exact")
        .eq("source_id", str(source_id))
        .execute()
    )
    return res.count or 0


# ---------------------------------------------------------------------------
# similarity search (RAG retrieval)
# ---------------------------------------------------------------------------

def match_chunks(
    query_embedding: list[float],
    match_threshold: float = 0.5,
    match_count: int = 8,
) -> list[dict]:
    """
    Call the SQL function match_chunks() defined in db/schema.sql.
    Returns the top-N most similar chunks above the threshold.
    """
    res = SB.rpc(
        "match_chunks",
        {
            "query_embedding": query_embedding,
            "match_threshold": match_threshold,
            "match_count": match_count,
        },
    ).execute()
    return res.data or []


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------

def get_source_stats() -> list[dict]:
    """Return the source_stats view: per-source chunk counts and embedding coverage."""
    res = SB.table("source_stats").select("*").execute()
    return res.data or []
