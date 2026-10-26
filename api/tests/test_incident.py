"""The five incident symptoms, each asserted twice.

Every test comes in a pair: one showing the broken handler reproducing the
symptom, one showing the fixed handler preventing it. Running the whole file is
the before-and-after evidence, in one place, without having to redeploy.

Symptoms, from the brief:
  1. uploads went live without being checked (provider timeout read as a pass)
  2. a video explicit in its last minute was published in an unverified workspace
     (only the first 8 frames were scored)
  3. a generation prompt asking for a 16-year-old reached the generator
  4. the invoice shows some uploads scored two or three times
  5. moderators saw "Nothing to review" while 40 items were waiting
"""
import asyncio

import pytest
from sqlalchemy import text

from app.config import settings
from app.db import Session
from app.screening import broken, fixed
from tests.conftest import (
    ADMIN_U,
    ADMIN_V,
    MEMBER_U,
    MEMBER_V,
    explicit_at_end,
    job_id_for,
    media_row,
    provider_calls,
    set_provider,
    video,
)


# ---------------------------------------------------------------- symptom 1
# A provider timeout must never be read as "nothing objectionable found".

@pytest.mark.incident
async def test_broken_publishes_unchecked_on_timeout(client):
    response = await client.post("/media", json=video([{"explicit": 0.01}] * 3), headers=MEMBER_V)
    media_id = response.json()["id"]

    await set_provider("timeout")
    assert await broken.handle(await job_id_for(media_id)) == "published_on_timeout"

    row = await media_row(media_id)
    assert row.status == "published", "broken handler should publish on timeout"
    # The damning part: live, with nothing behind it.
    assert row.screened_at is None, "published without ever being screened"


async def test_fixed_holds_from_the_very_first_failure(client):
    """Rule 5 applies immediately, not once the retry budget is spent.

    The brief speaks of "media held because of the outage", so an item we cannot
    get a decision for must read as held straight away. Leaving it pending for
    the minute the retries take would hide it from the review queue and from
    anyone reading its status during an outage.
    """
    response = await client.post("/media", json=video([{"explicit": 0.01}] * 3), headers=MEMBER_V)
    media_id = response.json()["id"]

    await set_provider("timeout")
    assert await fixed.handle(await job_id_for(media_id)) == "retry"

    row = await media_row(media_id)
    assert row.status == "held", f"expected held after one failed attempt, got {row.status}"
    assert any("screening_unavailable" in r for r in row.reasons)
    # Still not screened: a provisional hold must never look like a decision.
    assert row.screened_at is None

    # And it is visible to a moderator rather than invisible in a queue.
    queue = (await client.get("/review", headers=ADMIN_V)).json()
    assert media_id in [item["id"] for item in queue]


async def test_fixed_outage_hold_cannot_be_approved(client):
    """An admin may clear a borderline score, not an item with no score at all.

    This is the hole that holding-on-first-failure would otherwise open: the
    item is held, so it reaches the approve route, but no provider result exists
    and publishing it would be publishing unchecked.
    """
    response = await client.post("/media", json=video([{"explicit": 0.01}] * 3), headers=MEMBER_V)
    media_id = response.json()["id"]

    await set_provider("timeout")
    await fixed.handle(await job_id_for(media_id))
    assert (await media_row(media_id)).status == "held"

    approve = await client.post(f"/review/{media_id}/approve", headers=ADMIN_V)
    assert approve.status_code == 409, approve.text
    assert approve.json()["detail"]["reasons"] == ["never_screened"]

    row = await media_row(media_id)
    assert row.status == "held", "a refused approval must leave it held"
    assert row.screened_at is None


async def test_fixed_outage_hold_resolves_itself_when_the_provider_returns(client):
    """Recovery needs no human: a later attempt overwrites the provisional hold."""
    response = await client.post("/media", json=video([{"explicit": 0.01}] * 3), headers=MEMBER_V)
    media_id = response.json()["id"]

    await set_provider("timeout")
    await fixed.handle(await job_id_for(media_id))
    assert (await media_row(media_id)).status == "held"

    await set_provider("ok")
    assert await _run_now(media_id) == "scored"

    row = await media_row(media_id)
    assert row.status == "published"
    assert row.screened_at is not None
    assert await provider_calls(media_id) == 2, "one failed call, then one that worked"


async def test_fixed_holds_on_timeout(client):
    response = await client.post("/media", json=video([{"explicit": 0.01}] * 3), headers=MEMBER_V)
    media_id = response.json()["id"]

    await set_provider("timeout")
    # Exhaust the retry budget; every attempt times out.
    for _ in range(settings.max_attempts + 1):
        await _run_now(media_id)

    row = await media_row(media_id)
    assert row.status == "held", f"expected held, got {row.status}"
    assert row.screened_at is None
    assert any("screening_unavailable" in r for r in row.reasons)


