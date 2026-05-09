"""Structured logging + per-call cost tracking."""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator

import structlog

from .config import settings
from .schemas import CostRecord, RunMetrics


# Sonnet 4.6 list pricing as of Jan 2026: $3 / 1M input, $15 / 1M output.
# Source: Anthropic pricing page. We track at this rate; cache rates not modeled.
SONNET_INPUT_USD_PER_MTOK = 3.0
SONNET_OUTPUT_USD_PER_MTOK = 15.0


def _configure_logging() -> None:
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(level=log_level, format="%(message)s")
    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    if settings.log_json:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        cache_logger_on_first_use=True,
    )


_configure_logging()
log = structlog.get_logger()


def compute_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return round(
        (input_tokens / 1_000_000) * SONNET_INPUT_USD_PER_MTOK
        + (output_tokens / 1_000_000) * SONNET_OUTPUT_USD_PER_MTOK,
        6,
    )


def record_call(
    metrics: RunMetrics,
    *,
    label: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    duration_seconds: float,
) -> CostRecord:
    cost = compute_cost_usd(input_tokens, output_tokens)
    rec = CostRecord(
        label=label,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        duration_seconds=round(duration_seconds, 3),
    )
    metrics.calls.append(rec)
    log.info(
        "llm.call",
        label=label,
        model=model,
        input=input_tokens,
        output=output_tokens,
        cost_usd=cost,
        duration_s=round(duration_seconds, 3),
    )
    return rec


@contextmanager
def stopwatch() -> Iterator[dict]:
    start = time.perf_counter()
    holder = {"elapsed": 0.0}
    try:
        yield holder
    finally:
        holder["elapsed"] = time.perf_counter() - start
