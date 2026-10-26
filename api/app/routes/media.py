import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.deps import Caller, current_caller
from app.logging_conf import log_event
from app.models import Job, Media
from app.schemas import MediaIn, MediaOut
from app.screening.prompts import screen_prompt
from app.workq import fairqueue

router = APIRouter()
log = logging.getLogger("routes.media")


@router.post("/media", status_code=202)
async def create_media(
    payload: MediaIn,
    caller: Caller = Depends(current_caller),
    session: AsyncSession = Depends(get_session),
):
    """Accept a media item and queue it for screening.

    Returns 202 with status "pending": nothing is published at creation time, so
    an item is never live before a decision exists (rule 5).
    """
    # Rule 4: a generation prompt is screened before anything is generated or
    # scored. The refusal happens here, in the request, so no media row is
    # created and no provider call is ever made.
    if payload.source == "generation" and not settings.broken:
        refused = screen_prompt(payload.prompt)
        if refused:
            log_event(
                log,
                "generation_prompt_refused",
                level=logging.WARNING,
                workspace_id=caller.workspace_id,
                reasons=refused,
            )
            return JSONResponse(
                status_code=422,
                content={"reasons": [f"prompt_refused:{category}" for category in refused]},
            )
    elif payload.source == "generation" and settings.broken:
        # INCIDENT 3: in broken mode the prompt is never looked at, which is how
        # a request for a 16-year-old reached the generator.
        log_event(
            log,
            "generation_prompt_unchecked",
            level=logging.ERROR,
            workspace_id=caller.workspace_id,
        )

    media = Media(
        workspace_id=caller.workspace_id,
        created_by=caller.user_id,
        type=payload.type,
        source=payload.source,
        prompt=payload.prompt,
        frames=[dict(frame) for frame in payload.frames],
        status="pending",
        reasons=[],
    )
    session.add(media)
    await session.flush()

    job = Job(media_id=media.id, workspace_id=caller.workspace_id, state="queued")
    session.add(job)
    await session.commit()

    await fairqueue.enqueue(caller.workspace_id, job.id)

    log_event(
        log,
        "media_accepted",
        workspace_id=caller.workspace_id,
        media_id=media.id,
        job_id=job.id,
        type=payload.type,
        source=payload.source,
        frames=len(payload.frames),
    )
    return {"id": media.id, "status": "pending"}


@router.get("/media/{media_id}")
async def get_media(
    media_id: str,
    caller: Caller = Depends(current_caller),
    session: AsyncSession = Depends(get_session),
) -> MediaOut:
    """Rule 1: a workspace sees only its own media.

    Anyone else gets a 404, not a 403, so the response does not confirm that the
    item exists in some other workspace.
    """
    media = (
        await session.execute(
            select(Media).where(Media.id == media_id, Media.workspace_id == caller.workspace_id)
        )
    ).scalar_one_or_none()

    if media is None:
        raise HTTPException(status_code=404, detail="not found")

    return MediaOut(
        id=media.id,
        status=media.status,
        reasons=list(media.reasons or []),
        type=media.type,
        source=media.source,
        explicit=bool(media.explicit),
        frames_total=len(media.frames or []),
        frames_scored=int(media.frames_scored or 0),
        screened=media.screened_at is not None,
        created_at=media.created_at.isoformat(),
    )
