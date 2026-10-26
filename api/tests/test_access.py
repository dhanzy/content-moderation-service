"""Workspace isolation and role checks: rules 1 and 6."""
from tests.conftest import (
    ADMIN_U,
    ADMIN_V,
    MEMBER_U,
    MEMBER_V,
    headers,
    image,
    job_id_for,
    video,
)
from app.screening import fixed


# ------------------------------------------------------------------- rule 1

async def test_other_workspace_cannot_read_media(client):
    created = await client.post("/media", json=image({"explicit": 0.01}), headers=MEMBER_V)
    media_id = created.json()["id"]

    # Its own workspace can see it.
    assert (await client.get(f"/media/{media_id}", headers=MEMBER_V)).status_code == 200
    assert (await client.get(f"/media/{media_id}", headers=ADMIN_V)).status_code == 200

    # The other workspace gets 404, not 403: the response must not confirm that
    # the item exists somewhere else.
    for who in (MEMBER_U, ADMIN_U):
        response = await client.get(f"/media/{media_id}", headers=who)
        assert response.status_code == 404, response.text


async def test_review_queue_only_shows_own_workspace(client):
    held_v = await client.post("/media", json=image({"minor": 0.6}), headers=MEMBER_V)
    held_u = await client.post("/media", json=image({"minor": 0.6}), headers=MEMBER_U)
    for response in (held_v, held_u):
        await fixed.handle(await job_id_for(response.json()["id"]))

    queue_v = (await client.get("/review", headers=ADMIN_V)).json()
    queue_u = (await client.get("/review", headers=ADMIN_U)).json()

    assert [item["id"] for item in queue_v] == [held_v.json()["id"]]
    assert [item["id"] for item in queue_u] == [held_u.json()["id"]]


async def test_admin_cannot_act_on_another_workspaces_media(client):
    created = await client.post("/media", json=image({"minor": 0.6}), headers=MEMBER_V)
    media_id = created.json()["id"]
    await fixed.handle(await job_id_for(media_id))

    # An admin of the other workspace must not be able to approve it.
    response = await client.post(f"/review/{media_id}/approve", headers=ADMIN_U)
    assert response.status_code == 404


async def test_user_cannot_borrow_another_workspace_id(client):
    """Sending someone else's workspace header is rejected, not silently scoped."""
    response = await client.post(
        "/media", json=image({"explicit": 0.01}), headers=headers("member_v", "ws_unverified")
    )
    assert response.status_code == 403


async def test_identity_headers_are_required(client):
    assert (await client.get("/review")).status_code == 401
    assert (await client.post("/media", json=image({"explicit": 0.1}))).status_code == 401


async def test_unknown_user_is_rejected(client):
    response = await client.get("/review", headers=headers("nobody", "ws_verified"))
    assert response.status_code == 401


# ------------------------------------------------------------------- rule 6

async def test_member_cannot_approve_or_reject(client):
    created = await client.post("/media", json=image({"minor": 0.6}), headers=MEMBER_V)
    media_id = created.json()["id"]
    await fixed.handle(await job_id_for(media_id))

    for action in ("approve", "reject"):
        response = await client.post(f"/review/{media_id}/{action}", headers=MEMBER_V)
        assert response.status_code == 403, f"member should not be able to {action}"


async def test_member_cannot_read_review_queue(client):
    assert (await client.get("/review", headers=MEMBER_V)).status_code == 403


async def test_admin_can_approve_a_held_item(client):
    created = await client.post("/media", json=image({"minor": 0.6}), headers=MEMBER_V)
    media_id = created.json()["id"]
    await fixed.handle(await job_id_for(media_id))

    response = await client.post(f"/review/{media_id}/approve", headers=ADMIN_V)
    assert response.status_code == 200
    assert response.json()["status"] == "published"

    # It leaves the queue once decided.
    assert (await client.get("/review", headers=ADMIN_V)).json() == []


async def test_admin_can_reject_a_held_item(client):
    created = await client.post("/media", json=image({"minor": 0.6}), headers=MEMBER_V)
    media_id = created.json()["id"]
    await fixed.handle(await job_id_for(media_id))

    response = await client.post(f"/review/{media_id}/reject", headers=ADMIN_V)
    assert response.status_code == 200
    assert response.json()["status"] == "blocked"


async def test_admin_cannot_approve_past_an_absolute_rule(client):
    """An admin may clear a borderline score, never a payment-processor rule.

    The item is held for a 0.6 on self_harm, but the stored provider result also
    carries explicit 0.95 in an unverified workspace. Approving must not publish it.
    """
    payload = video([{"self_harm": 0.6}, {"explicit": 0.95}])
    created = await client.post("/media", json=payload, headers=MEMBER_U)
    media_id = created.json()["id"]
    await fixed.handle(await job_id_for(media_id))

    # It was blocked outright rather than held, because explicit in an unverified
    # workspace is absolute.
    detail = (await client.get(f"/media/{media_id}", headers=MEMBER_U)).json()
    assert detail["status"] == "blocked"

    # And an approve on a non-held item is refused.
    response = await client.post(f"/review/{media_id}/approve", headers=ADMIN_U)
    assert response.status_code == 409


async def test_approving_a_pending_item_is_refused(client):
    created = await client.post("/media", json=image({"explicit": 0.01}), headers=MEMBER_V)
    media_id = created.json()["id"]

    response = await client.post(f"/review/{media_id}/approve", headers=ADMIN_V)
    assert response.status_code == 409
