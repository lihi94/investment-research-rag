"""
config.py - Centralized configuration loader.

Loads environment variables from .env and exposes them as a frozen Config object.
Fails loudly on startup if a required key is missing — better than a confusing
runtime error 10 minutes into ingestion.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


# Load .env from the project root. This searches upward from the current file,
# so it works whether scripts are run from project root or from scripts/.
# override=True so values in .env always win over inherited shell env vars —
# otherwise an empty inherited variable (common on Windows) silently blocks .env.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env", override=True)


@dataclass(frozen=True)
class Config:
    # --- Supabase ---
    supabase_url: str
    supabase_service_role_key: str

    # --- OpenAI ---
    openai_api_key: str
    embedding_model: str

    # --- Anthropic ---
    anthropic_api_key: str
    chat_model: str

    # --- Paths ---
    pdf_dir: Path
    log_dir: Path

    # --- Chunking ---
    chunk_words: int
    chunk_overlap_words: int


def _require(name: str) -> str:
    """Read an env var or exit with a clear error message."""
    value = os.getenv(name)
    if not value or value.startswith("your-") or value == "sk-...":
        raise RuntimeError(
            f"Missing or unset environment variable: {name}\n"
            f"Did you copy .env.example to .env and fill in the real values?"
        )
    return value


def load_config() -> Config:
    """Build the Config from env vars. Call once at the start of each script."""
    cfg = Config(
        supabase_url=_require("SUPABASE_URL"),
        supabase_service_role_key=_require("SUPABASE_SERVICE_ROLE_KEY"),
        openai_api_key=_require("OPENAI_API_KEY"),
        embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
        chat_model=os.getenv("CHAT_MODEL", "claude-sonnet-4-6"),
        pdf_dir=Path(os.getenv("PDF_DIR", "./data/pdfs")).resolve(),
        log_dir=Path(os.getenv("LOG_DIR", "./logs")).resolve(),
        chunk_words=int(os.getenv("CHUNK_WORDS", "650")),
        chunk_overlap_words=int(os.getenv("CHUNK_OVERLAP_WORDS", "80")),
    )

    # Sanity checks
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    if not cfg.pdf_dir.exists():
        # Don't fail — the survey script needs to be able to report "no PDFs yet".
        # Just warn.
        print(f"[config] WARNING: PDF_DIR does not exist: {cfg.pdf_dir}")

    return cfg


# Convenience: a module-level singleton for quick imports.
# Use `from src.config import CONFIG`.
CONFIG = load_config()
