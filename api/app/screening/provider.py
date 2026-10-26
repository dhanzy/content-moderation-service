"""Client for the moderation provider, with explicit error classification.

The requirement is that timeouts, 429s, 500s and malformed responses are handled
*differently*, so this module's job is to turn whatever happened into one of a
small set of named outcomes that the retry policy can reason about.
"""
import json
from dataclasses import dataclass

import httpx

from app.config import settings

# Error classes, in the order of how much we trust the provider afterwards.
TIMEOUT = "timeout"
RATE_LIMIT = "rate_limit"
SERVER_ERROR = "server_error"
MALFORMED = "malformed"
CLIENT_ERROR = "client_error"
TRANSPORT = "transport"


@dataclass
class ProviderOutcome:
    kind: str  # scored | accepted | error
    frames: list[dict] | None = None
    error_class: str | None = None
    retryable: bool = False
    retry_after: float | None = None
    http_status: int | None = None


# How a given error class is retried. `max_extra` caps retries for classes we
# want to give up on sooner: a malformed body is a provider bug, and hammering it
# costs money without improving our odds.
RETRY_POLICY: dict[str, dict] = {
    TIMEOUT: {"retryable": True, "base_delay": 2.0, "max_attempts": settings.max_attempts},
    SERVER_ERROR: {"retryable": True, "base_delay": 2.0, "max_attempts": settings.max_attempts},
    TRANSPORT: {"retryable": True, "base_delay": 2.0, "max_attempts": settings.max_attempts},
    # 429 is the provider asking us to slow down, not a failure. Back off harder
    # and for longer, and honour Retry-After when it sends one.
    RATE_LIMIT: {"retryable": True, "base_delay": 5.0, "max_attempts": settings.max_attempts + 3},
    # A body we cannot parse is never treated as a pass. One retry, then hold.
    MALFORMED: {"retryable": True, "base_delay": 1.0, "max_attempts": 2},
    # A 4xx that is not a 429 means we sent something wrong. Retrying will not fix it.
    CLIENT_ERROR: {"retryable": False, "base_delay": 0.0, "max_attempts": 1},
}


def backoff_delay(error_class: str, attempt_no: int, retry_after: float | None = None) -> float:
    policy = RETRY_POLICY.get(error_class, RETRY_POLICY[SERVER_ERROR])
    if retry_after:
        return max(float(retry_after), policy["base_delay"])
    # Exponential, capped. No jitter here because the dispatcher already spreads
    # work across workspaces; a real deployment would add jitter.
    return min(policy["base_delay"] * (2 ** max(attempt_no - 1, 0)), 60.0)


def should_retry(error_class: str, attempts_so_far: int) -> bool:
    policy = RETRY_POLICY.get(error_class, RETRY_POLICY[SERVER_ERROR])
    return bool(policy["retryable"]) and attempts_so_far < policy["max_attempts"]


async def score_media(media_id: str, frames: list[dict], idempotency_key: str) -> ProviderOutcome:
    """Ask the provider to score a media item.

    `idempotency_key` is sent so that a provider which supports it can dedupe a
    call we had to repeat after a crash. Our fake provider counts every call
    regardless, which is what makes the duplicate-spend problem visible.
    """
    url = f"{settings.provider_url}/score"
    payload = {"media_id": media_id, "frames": frames}
    headers = {"Idempotency-Key": idempotency_key}

    try:
        async with httpx.AsyncClient(timeout=settings.provider_timeout_s) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException:
        return ProviderOutcome(kind="error", error_class=TIMEOUT, retryable=True)
    except httpx.HTTPError:
        return ProviderOutcome(kind="error", error_class=TRANSPORT, retryable=True)

    status = response.status_code

    if status == 202:
        return ProviderOutcome(kind="accepted", http_status=status)

    if status == 429:
        retry_after = response.headers.get("Retry-After")
        return ProviderOutcome(
            kind="error",
            error_class=RATE_LIMIT,
            retryable=True,
            retry_after=float(retry_after) if retry_after else None,
            http_status=status,
        )

    if status >= 500:
        return ProviderOutcome(kind="error", error_class=SERVER_ERROR, retryable=True, http_status=status)

    if status >= 400:
        return ProviderOutcome(kind="error", error_class=CLIENT_ERROR, retryable=False, http_status=status)

    # 2xx: the body still has to make sense. An unparseable or shape-wrong body
    # is an error, never an empty-and-therefore-safe result.
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        return ProviderOutcome(kind="error", error_class=MALFORMED, retryable=True, http_status=status)

    frames_out = body.get("frames") if isinstance(body, dict) else None
    if not isinstance(frames_out, list) or not all(isinstance(f, dict) for f in frames_out):
        return ProviderOutcome(kind="error", error_class=MALFORMED, retryable=True, http_status=status)
    if len(frames_out) != len(frames):
        # A short result would silently hide the frames we never saw scored.
        return ProviderOutcome(kind="error", error_class=MALFORMED, retryable=True, http_status=status)

    return ProviderOutcome(kind="scored", frames=frames_out, http_status=status)


async def set_mode(mode: str, delay_ms: int = 0) -> None:
    """Test helper: drive the fake provider's behaviour."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(
            f"{settings.provider_url}/_control", json={"mode": mode, "delay_ms": delay_ms}
        )


async def get_stats() -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{settings.provider_url}/_stats")
    return response.json()
