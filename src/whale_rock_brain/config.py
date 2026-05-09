"""Runtime configuration loaded from environment / .env file."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
TICKERS_FILE = DATA_DIR / "tickers.json"


class Settings(BaseSettings):
    """Application settings — populated from .env at startup."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    anthropic_api_key: SecretStr = Field(default=SecretStr(""))
    github_token: SecretStr = Field(default=SecretStr(""))
    edgar_contact_email: str = Field(default="research@example.com")

    # X / Twitter v2 API (paid tier required).
    x_bearer_token: SecretStr = Field(default=SecretStr(""))

    model: str = Field(default="claude-sonnet-4-6")
    max_tokens: int = Field(default=2000)
    temperature: float = Field(default=0.3)

    log_level: str = Field(default="INFO")
    log_json: bool = Field(default=False)

    # Per-source ingestion knobs
    reddit_items_per_sub: int = Field(default=8)
    hn_items: int = Field(default=20)
    github_items: int = Field(default=15)
    edgar_items: int = Field(default=10)

    # Connection engine knobs
    connection_min_similarity: float = Field(default=0.18)
    connection_max_pairs: int = Field(default=12)
    connection_judge_threshold: int = Field(default=2)  # min confidence (1=Low, 2=Med, 3=High)


settings = Settings()


def load_tickers() -> dict[str, dict[str, Any]]:
    """Load the ticker metadata file."""
    with TICKERS_FILE.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def get_ticker_meta(ticker: str) -> dict[str, Any]:
    tickers = load_tickers()
    if ticker not in tickers:
        raise ValueError(f"Unknown ticker {ticker!r}. Known: {list(tickers)}")
    return tickers[ticker]
