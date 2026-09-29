"""Runtime configuration, read from environment variables.

Settings are read on every call (not cached) so tests can monkeypatch the
environment without import-order tricks.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[1]
BUNDLED_DATA_DIR = BACKEND_DIR / "data"

load_dotenv(BACKEND_DIR / ".env")

DEFAULT_MODEL = "qwen/qwen3.8-27b"


def _int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    groq_api_key: str
    vision_model: str
    text_model: str
    groq_base_url: str
    groq_max_retries: int
    groq_timeout_s: int
    runtime_dir: Path
    frontend_origins: list[str]
    frontend_origin_regex: str | None
    reviewer_token: str
    max_file_mb: int
    max_files: int
    max_pages_per_document: int
    rate_limit_per_10_min: int


def get_settings() -> Settings:
    vision = os.getenv("GROQ_VISION_MODEL") or os.getenv("GROQ_MODEL") or DEFAULT_MODEL
    text = os.getenv("GROQ_TEXT_MODEL") or os.getenv("GROQ_MODEL") or DEFAULT_MODEL
    origins = [o.strip().rstrip("/") for o in os.getenv("FRONTEND_ORIGIN", "").split(",") if o.strip()]
    return Settings(
        groq_api_key=os.getenv("GROQ_API_KEY", "").strip(),
        vision_model=vision.strip(),
        text_model=text.strip(),
        groq_base_url=os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/"),
        groq_max_retries=_int("GROQ_MAX_RETRIES", 3, 0, 6),
        groq_timeout_s=_int("GROQ_TIMEOUT_SECONDS", 90, 10, 300),
        runtime_dir=Path(os.getenv("DATA_DIR", str(BUNDLED_DATA_DIR / "runtime"))).expanduser().resolve(),
        frontend_origins=origins,
        frontend_origin_regex=(os.getenv("FRONTEND_ORIGIN_REGEX") or "").strip() or None,
        reviewer_token=os.getenv("REVIEWER_TOKEN", "").strip(),
        max_file_mb=_int("MAX_FILE_MB", 10, 1, 25),
        max_files=_int("MAX_FILES", 6, 1, 10),
        max_pages_per_document=_int("MAX_PAGES_PER_DOCUMENT", 3, 1, 6),
        rate_limit_per_10_min=_int("RATE_LIMIT_PER_10_MIN", 20, 1, 1000),
    )
