"""Fake moderation provider.

Stands in for whatever classifies content in production. It does not look at
pixels: it echoes back the scores already present on each frame, filling in 0
for labels the frame left out. It counts every call it receives per media ID,
because that count is what the invoice is built from.
"""
import asyncio
import hashlib
import hmac
import json
import os
import time
from collections import defaultdict

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

LABELS = (
    "explicit",
    "minor",
    "sexual_violence",
    "bestiality",
    "self_harm",
    "real_person",
)

CALLBACK_URL = os.environ.get("CALLBACK_URL", "http://api:8000/provider/callback")
SECRET = os.environ.get("PROVIDER_SECRET", "dev-provider-secret").encode()
ASYNC_FRAME_THRESHOLD = int(os.environ.get("ASYNC_FRAME_THRESHOLD", "30"))

app = FastAPI(title="fake-moderation-provider")

# Call counter per media ID. Every call lands here, including the ones we then
# fail, so that _stats shows what a real invoice would show.
CALLS: dict[str, int] = defaultdict(int)
STATE = {"mode": "ok", "delay_ms": 0}


def score_frames(frames: list[dict]) -> list[dict]:
    """Echo each frame's scores back, always returning every label."""
    return [{label: float(frame.get(label, 0.0)) for label in LABELS} for frame in frames]


def sign(body: bytes) -> str:
    return hmac.new(SECRET, body, hashlib.sha256).hexdigest()


@app.get("/_health")
async def health() -> dict:
    return {"ok": True, "mode": STATE["mode"]}


@app.post("/_control")
async def control(payload: dict) -> dict:
    """Set the provider's behaviour. {"mode": "ok"|"slow"|"timeout"|"error", "delay_ms": 0}

    rate_limit and malformed are extra modes used by our own tests so the API's
    handling of 429s and unparseable bodies can be exercised too.
    """
    mode = payload.get("mode", "ok")
    allowed = {"ok", "slow", "timeout", "error", "rate_limit", "malformed"}
    if mode not in allowed:
        return JSONResponse({"error": f"unknown mode {mode}"}, status_code=400)
    STATE["mode"] = mode
    STATE["delay_ms"] = int(payload.get("delay_ms", 0) or 0)
    return {"mode": STATE["mode"], "delay_ms": STATE["delay_ms"]}


@app.get("/_stats")
async def stats() -> dict:
    return {"calls": dict(CALLS), "mode": STATE["mode"]}


@app.post("/_stats/reset")
async def stats_reset() -> dict:
    CALLS.clear()
    return {"calls": {}}


async def deliver_callback(media_id: str, frames: list[dict], request_id: str) -> None:
    """Post an async result back to the API, signed over the exact bytes sent."""
    body = json.dumps(
        {
            "media_id": media_id,
            "request_id": request_id,
            "frames": score_frames(frames),
            "scored_at": time.time(),
        },
        separators=(",", ":"),
    ).encode()
    headers = {"X-Signature": sign(body), "Content-Type": "application/json"}
    for attempt in range(5):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(CALLBACK_URL, content=body, headers=headers)
            if resp.status_code < 500:
                return
        except Exception:
            pass
        await asyncio.sleep(0.5 * (attempt + 1))


@app.post("/score")
async def score(request: Request) -> Response:
    payload = await request.json()
    media_id = str(payload.get("media_id", ""))
    frames = payload.get("frames") or []

    # Count first: a call that we then make slow or fail still costs money.
    CALLS[media_id] += 1

    mode = STATE["mode"]
    delay_ms = STATE["delay_ms"]

    if mode == "timeout":
        # Never answer inside any sane client timeout.
        await asyncio.sleep(600)
    if mode == "slow":
        await asyncio.sleep(delay_ms / 1000.0)
    if mode == "error":
        return JSONResponse({"error": "internal provider error"}, status_code=500)
    if mode == "rate_limit":
        return JSONResponse(
            {"error": "too many requests"},
            status_code=429,
            headers={"Retry-After": "1"},
        )
    if mode == "malformed":
        return Response(content=b'{"frames": [ truncated', media_type="application/json")

    if len(frames) > ASYNC_FRAME_THRESHOLD:
        request_id = hashlib.sha256(f"{media_id}:{CALLS[media_id]}".encode()).hexdigest()[:16]
        asyncio.create_task(deliver_callback(media_id, frames, request_id))
        return JSONResponse(
            {"media_id": media_id, "status": "accepted", "request_id": request_id},
            status_code=202,
        )

    return JSONResponse(
        {"media_id": media_id, "frames": score_frames(frames), "scored_at": time.time()},
        status_code=200,
    )


if __name__ == "__main__":
    # So that `uv run main.py` starts the service. In compose we call uvicorn
    # directly instead, which is why the app object above is the real entry point.
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8001")),
        log_level="info",
    )
