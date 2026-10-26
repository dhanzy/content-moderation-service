import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.deps import Caller, require_admin
from app.logging_conf import log_event
from app.models import Media, ReviewAction, now
from app.screening.apply import cached_result
from app.screening.rules import BLOCK_THRESHOLD, EXPLICIT_THRESHOLD, PROHIBITED_LABELS, peak_scores

router = APIRouter()
log = logging.getLogger("routes.review")


class ReviewQueueUnavailable(Exception):
    pass


async def _held_items(session: AsyncSession, workspace_id: str) -> list[dict]:
    # Fault injection so the three UI states can be tested for real rather than
    # mocked. See tests/test_review_states.py.
    if settings.review_fail:
        raise ReviewQueueUnavailable("injected review-queue failure")

    rows = (
        await session.execute(
            select(Media)
            .where(Media.workspace_id == workspace_id, Media.status == "held")
            .order_by(Media.created_at)
        )
    ).scalars().all()

    return [
        {
            "id": m.id,
            "status": m.status,
            "reasons": list(m.reasons or []),
            "type": m.type,
            "source": m.source,
            "prompt": m.prompt,
            "explicit": bool(m.explicit),
            "frames_total": len(m.frames or []),
            "frames_scored": int(m.frames_scored or 0),
            "screened": m.screened_at is not None,
            "created_at": m.created_at.isoformat(),
        }
        for m in rows
    ]


@router.get("/review")
async def review_queue(
    caller: Caller = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """The held media of the caller's workspace.

    A failure here must surface as a failure. Returning an empty list when the
    query broke is what showed moderators "Nothing to review" for a morning while
    40 items waited, so the fixed path answers 503 and lets the UI say so.
    """
    if settings.broken:
        # INCIDENT 5: swallow the error and answer with an empty list, which is
        # indistinguishable from "nothing is waiting".
        try:
            return await _held_items(session, caller.workspace_id)
        except Exception:
            log_event(
                log,
                "review_failure_shown_as_empty",
                level=logging.ERROR,
                workspace_id=caller.workspace_id,
            )
            return []

    try:
        return await _held_items(session, caller.workspace_id)
    except ReviewQueueUnavailable:
        log_event(
            log,
            "review_queue_unavailable",
            level=logging.ERROR,
            workspace_id=caller.workspace_id,
        )
        return JSONResponse(
            status_code=503,
            content={"error": "review_queue_unavailable"},
        )


async def _load_held(session: AsyncSession, media_id: str, workspace_id: str) -> Media:
    media = (
        await session.execute(
            select(Media).where(Media.id == media_id, Media.workspace_id == workspace_id)
        )
    ).scalar_one_or_none()
    if media is None:
        raise HTTPException(status_code=404, detail="not found")
    if media.status != "held":
        raise HTTPException(status_code=409, detail=f"media is {media.status}, not held")
    return media


@router.post("/review/{media_id}/approve")
async def approve(
    media_id: str,
    caller: Caller = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Publish a held item.

    An admin may clear a borderline score, but may not override the absolute
    rules: an item carrying a prohibited label at 0.8 or above, or explicit
    content in an unverified workspace, stays blocked whatever the admin clicks.
    """
    media = await _load_held(session, media_id, caller.workspace_id)

    result = await cached_result(session, media.id)

    # Two different reasons an item can be held, and only one of them is a
    # judgement call. A borderline score (0.5 to 0.8) has a provider result
    # behind it and a human decides. An item held because screening was
    # unavailable has no result at all -- there is nothing to judge, and
    # publishing it would be publishing unchecked, which rule 5 forbids whatever
    # an admin clicks. The re-screening sweep will deal with it once the provider
    # is back.
    if result is None:
        log_event(
            log,
            "approval_refused_never_screened",
            level=logging.WARNING,
            workspace_id=caller.workspace_id,
            media_id=media.id,
            user_id=caller.user_id,
        )
        raise HTTPException(
            status_code=409,
            detail={"reasons": ["never_screened"], "status": media.status},
        )

    if result is not None:
        peaks = peak_scores(result.frames)
        hard = [l for l in PROHIBITED_LABELS if peaks.get(l, 0.0) >= BLOCK_THRESHOLD]
        explicit = peaks.get("explicit", 0.0) >= EXPLICIT_THRESHOLD
        if hard or (explicit and not caller.workspace_verified):
            reasons = [f"prohibited:{l}" for l in sorted(hard)]
            if explicit and not caller.workspace_verified:
                reasons.append("explicit_in_unverified_workspace")
            media.status = "blocked"
            media.reasons = reasons
            media.explicit = explicit
            await session.commit()
            log_event(
                log,
                "approval_refused_absolute_rule",
                level=logging.WARNING,
                workspace_id=caller.workspace_id,
                media_id=media.id,
                reasons=reasons,
            )
            raise HTTPException(status_code=409, detail={"reasons": reasons})

    media.status = "published"
    media.reasons = []
    session.add(ReviewAction(media_id=media.id, user_id=caller.user_id, action="approve", at=now()))
    await session.commit()
    log_event(
        log,
        "review_approved",
        workspace_id=caller.workspace_id,
        media_id=media.id,
        user_id=caller.user_id,
    )
    return {"id": media.id, "status": media.status}


@router.post("/review/{media_id}/reject")
async def reject(
    media_id: str,
    caller: Caller = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    media = await _load_held(session, media_id, caller.workspace_id)
    media.status = "blocked"
    media.reasons = list(media.reasons or []) + ["rejected_by_admin"]
    session.add(ReviewAction(media_id=media.id, user_id=caller.user_id, action="reject", at=now()))
    await session.commit()
    log_event(
        log,
        "review_rejected",
        workspace_id=caller.workspace_id,
        media_id=media.id,
        user_id=caller.user_id,
    )
    return {"id": media.id, "status": media.status}
