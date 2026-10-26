"""Job leasing.

A redelivered message must not start a second run of a job that is still
running. That is enforced with a single conditional UPDATE: a job can be claimed
only if it is queued, or if it is running but its lease has expired (meaning the
worker holding it is gone). Postgres serialises the UPDATE, so of two workers
racing on the same job ID exactly one gets a row back and the other is told to
drop the message.
"""
import logging
import uuid
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.logging_conf import log_event
from app.models import Job, now

log = logging.getLogger("workq.lease")

CLAIM_SQL = text(
    """
    UPDATE jobs
       SET state = 'running',
           lease_token = :token,
           lease_expires_at = :expires,
           attempts = attempts + 1
     WHERE id = :job_id
       AND next_attempt_at <= :nowts
       AND (
             state = 'queued'
             OR (
                  -- A running job is only reclaimable once its lease has lapsed,
                  -- which is what stops a redelivery joining an attempt in flight.
                  state IN ('running', 'awaiting_callback')
                  AND (lease_expires_at IS NULL OR lease_expires_at < :nowts)
                )
           )
    RETURNING id, attempts
    """
)


async def claim(session: AsyncSession, job_id: str) -> tuple[str, int] | None:
    """Try to take the lease on a job. Returns (token, attempt_no) or None."""
    token = uuid.uuid4().hex
    expires = now() + timedelta(seconds=settings.lease_seconds)
    row = (
        await session.execute(
            CLAIM_SQL,
            {"token": token, "expires": expires, "job_id": job_id, "nowts": now()},
        )
    ).first()
    await session.commit()
    if row is None:
        return None
    return token, int(row.attempts)


async def extend(session: AsyncSession, job_id: str, token: str) -> bool:
    """Push the lease out while still working. Fails if we no longer hold it."""
    expires = now() + timedelta(seconds=settings.lease_seconds)
    result = await session.execute(
        text(
            "UPDATE jobs SET lease_expires_at = :expires "
            "WHERE id = :job_id AND lease_token = :token"
        ),
        {"expires": expires, "job_id": job_id, "token": token},
    )
    await session.commit()
    return bool(result.rowcount)


async def release(
    session: AsyncSession,
    job: Job,
    token: str,
    *,
    state: str,
    error_class: str | None = None,
    next_attempt_delay_s: float = 0.0,
) -> bool:
    """Finish with a job, but only if we still hold its lease.

    The token check is the fence: a worker whose lease expired while it was
    stalled cannot come back and overwrite the state of the worker that took over.
    """
    from datetime import timedelta as _td

    fields = {
        "state": state,
        "error_class": error_class,
        "job_id": job.id,
        "token": token,
        "next_attempt_at": now() + _td(seconds=next_attempt_delay_s),
        "finished_at": now() if state in ("done", "failed") else None,
    }
    result = await session.execute(
        text(
            """
            UPDATE jobs
               SET state = :state,
                   last_error = :error_class,
                   next_attempt_at = :next_attempt_at,
                   finished_at = :finished_at,
                   lease_token = NULL,
                   lease_expires_at = NULL
             WHERE id = :job_id AND lease_token = :token
            """
        ),
        fields,
    )
    await session.commit()
    held = bool(result.rowcount)
    if not held:
        log_event(
            log,
            "lease_lost_on_release",
            level=logging.WARNING,
            workspace_id=job.workspace_id,
            media_id=job.media_id,
            job_id=job.id,
            intended_state=state,
        )
    return held
