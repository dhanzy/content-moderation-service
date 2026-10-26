"""The real screening handler.

Invariants it is responsible for:
  * rule 5  -- no decision means held, never published
  * rule 8  -- at most one successful score per media item
  * rule 9  -- every frame of a video is scored
  * no two attempts for one job run at the same time (job lease)
"""
import logging
import os

from app.config import settings
from app.db import Session
from app.logging_conf import log_event
from app.models import Job, JobAttempt, Media, now
from app.screening import provider as prov
from app.screening.apply import apply_decision, apply_scored_frames, cached_result
from app.screening.rules import held_for_no_decision
from app.workq import fairqueue
from app.workq.leases import claim, release

log = logging.getLogger("screening.fixed")

# How long we wait for an async (202) result before treating its absence as a
# failure to get a decision. Must exceed the provider's callback latency.
CALLBACK_DEADLINE_S = 60.0


async def handle(job_id: str) -> str:
    """Run one screening attempt. Returns a short outcome tag for the logs."""
    async with Session() as session:
        job = await session.get(Job, job_id)
        if job is None:
            return "missing"

        claimed = await claim(session, job_id)
        if claimed is None:
            # Either another worker holds the lease, or the backoff has not
            # elapsed. Dropping the delivery is correct: the job is still in
            # Postgres and will be picked up by its owner or by the reconciler.
            log_event(
                log,
                "duplicate_delivery_dropped",
                workspace_id=job.workspace_id,
                media_id=job.media_id,
                job_id=job.id,
                job_state=job.state,
            )
            return "not_claimed"

        token, attempt_no = claimed
        media = await session.get(Media, job.media_id)
        if media is None:
            await release(session, job, token, state="failed", error_class="media_missing")
            return "missing"

        # Rule 8. A result we already hold is reused rather than re-bought. This
        # is what keeps provider calls at one per upload across retries, worker
        # restarts and redeliveries.
        existing = await cached_result(session, media.id)
        if existing is not None:
            session.add(
                JobAttempt(
                    job_id=job.id,
                    media_id=media.id,
                    workspace_id=job.workspace_id,
                    attempt_no=attempt_no,
                    state="skipped_cached",
                    finished_at=now(),
                )
            )
            await apply_scored_frames(session, media, existing.frames, existing.via)
            await session.commit()
            await release(session, job, token, state="done")
            log_event(
                log,
                "scored_from_cache",
                workspace_id=job.workspace_id,
                media_id=media.id,
                job_id=job.id,
                attempt_no=attempt_no,
            )
            return "cached"

        # Write the attempt down *before* calling out, so a crash mid-call leaves
        # an in_flight row. That row is the only evidence that money may have been
        # spent without a result landing.
        attempt = JobAttempt(
            job_id=job.id,
            media_id=media.id,
            workspace_id=job.workspace_id,
            attempt_no=attempt_no,
            state="in_flight",
        )
        session.add(attempt)
        await session.commit()

        log_event(
            log,
            "provider_call_started",
            workspace_id=job.workspace_id,
            media_id=media.id,
            job_id=job.id,
            attempt_no=attempt_no,
            frames=len(media.frames),
        )

        # Rule 9: the whole frame list goes to the provider, never a prefix.
        outcome = await prov.score_media(media.id, media.frames, idempotency_key=media.id)

        if outcome.kind == "scored":
            # Fault injection: die here, with the provider already paid and the
            # result still only in memory. See README "worker death".
            if settings.crash_after_score and settings.crash_after_score in ("*", media.id):
                log_event(
                    log,
                    "injected_crash_after_score",
                    level=logging.ERROR,
                    workspace_id=job.workspace_id,
                    media_id=media.id,
                    job_id=job.id,
                    attempt_no=attempt_no,
                )
                os._exit(97)

            attempt.state = "scored"
            attempt.finished_at = now()
            await apply_scored_frames(session, media, outcome.frames or [], via="sync")
            await session.commit()
            await release(session, job, token, state="done")
            return "scored"

        if outcome.kind == "accepted":
            # Long video: the provider will post the result to our callback. The
            # media stays pending and the job is parked with a deadline, so a
            # callback that never arrives still ends in a held item, not a
            # published one.
            attempt.state = "accepted"
            attempt.finished_at = now()
            await session.commit()
            await release(
                session,
                job,
                token,
                state="awaiting_callback",
                next_attempt_delay_s=CALLBACK_DEADLINE_S,
            )
            await fairqueue.enqueue_delayed(job.workspace_id, job.id, CALLBACK_DEADLINE_S + 1)
            log_event(
                log,
                "provider_accepted_async",
                workspace_id=job.workspace_id,
                media_id=media.id,
                job_id=job.id,
                attempt_no=attempt_no,
            )
            return "accepted"

        # Anything else is a failure to get a decision.
        error_class = outcome.error_class or prov.SERVER_ERROR
        attempt.state = "failed"
        attempt.error_class = error_class
        attempt.finished_at = now()
        await session.commit()

        log_event(
            log,
            "provider_call_failed",
            level=logging.WARNING,
            workspace_id=job.workspace_id,
            media_id=media.id,
            job_id=job.id,
            attempt_no=attempt_no,
            error_class=error_class,
            http_status=outcome.http_status,
        )

        if prov.should_retry(error_class, attempt_no):
            delay = prov.backoff_delay(error_class, attempt_no, outcome.retry_after)
            await release(
                session,
                job,
                token,
                state="queued",
                error_class=error_class,
                next_attempt_delay_s=delay,
            )
            await fairqueue.enqueue_delayed(job.workspace_id, job.id, delay)
            log_event(
                log,
                "retry_scheduled",
                workspace_id=job.workspace_id,
                media_id=media.id,
                job_id=job.id,
                attempt_no=attempt_no,
                error_class=error_class,
                delay_s=round(delay, 2),
            )
            return "retry"

        # Rule 5: retries exhausted and still no decision, so the item is held.
        # It is never published unchecked, and screened_at stays null so the
        # unchecked-publish metric cannot be fooled by a finished job.
        await apply_decision(session, media, held_for_no_decision(error_class), screened=False)
        await session.commit()
        await release(session, job, token, state="failed", error_class=error_class)
        log_event(
            log,
            "held_no_decision",
            level=logging.WARNING,
            workspace_id=job.workspace_id,
            media_id=media.id,
            job_id=job.id,
            error_class=error_class,
        )
        return "held"
