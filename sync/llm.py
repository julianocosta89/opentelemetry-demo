"""
Thin wrapper around the Anthropic SDK for the one thing this pipeline needs:
turn a prompt into text, reliably, with the current model.

Uses claude-opus-4-8 with adaptive thinking at high effort — source-conflict
resolution is correctness-critical and low-volume, so we buy the strongest model.
Streams (so large files don't trip the SDK's non-streaming timeout guard) and
surfaces a safety refusal as a typed error rather than returning empty text.
"""

from __future__ import annotations

import logging
import time

import anthropic

import config

log = logging.getLogger("sync.llm")

# Transient API failures worth retrying at the application level. The SDK retries
# these too, but its retry of a long streaming request is unreliable under sustained
# overload, so we wrap the whole stream in our own backoff loop.
_RETRYABLE_TYPES = (
    anthropic.RateLimitError,       # 429
    anthropic.InternalServerError,  # 5xx
    anthropic.APIConnectionError,   # network blip / timeout
)


def _is_retryable(exc: Exception) -> bool:
    """Whether an Anthropic error is a transient failure worth retrying.

    Typed 429/5xx/connection errors are obvious. But an overloaded_error (529)
    raised mid-stream as an SSE `error` event surfaces as a bare APIError that
    isn't mapped to InternalServerError — so also match on status code and the
    'overloaded' marker in the payload.
    """
    if isinstance(exc, _RETRYABLE_TYPES):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    text = str(exc).lower()
    return "overloaded" in text or "rate_limit" in text

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        # Resolves ANTHROPIC_API_KEY from the environment. Keep the SDK's per-call
        # retries modest (it respects Retry-After for quick blips); the reliable
        # backoff for sustained overload is the app-level loop in complete().
        _client = anthropic.Anthropic(max_retries=2)
    return _client


class LLMError(RuntimeError):
    pass


class LLMRefusal(LLMError):
    """The model declined the request (stop_reason == 'refusal')."""


def complete(system: str, user: str, max_tokens: int = 32000) -> str:
    """Run one completion and return the concatenated text blocks.

    Retries transient API failures (429/5xx/529-overloaded/connection) with
    exponential backoff so a momentary overload doesn't abort a file's resolution.
    """
    attempts = config.LLM_API_MAX_RETRIES + 1
    for attempt in range(1, attempts + 1):
        try:
            with _get_client().messages.stream(
                model=config.MODEL,
                max_tokens=max_tokens,
                system=system,
                thinking={"type": "adaptive"},
                output_config={"effort": config.LLM_EFFORT},
                messages=[{"role": "user", "content": user}],
            ) as stream:
                message = stream.get_final_message()
            break
        except anthropic.APIError as exc:
            if not _is_retryable(exc):  # non-retryable 4xx
                raise LLMError(f"Anthropic API error: {exc}") from exc
            if attempt == attempts:
                raise LLMError(f"Anthropic API error after {attempts} attempts: {exc}") from exc
            delay = min(2.0 * 2 ** (attempt - 1), 60.0)
            log.warning("Transient API error (attempt %d/%d), retrying in %.0fs: %s",
                        attempt, attempts, delay, exc)
            time.sleep(delay)

    if message.stop_reason == "refusal":
        raise LLMRefusal("model refused to resolve this conflict")

    return "".join(b.text for b in message.content if b.type == "text")