# ---------------------------------------------------------------- symptom 2
# Every frame counts. The incident video is clean for 59s and explicit at 60s.

@pytest.mark.incident
async def test_broken_misses_explicit_in_last_frame(client):
    payload = video(explicit_at_end(60))
    response = await client.post("/media", json=payload, headers=MEMBER_U)
    media_id = response.json()["id"]

    assert await broken.handle(await job_id_for(media_id)) == "scored"

    row = await media_row(media_id)
    # Published, explicit, in an unverified workspace: the exact breach.
    assert row.status == "published"
    assert row.explicit is False, "broken handler never saw the explicit frame"
    assert row.frames_scored == broken.BROKEN_FRAME_LIMIT


async def test_fixed_blocks_explicit_in_last_frame_unverified(client):
    """30 frames, explicit only in the last one: the synchronous scoring path."""
    payload = video(explicit_at_end(30))
    response = await client.post("/media", json=payload, headers=MEMBER_U)
    media_id = response.json()["id"]

    assert await fixed.handle(await job_id_for(media_id)) == "scored"

    row = await media_row(media_id)
    assert row.status == "blocked", f"expected blocked, got {row.status}"
    assert row.explicit is True
    assert "explicit_in_unverified_workspace" in row.reasons
    assert row.frames_scored == 30, "every frame must be scored"


async def test_fixed_publishes_same_video_in_verified_workspace(client):
    """Explicit between consenting adults is allowed -- but only when verified."""
    response = await client.post("/media", json=video(explicit_at_end(30)), headers=MEMBER_V)
    media_id = response.json()["id"]

    await fixed.handle(await job_id_for(media_id))

    row = await media_row(media_id)
    assert row.status == "published"
    assert row.explicit is True


async def test_fixed_long_video_stays_pending_until_the_callback_arrives(client):
    """A full minute of video crosses the provider's async threshold.

    The incident video was 60 frames, so it takes the 202-plus-callback path. The
    item must stay pending while the provider is still thinking -- never
    published on the strength of an accepted request -- and only reach a verdict
    when a signed result covering every frame arrives.
    """
    frames = explicit_at_end(60)
    response = await client.post("/media", json=video(frames), headers=MEMBER_U)
    media_id = response.json()["id"]

    assert await fixed.handle(await job_id_for(media_id)) == "accepted"

    row = await media_row(media_id)
    assert row.status == "pending", "202 is not a decision"
    assert row.screened_at is None

    # Deliver the result the way the provider would, signed over the raw body.
    import hashlib
    import hmac
    import json

    body = json.dumps(
        {"media_id": media_id, "frames": [dict(f) for f in frames]}, separators=(",", ":")
    ).encode()
    signature = hmac.new(settings.provider_secret, body, hashlib.sha256).hexdigest()

    callback = await client.post(
        "/provider/callback", content=body, headers={"X-Signature": signature}
    )
    assert callback.status_code == 200

    row = await media_row(media_id)
    assert row.status == "blocked"
    assert "explicit_in_unverified_workspace" in row.reasons
    assert row.frames_scored == 60


# ---------------------------------------------------------------- symptom 3
# A generation prompt asking for a minor is refused before anything happens.

@pytest.mark.incident
async def test_broken_lets_minor_prompt_through(client):
    settings.screening_mode = "broken"
    payload = video([{"explicit": 0.01}], source="generation", prompt="a 16-year-old girl")

    response = await client.post("/media", json=payload, headers=MEMBER_V)

    assert response.status_code == 202, "broken build accepts the prompt"
    assert response.json()["status"] == "pending"


async def test_fixed_refuses_minor_prompt(client):
    payload = video([{"explicit": 0.01}], source="generation", prompt="a 16-year-old girl")

    response = await client.post("/media", json=payload, headers=MEMBER_V)

    assert response.status_code == 422
    assert "reasons" in response.json()
    assert any("minor" in r for r in response.json()["reasons"])

    # Nothing was created, so nothing was generated and nothing was scored.
    async with Session() as session:
        count = (await session.execute(text("SELECT COUNT(*) FROM media"))).scalar_one()
    assert count == 0, "a refused prompt must not create media"


