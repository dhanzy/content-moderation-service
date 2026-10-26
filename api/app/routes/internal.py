"""Operational endpoints: the incident metrics, and a reset for the load harness.

The three numbers the brief asks to compare before and after the fix all come
from here, so the same query produces both sides of the table in the README.
"""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.logging_conf import log_event
from app.screening import provider as prov
from app.workq import fairqueue

router = APIRouter()
log = logging.getLogger("routes.internal")


@router.get("/_metrics")
async def metrics(session: AsyncSession = Depends(get_session)) -> dict:
    """Counts, the unchecked-publish number, and queue age.

    `published_unchecked` is the headline safety metric: media that is live with
    no provider decision behind it. It is derived from screened_at, not from job
    state, precisely because a finished job is not proof that anything was
    screened -- that conflation is what hid the incident.
    """
    by_status = {
        row.status: row.n
        for row in (
            await session.execute(text("SELECT status, COUNT(*) AS n FROM media GROUP BY status"))
        ).all()
    }

    unchecked = (
        await session.execute(
            text(
                "SELECT COUNT(*) FROM media WHERE status = 'published' AND screened_at IS NULL"
            )
        )
    ).scalar_one()

    # Items that are live and explicit while their workspace is unverified. This
    # must always be zero; it is the specific breach the incident produced.
    explicit_unverified_live = (
        await session.execute(
            text(
                """
                SELECT COUNT(*) FROM media m
                  JOIN workspaces w ON w.id = m.workspace_id
                 WHERE m.status = 'published' AND m.explicit IS TRUE AND w.verified IS FALSE
                """
            )
        )
    ).scalar_one()

    oldest = (
        await session.execute(
            text(
                """
                SELECT COALESCE(
                         MAX(EXTRACT(EPOCH FROM (NOW() - created_at))), 0
                       ) AS age
                  FROM jobs
                 WHERE state IN ('queued', 'running', 'awaiting_callback')
                """
            )
        )
    ).scalar_one()

    job_states = {
        row.state: row.n
        for row in (
            await session.execute(text("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state"))
        ).all()
    }

    attempts = (
        await session.execute(
            text("SELECT state, COUNT(*) AS n FROM job_attempts GROUP BY state")
        )
    ).all()

    media_total = sum(by_status.values())
    results_stored = (
        await session.execute(text("SELECT COUNT(*) FROM provider_results"))
    ).scalar_one()

    # Provider-side truth: what the invoice would say.
    try:
        stats = await prov.get_stats()
        calls = stats.get("calls", {}) or {}
        total_calls = sum(int(v) for v in calls.values())
        duplicated = sum(1 for v in calls.values() if int(v) > 1)
    except Exception:
        total_calls, duplicated, calls = -1, -1, {}

    return {
        "media_total": media_total,
        "media_by_status": by_status,
        "published_unchecked": int(unchecked),
        "explicit_published_in_unverified": int(explicit_unverified_live),
        "oldest_waiting_job_age_s": round(float(oldest or 0.0), 2),
        "job_states": job_states,
        "attempt_states": {row.state: row.n for row in attempts},
        "provider_results_stored": int(results_stored),
        "provider_calls_total": total_calls,
        "media_with_duplicate_calls": duplicated,
        "provider_calls_per_upload": (
            round(total_calls / media_total, 3) if media_total and total_calls >= 0 else None
        ),
        "queue_depth": await fairqueue.depth(),
    }


@router.post("/_admin/reset")
async def reset(session: AsyncSession = Depends(get_session)) -> dict:
    """Drop all media and queue state so a workload can be re-run from clean.

    Workspaces and users are left in place. This is a test fixture, not something
    a real deployment would expose.
    """
    await session.execute(
        text(
            "TRUNCATE review_actions, job_attempts, provider_results, jobs, media "
            "RESTART IDENTITY CASCADE"
        )
    )
    await session.commit()
    await fairqueue.clear()
    try:
        import httpx

        from app.config import settings

        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(f"{settings.provider_url}/_stats/reset")
            await client.post(f"{settings.provider_url}/_control", json={"mode": "ok"})
    except Exception:
        log.warning("could not reset provider", extra={"event": "provider_reset_failed"})

    log_event(log, "state_reset")
    return {"status": "reset"}
