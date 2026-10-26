"""Worker process: a fair dispatcher, plus a reconciler that heals the queue.

Three kinds of loop run here.

consumer()    pops the next job (rotating across workspaces) and screens it.
reconciler()  promotes jobs whose backoff elapsed, and re-enqueues jobs that
              Postgres says are runnable but Redis has lost -- a crashed worker's
              lease, or a flushed Redis. Postgres is the source of truth.
rescreen()    once the provider is healthy again, re-queues everything that was
              held purely because screening was unavailable, so an outage does
              not leave a pile of work waiting on a human.
"""
import asyncio
import logging
import signal

import httpx
from sqlalchemy import select, text

from app.config import settings
from app.db import Session, init_db
from app.logging_conf import log_event, setup_logging
from app.models import Media, now
from app.workq import fairqueue

log = logging.getLogger("workq.worker")

_stopping = asyncio.Event()


def get_handler():
    """Pick the screening handler. Broken mode is loud on purpose."""
    if settings.broken:
        from app.screening import broken

        log_event(
            log,
            "broken_mode_active",
            level=logging.ERROR,
            note="SCREENING_MODE=broken: reproducing the incident, do not run this for real",
        )
        return broken.handle
    from app.screening import fixed

    return fixed.handle


async def provider_healthy() -> bool:
    """Cheap health probe before spending calls on re-screening.

    In production this would be a circuit breaker with half-open probes rather
    than a status endpoint the provider volunteers; noted in the README.
    """
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{settings.provider_url}/_health")
        return response.status_code == 200 and response.json().get("mode") == "ok"
    except Exception:
        return False


async def consumer(index: int) -> None:
    handler = get_handler()
    while not _stopping.is_set():
        try:
            item = await fairqueue.dequeue(timeout_s=1.0)
            if item is None:
                continue
            workspace_id, job_id = item
            outcome = await handler(job_id)
            log_event(
                log,
                "job_handled",
                workspace_id=workspace_id,
                job_id=job_id,
                outcome=outcome,
                consumer=index,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("consumer loop error", extra={"event": "consumer_error"})
            await asyncio.sleep(0.5)


# Jobs Postgres believes are runnable but which no longer appear in Redis.
ORPHAN_SQL = text(
    """
    SELECT id, workspace_id FROM jobs
     WHERE next_attempt_at <= :nowts
       AND (
             state = 'queued'
             OR (state IN ('running', 'awaiting_callback')
                 AND (lease_expires_at IS NULL OR lease_expires_at < :nowts))
           )
     ORDER BY created_at
     LIMIT 200
    """
)


async def reconciler() -> None:
    while not _stopping.is_set():
        try:
            moved = await fairqueue.promote_due()
            if moved:
                log_event(log, "delayed_promoted", count=moved)

            async with Session() as session:
                rows = (await session.execute(ORPHAN_SQL, {"nowts": now()})).all()
            # Re-enqueueing something already in Redis is harmless: the lease
            # check turns the extra delivery into a dropped duplicate.
            for row in rows:
                await fairqueue.enqueue(row.workspace_id, row.id)
            if rows:
                log_event(log, "orphans_requeued", count=len(rows))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reconciler error", extra={"event": "reconciler_error"})
        await asyncio.sleep(2.0)


async def rescreen() -> None:
    """Re-screen outage casualties once the provider is back."""
    while not _stopping.is_set():
        await asyncio.sleep(3.0)
        try:
            if not await provider_healthy():
                continue
            async with Session() as session:
                stmt = (
                    select(Media)
                    .where(Media.status == "held", Media.screened_at.is_(None))
                    .limit(500)
                )
                candidates = (await session.execute(stmt)).scalars().all()
                targets = [
                    m
                    for m in candidates
                    if any(str(r).startswith("screening_unavailable") for r in (m.reasons or []))
                ]
                if not targets:
                    continue
                # Reset the attempt budget: these failed because the provider was
                # down, not because the item is undecidable.
                await session.execute(
                    text(
                        """
                        UPDATE jobs
                           SET state = 'queued', attempts = 0, next_attempt_at = :nowts,
                               lease_token = NULL, lease_expires_at = NULL, last_error = NULL
                         WHERE media_id = ANY(:ids)
                        """
                    ),
                    {"nowts": now(), "ids": [m.id for m in targets]},
                )
                await session.commit()
                job_rows = (
                    await session.execute(
                        text("SELECT id, workspace_id FROM jobs WHERE media_id = ANY(:ids)"),
                        {"ids": [m.id for m in targets]},
                    )
                ).all()
            for row in job_rows:
                await fairqueue.enqueue(row.workspace_id, row.id)
            log_event(log, "outage_rescreen_queued", count=len(job_rows))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("rescreen error", extra={"event": "rescreen_error"})


async def main() -> None:
    setup_logging()
    await init_db()
    log_event(log, "worker_start", mode=settings.screening_mode, concurrency=settings.worker_concurrency)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stopping.set)

    tasks = [asyncio.create_task(consumer(i)) for i in range(settings.worker_concurrency)]
    tasks.append(asyncio.create_task(reconciler()))
    tasks.append(asyncio.create_task(rescreen()))

    await _stopping.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    log_event(log, "worker_stop")


if __name__ == "__main__":
    asyncio.run(main())
