"""Provider callback signing: rule 7, plus the partial-result guard."""
import hashlib
import hmac
import json

from app.config import settings
from tests.conftest import MEMBER_U, MEMBER_V, media_row, video


def sign(body: bytes, secret: bytes | None = None) -> str:
    return hmac.new(secret or settings.provider_secret, body, hashlib.sha256).hexdigest()


def body_for(media_id: str, frames: list[dict]) -> bytes:
    return json.dumps({"media_id": media_id, "frames": frames}, separators=(",", ":")).encode()


async def create(client, frames: list[dict], who=MEMBER_V) -> str:
    response = await client.post("/media", json=video(frames), headers=who)
    return response.json()["id"]


async def test_callback_without_signature_is_rejected(client):
    media_id = await create(client, [{"explicit": 0.01}])
    body = body_for(media_id, [{"explicit": 0.01}])

    response = await client.post(
        "/provider/callback", content=body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 401
    assert (await media_row(media_id)).status == "pending", "unsigned callback must not decide"


async def test_callback_with_wrong_signature_is_rejected(client):
    media_id = await create(client, [{"explicit": 0.01}])
    body = body_for(media_id, [{"explicit": 0.01}])

    response = await client.post(
        "/provider/callback", content=body, headers={"X-Signature": sign(body, b"wrong-secret")}
    )

    assert response.status_code == 401
    assert (await media_row(media_id)).status == "pending"


async def test_callback_signature_must_cover_the_exact_body(client):
    """A signature valid for different bytes must not authorise this payload."""
    media_id = await create(client, [{"explicit": 0.01}])
    honest = body_for(media_id, [{"explicit": 0.01}])
    tampered = body_for(media_id, [{"explicit": 0.99}])

    response = await client.post(
        "/provider/callback", content=tampered, headers={"X-Signature": sign(honest)}
    )

    assert response.status_code == 401


async def test_valid_callback_applies_the_decision(client):
    frames = [{"explicit": 0.01}] * 40
    media_id = await create(client, frames)
    scored = [{"explicit": 0.01}] * 39 + [{"explicit": 0.95}]
    body = body_for(media_id, scored)

    response = await client.post(
        "/provider/callback", content=body, headers={"X-Signature": sign(body)}
    )

    assert response.status_code == 200
    row = await media_row(media_id)
    assert row.status == "published"  # verified workspace, explicit allowed
    assert row.explicit is True
    assert row.screened_at is not None


async def test_valid_callback_blocks_explicit_in_unverified_workspace(client):
    frames = [{"explicit": 0.01}] * 40
    media_id = await create(client, frames, who=MEMBER_U)
    scored = [{"explicit": 0.01}] * 39 + [{"explicit": 0.95}]
    body = body_for(media_id, scored)

    await client.post("/provider/callback", content=body, headers={"X-Signature": sign(body)})

    row = await media_row(media_id)
    assert row.status == "blocked"
    assert "explicit_in_unverified_workspace" in row.reasons


async def test_sha256_prefixed_signature_is_accepted(client):
    media_id = await create(client, [{"explicit": 0.01}])
    body = body_for(media_id, [{"explicit": 0.01}])

    response = await client.post(
        "/provider/callback", content=body, headers={"X-Signature": f"sha256={sign(body)}"}
    )

    assert response.status_code == 200


async def test_duplicate_callback_is_ignored(client):
    """Rule 8 from the callback side: a redelivered result is not applied twice."""
    media_id = await create(client, [{"explicit": 0.01}])
    body = body_for(media_id, [{"explicit": 0.01}])
    headers = {"X-Signature": sign(body)}

    first = await client.post("/provider/callback", content=body, headers=headers)
    second = await client.post("/provider/callback", content=body, headers=headers)

    assert first.status_code == 200
    assert second.json()["status"] == "already_applied"


async def test_partial_callback_is_held_not_published(client):
    """A result covering fewer frames than the item has is not a clean pass."""
    media_id = await create(client, [{"explicit": 0.01}] * 40)
    scored = [{"explicit": 0.01}] * 8  # only the first 8, as in the incident
    body = body_for(media_id, scored)

    response = await client.post(
        "/provider/callback", content=body, headers={"X-Signature": sign(body)}
    )

    assert response.json()["reason"] == "frame_count_mismatch"
    row = await media_row(media_id)
    assert row.status == "held"
    assert row.screened_at is None


async def test_malformed_frames_in_callback_are_held(client):
    media_id = await create(client, [{"explicit": 0.01}])
    body = json.dumps({"media_id": media_id, "frames": "not-a-list"}).encode()

    response = await client.post(
        "/provider/callback", content=body, headers={"X-Signature": sign(body)}
    )

    assert response.json()["status"] == "held"
    assert (await media_row(media_id)).status == "held"


async def test_signed_callback_for_unknown_media_is_acknowledged(client):
    body = body_for("does-not-exist", [{"explicit": 0.01}])
    response = await client.post(
        "/provider/callback", content=body, headers={"X-Signature": sign(body)}
    )
    assert response.status_code == 200
    assert response.json()["reason"] == "unknown_media"
