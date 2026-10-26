"""Turning a provider answer into a stored decision.

Split out of the handlers because three callers need it: the sync path, the
async callback path, and the re-screening sweep after an outage.
"""
import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_conf import log_event
from app.models import Media, ProviderResult, ReviewAction, Workspace, now
from app.screening.rules import Decision, decide

log = logging.getLogger("screening.apply")


async def store_provider_result(session: AsyncSession, media_id: str, frames: list[dict], via: str) -> bool:
    """Record the provider's answer. Returns False if one was already stored.

    The media ID is the primary key, so this is the enforcement point for rule 8:
    a second successful score for the same media cannot be recorded, and callers
    that find a row here must not pay for another call.
    """
    stmt = (
        pg_insert(ProviderResult)
        .values(media_id=media_id, frames=frames, via=via, received_at=now())
        .on_conflict_do_nothing(index_elements=["media_id"])
    )
    result = await session.execute(stmt)
    return bool(result.rowcount)


async def cached_result(session: AsyncSession, media_id: str) -> ProviderResult | None:
    return (
        await session.execute(select(ProviderResult).where(ProviderResult.media_id == media_id))
    ).scalar_one_or_none()


async def human_has_ruled(session: AsyncSession, media_id: str) -> bool:
    """True once an admin has approved or rejected this item.

    A late provider callback must not quietly overturn a human decision.
    """
    found = (
        await session.execute(select(ReviewAction.id).where(ReviewAction.media_id == media_id).limit(1))
    ).first()
    return found is not None


async def apply_decision(
    session: AsyncSession, media: Media, decision: Decision, *, screened: bool
) -> None:
    """Write a verdict onto a media item.

    `screened` records whether this verdict came from a real provider answer.
    `screened_at` is therefore the only trustworthy answer to "was this item
    actually checked", and the unchecked-publish metric is built from it.
    """
    media.status = decision.status
    media.explicit = decision.explicit
    media.reasons = decision.reasons
    if screened:
        media.screened_at = now()

    log_event(
        log,
        "decision_applied",
        workspace_id=media.workspace_id,
        media_id=media.id,
        status=decision.status,
        explicit=decision.explicit,
        reasons=decision.reasons,
        screened=screened,
        frames_scored=media.frames_scored,
    )


async def apply_scored_frames(
    session: AsyncSession, media: Media, frames: list[dict], via: str
) -> Decision | None:
    """Store a provider answer and apply the verdict it implies.

    Returns None when the item should be left alone (a human already ruled).
    """
    if await human_has_ruled(session, media.id):
        log_event(
            log,
            "decision_skipped_human_ruled",
            workspace_id=media.workspace_id,
            media_id=media.id,
        )
        return None

    await store_provider_result(session, media.id, frames, via)

    workspace = await session.get(Workspace, media.workspace_id)
    verified = bool(workspace and workspace.verified)

    media.frames_scored = len(frames)
    decision = decide(frames, verified)
    await apply_decision(session, media, decision, screened=True)
    return decision