@pytest.mark.parametrize(
    "prompt",
    [
        "a 16-year-old girl",
        "sixteen year old",
        "underage model",
        "non-consensual scene",
        "sex with a dog",
        "suicide scene",
        "portrait of Taylor Swift",
        "incest roleplay, explicit",
        "sex trafficking victim",
    ],
)
async def test_fixed_refuses_every_prohibited_prompt_category(client, prompt):
    payload = video([{"explicit": 0.01}], source="generation", prompt=prompt)
    response = await client.post("/media", json=payload, headers=MEMBER_V)
    assert response.status_code == 422, f"{prompt!r} should be refused"


@pytest.mark.parametrize(
    "prompt",
    [
        "two adults on a beach at sunset",
        "a 19-year-old woman",
        "eighteen year old model",
        "two consenting adults, explicit",
    ],
)
async def test_fixed_allows_legitimate_prompts(client, prompt):
    payload = video([{"explicit": 0.01}], source="generation", prompt=prompt)
    response = await client.post("/media", json=payload, headers=MEMBER_V)
    assert response.status_code == 202, f"{prompt!r} should be allowed"


# ---------------------------------------------------------------- symptom 4
# Each media item is scored successfully at most once.

@pytest.mark.incident
async def test_broken_pays_twice_for_a_redelivered_job(client):
    response = await client.post("/media", json=video([{"explicit": 0.01}] * 3), headers=MEMBER_V)
    media_id = response.json()["id"]
    job_id = await job_id_for(media_id)

    # The same job delivered twice, concurrently -- what a redelivery looks like.
    await asyncio.gather(broken.handle(job_id), broken.handle(job_id))

    assert await provider_calls(media_id) >= 2, "broken handler pays per delivery"


async def test_fixed_scores_once_under_concurrent_redelivery(client):
    response = await client.post("/media", json=video([{"explicit": 0.01}] * 3), headers=MEMBER_V)
    media_id = response.json()["id"]
    job_id = await job_id_for(media_id)

    outcomes = await asyncio.gather(*(fixed.handle(job_id) for _ in range(4)))

    assert await provider_calls(media_id) == 1, f"expected 1 provider call, outcomes={outcomes}"
    # Exactly one delivery did the work; the rest found the lease taken.
    assert outcomes.count("not_claimed") >= 1
    assert (await media_row(media_id)).status == "published"


async def test_fixed_reuses_stored_result_instead_of_rescoring(client):
    response = await client.post("/media", json=video([{"explicit": 0.01}] * 3), headers=MEMBER_V)
    media_id = response.json()["id"]
    job_id = await job_id_for(media_id)

    assert await fixed.handle(job_id) == "scored"

    # Force the job back to runnable and hand it to the worker again.
    async with Session() as session:
        await session.execute(
            text("UPDATE jobs SET state='queued', lease_token=NULL, lease_expires_at=NULL "
                 "WHERE id=:j"),
            {"j": job_id},
        )
        await session.commit()

    assert await fixed.handle(job_id) == "cached"
    assert await provider_calls(media_id) == 1, "a second run must not buy a second call"


# ---------------------------------------------------------------- symptom 5
# A failed review-queue request must not look like an empty queue.

@pytest.mark.incident
async def test_broken_shows_failure_as_empty_list(client):
    settings.screening_mode = "broken"
    settings.review_fail = True

    response = await client.get("/review", headers=ADMIN_U)

    assert response.status_code == 200
    assert response.json() == [], "broken build reports a failure as 'nothing to review'"


async def test_fixed_reports_review_failure_as_failure(client):
    settings.review_fail = True

    response = await client.get("/review", headers=ADMIN_U)

    assert response.status_code == 503, "a broken queue must not look empty"
    assert response.json()["error"] == "review_queue_unavailable"


async def test_fixed_empty_queue_is_distinguishable_from_failure(client):
    response = await client.get("/review", headers=ADMIN_U)
    assert response.status_code == 200
    assert response.json() == []


async def test_held_items_appear_in_review_queue(client):
    """The 40 waiting items: held media must be visible to its workspace admin."""
    for _ in range(3):
        response = await client.post(
            "/media", json=video([{"minor": 0.6}]), headers=MEMBER_U
        )
        await fixed.handle(await job_id_for(response.json()["id"]))

    response = await client.get("/review", headers=ADMIN_U)
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 3, body
    assert all(item["status"] == "held" for item in body)


# ---------------------------------------------------------------- helper

async def _run_now(media_id: str) -> str:
    """Run one attempt immediately, ignoring the scheduled backoff.

    Backoff is real and tested elsewhere; here it would only make the test slow.
    """
    job_id = await job_id_for(media_id)
    async with Session() as session:
        await session.execute(
            text("UPDATE jobs SET next_attempt_at = NOW() WHERE id = :j"), {"j": job_id}
        )
        await session.commit()
    return await fixed.handle(job_id)
