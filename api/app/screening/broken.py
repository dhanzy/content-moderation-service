"""The deliberately broken handler that reproduces last Tuesday's incident.

This is the pre-fix behaviour, kept in the tree so the failure can be reproduced
on demand rather than described. Run the stack with SCREENING_MODE=broken and the
regression suite in tests/test_incident_broken.py will show each symptom.

The five faults, each marked INCIDENT below:

  1. a provider timeout is treated as a pass, so the item is published unchecked
  2. only the first 8 frames of a video are scored, so a video that turns
     explicit in its last minute looks clean
  3. generation prompts are never screened (see routes/media.py)
  4. no lease is taken, so a redelivered job runs alongside the attempt already
     in flight -- and because nothing is cached, both pay the provider
  5. a failed review-queue request is rendered as an empty list
     (see routes/review.py and web/src/App.tsx)

Nothing in here should be copied. It exists to be red in the test output.
"""
import logging

from app.db import Session
from app.logging_conf import log_event
from app.models import Job, JobAttempt, Media, Workspace, now
from app.screening import provider as prov
from app.screening.apply import apply_decision
from app.screening.rules import decide
from app.workq import fairqueue

log = logging.getLogger("screening.broken")

# INCIDENT 2: a magic prefix. Someone capped the frame list while debugging a
# slow provider and never took the cap back out.
BROKEN_FRAME_LIMIT = 8


async def handle(job_id: str) -> str:
    async with Session() as session:
        job = await session.get(Job, job_id)
        if job is None:
            return "missing"

        # INCIDENT 4: no claim, no lease, no attempt counter check. A redelivery
        # of the same job starts a second run while the first is still talking to
        # the provider, which is how the invoice ended up with two and three
        # calls for one upload.
        job.state = "running"
        job.attempts = (job.attempts or 0) + 1
        attempt_no = job.attempts
        await session.commit()

        media = await session.get(Media, job.media_id)
        if media is None:
            return "missing"

        workspace = await session.get(Workspace, media.workspace_id)
        verified = bool(workspace and workspace.verified)

        session.add(
            JobAttempt(
                job_id=job.id,
                media_id=media.id,
                workspace_id=job.workspace_id,
                attempt_no=attempt_no,
                state="in_flight",
            )
        )
        await session.commit()

        # INCIDENT 2: only the first 8 frames are sent for scoring.
        frames_sent = (media.frames or [])[:BROKEN_FRAME_LIMIT]

        outcome = await prov.score_media(media.id, frames_sent, idempotency_key=media.id)

        if outcome.kind == "scored":
            media.frames_scored = len(outcome.frames or [])
            decision = decide(outcome.frames or [], verified)
            await apply_decision(session, media, decision, screened=True)
            job.state = "done"
            job.finished_at = now()
            await session.commit()
            return "scored"

        if outcome.kind == "accepted":
            job.state = "awaiting_callback"
            await session.commit()
            await fairqueue.enqueue_delayed(job.workspace_id, job.id, 60.0)
            return "accepted"

        # INCIDENT 1: a timeout is read as "nothing objectionable came back", so
        # the item is published without ever having been screened. This is the
        # single line that let 40 minutes of uploads go live during the outage.
        if outcome.error_class == prov.TIMEOUT:
            media.status = "published"
            media.explicit = False
            media.reasons = []
            # Note what is *not* set: screened_at. The job says done, the media
            # says published, and nothing was ever checked.
            job.state = "done"
            job.finished_at = now()
            await session.commit()
            log_event(
                log,
                "timeout_treated_as_pass",
                level=logging.ERROR,
                workspace_id=job.workspace_id,
                media_id=media.id,
                job_id=job.id,
                attempt_no=attempt_no,
            )
            return "published_on_timeout"

        # Other failures retry forever with no cap and no result caching, so each
        # pass buys another provider call.
        await fairqueue.enqueue_delayed(job.workspace_id, job.id, 1.0)
        job.state = "queued"
        await session.commit()
        return "retry"
