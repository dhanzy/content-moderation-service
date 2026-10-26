"""Async provider results.

Rule 7: a callback without a valid signature is rejected. The HMAC is computed
over the raw request body, so the bytes are read before any parsing and the
signature is checked before anything in the payload is trusted or acted on.
"""
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.logging_conf import log_event
from app.models import Media, now
from app.screening.apply import apply_decision, apply_scored_frames, cached_result
from app.screening.rules import held_for_no_decision

router = APIRouter()
log = logging.getLogger("routes.callback")


def signature_valid(raw_body: bytes, provided: str | None) -> bool:
    if not provided:
        return False
    # Accept a bare hex digest or a "sha256=<hex>" form.
    candidate = provided.split("=", 1)[1] if provided.startswith("sha256=") else provided
    expected = hmac.new(settings.provider_secret, raw_body, hashlib.sha256).hexdigest()
    # Constant-time comparison: a timing-sensitive compare would leak the digest
    # one byte at a time to anyone able to call this endpoint repeatedly.
    return hmac.compare_digest(expected, candidate.strip())


@router.post("/provider/callback")
async def provider_callback(
    request: Request,
    x_signature: str | None = Header(default=None, alias="X-Signature"),
    session: AsyncSession = Depends(get_session),
):
    raw = await request.body()

    if not signature_valid(raw, x_signature):
        log_event(
            log,
            "callback_rejected_bad_signature",
            level=logging.WARNING,
            body_bytes=len(raw),
            had_header=bool(x_signature),
        )
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="unparseable body")

    media_id = str(payload.get("media_id") or "")
    frames = payload.get("frames")

    media = (await session.execute(select(Media).where(Media.id == media_id))).scalar_one_or_none()
    if media is None:
        # Signed but unknown: acknowledge so the provider stops retrying, and say
        # so in the logs rather than failing silently.
        log_event(log, "callback_unknown_media", level=logging.WARNING, media_id=media_id)
        return {"status": "ignored", "reason": "unknown_media"}

    # Rule 8: a result already stored is not replaced and not re-applied, so a
    # provider that delivers the same callback twice cannot double-count.
    if await cached_result(session, media.id) is not None:
        log_event(
            log,
            "callback_duplicate_ignored",
            workspace_id=media.workspace_id,
            media_id=media.id,
        )
        return {"status": "already_applied", "id": media.id}

    if not isinstance(frames, list) or not all(isinstance(f, dict) for f in frames):
        log_event(
            log,
            "callback_malformed_frames",
            level=logging.ERROR,
            workspace_id=media.workspace_id,
            media_id=media.id,
        )
        await apply_decision(session, media, held_for_no_decision("malformed_callback"), screened=False)
        await session.commit()
        return {"status": "held", "id": media.id, "reason": "malformed_frames"}

    # Rule 9 again, from the other direction: a callback covering fewer frames
    # than the item has is not a clean pass. Hold it rather than publish on the
    # strength of a partial result.
    if len(frames) != len(media.frames or []):
        log_event(
            log,
            "callback_frame_count_mismatch",
            level=logging.ERROR,
            workspace_id=media.workspace_id,
            media_id=media.id,
            expected=len(media.frames or []),
            received=len(frames),
        )
        await apply_decision(session, media, held_for_no_decision("partial_callback"), screened=False)
        await session.commit()
        return {"status": "held", "id": media.id, "reason": "frame_count_mismatch"}

    decision = await apply_scored_frames(session, media, frames, via="callback")
    # The job this result belongs to is finished regardless of the verdict.
    await session.execute(
        text(
            "UPDATE jobs SET state = 'done', finished_at = :nowts, "
            "lease_token = NULL, lease_expires_at = NULL WHERE media_id = :media_id"
        ),
        {"nowts": now(), "media_id": media.id},
    )
    await session.commit()

    log_event(
        log,
        "callback_applied",
        workspace_id=media.workspace_id,
        media_id=media.id,
        status=media.status,
        frames=len(frames),
    )
    return {"status": media.status, "id": media.id, "skipped": decision is None}
