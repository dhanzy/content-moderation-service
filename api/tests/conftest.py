"""Test fixtures.

Isolation matters here. The tests talk to the same Postgres and Redis instances
that the running stack uses, so they are pointed at their own database
(moderation_test) and their own Redis DB index (1). Without that, the worker
container would pick jobs out from under a test through the reconciler and the
results would be timing-dependent.

The environment is set before any app module is imported, because app.config
reads os.environ at import time.
"""
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

# Load api/.env before reading anything, the same way app/config.py does, so
# `uv run pytest` with nothing exported sees the same settings as the services.
# override=False, so a real environment variable still wins.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

# Fallbacks for when neither the environment nor .env says anything. These match
# the host ports docker-compose.yml publishes.
PG_HOST_PORT = os.environ.get("PG_HOST_PORT", "5432")
REDIS_HOST_PORT = os.environ.get("REDIS_HOST_PORT", "6379")

TEST_DB_NAME = "moderation_test"
TEST_REDIS_INDEX = "1"


def _with_path(url: str, path: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{path}", parts.query, parts.fragment))


# The test database and Redis index are forced, never merely defaulted. Inside
# the api container DATABASE_URL and REDIS_URL already point at the live
# database and Redis DB 0, so a setdefault here would have the suite truncate
# real data and fight the worker for jobs. Host and container runs both end up
# on moderation_test and Redis DB 1.
_base_db = os.environ.get(
    "DATABASE_URL", f"postgresql+asyncpg://mod:mod@localhost:{PG_HOST_PORT}/moderation"
)
_base_redis = os.environ.get("REDIS_URL", f"redis://localhost:{REDIS_HOST_PORT}/0")

os.environ["DATABASE_URL"] = _with_path(_base_db, TEST_DB_NAME)
os.environ["REDIS_URL"] = _with_path(_base_redis, TEST_REDIS_INDEX)
os.environ.setdefault("PROVIDER_URL", "http://localhost:8001")
os.environ.setdefault("PROVIDER_SECRET", "dev-provider-secret")
os.environ.setdefault("SCREENING_MODE", "fixed")
os.environ.setdefault("MAX_ATTEMPTS", "3")
os.environ.setdefault("PROVIDER_TIMEOUT_S", "2.0")
os.environ.setdefault("LEASE_SECONDS", "30")
os.environ.setdefault("LOG_LEVEL", "WARNING")

import asyncpg  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import Session, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Job, Media  # noqa: E402
from app.workq import fairqueue  # noqa: E402

_db_ready = False

TABLES = "review_actions, job_attempts, provider_results, jobs, media"


async def _ensure_database() -> None:
    """Create the test database once, then the tables."""
    global _db_ready
    if _db_ready:
        return

    # Connect to the default database to issue CREATE DATABASE, using the same
    # host and credentials the test URL resolved to, with the asyncpg dialect
    # suffix stripped for the raw driver.
    admin_dsn = _with_path(os.environ["DATABASE_URL"], "moderation").replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    conn = await asyncpg.connect(admin_dsn)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", "moderation_test"
        )
        if not exists:
            # CREATE DATABASE cannot run inside a transaction block.
            await conn.execute("CREATE DATABASE moderation_test")
    finally:
        await conn.close()

    await init_db()
    _db_ready = True


@pytest.fixture
async def db():
    """Give a test an empty database, an empty queue and a healthy provider.

    Deliberately not autouse: the rule tests are pure functions and must not pay
    for a database round trip. Integration tests reach this through `client`.

    The engine is disposed on teardown because pytest-asyncio gives each test its
    own event loop, and a pooled asyncpg connection created on a previous loop
    fails with InterfaceError when reused on the next one.
    """
    # Both clients bind to the loop that created them, and this test has a new
    # one. Drop the Redis reference and the connection pool before using either.
    fairqueue.reset_client()
    await engine.dispose()

    await _ensure_database()

    async with Session() as session:
        await session.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        await session.commit()

    await fairqueue.clear()

    # Reset the provider: mode ok, call counters zeroed, so a test can assert on
    # exactly how many calls its own media caused.
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(f"{settings.provider_url}/_stats/reset")
            await client.post(f"{settings.provider_url}/_control", json={"mode": "ok"})
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"fake provider not reachable at {settings.provider_url}: {exc}")

    # Each test chooses its own handler explicitly, so reset the shared flags
    # that routes read at request time.
    settings.screening_mode = "fixed"
    settings.review_fail = False

    yield

    settings.screening_mode = "fixed"
    settings.review_fail = False
    # Release both clients so nothing outlives this test's event loop.
    await fairqueue.aclose()
    await engine.dispose()


@pytest.fixture
async def client(db):
    """The real app, in process, over ASGI -- no API container required."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def headers(user_id: str, workspace_id: str) -> dict[str, str]:
    return {"X-User-Id": user_id, "X-Workspace-Id": workspace_id}


# Convenience identities matching the seeded workspaces.
ADMIN_V = headers("admin_v", "ws_verified")
MEMBER_V = headers("member_v", "ws_verified")
ADMIN_U = headers("admin_u", "ws_unverified")
MEMBER_U = headers("member_u", "ws_unverified")


def video(frames: list[dict], source: str = "upload", prompt: str | None = None) -> dict:
    return {"type": "video", "source": source, "prompt": prompt, "frames": frames}


def image(scores: dict, source: str = "upload", prompt: str | None = None) -> dict:
    return {"type": "image", "source": source, "prompt": prompt, "frames": [scores]}


def clean_video(seconds: int = 10) -> list[dict]:
    return [{"explicit": 0.01} for _ in range(seconds)]


def explicit_at_end(seconds: int = 60) -> list[dict]:
    """The incident video: clean throughout, explicit in its final second."""
    return [{"explicit": 0.01} for _ in range(seconds - 1)] + [{"explicit": 0.95}]


async def job_for(media_id: str) -> Job:
    async with Session() as session:
        return (
            await session.execute(text("SELECT * FROM jobs WHERE media_id = :m"), {"m": media_id})
        ).one()


async def job_id_for(media_id: str) -> str:
    async with Session() as session:
        return (
            await session.execute(
                text("SELECT id FROM jobs WHERE media_id = :m"), {"m": media_id}
            )
        ).scalar_one()


async def media_row(media_id: str) -> Media:
    async with Session() as session:
        return await session.get(Media, media_id)


async def provider_calls(media_id: str) -> int:
    async with httpx.AsyncClient(timeout=5.0) as c:
        stats = (await c.get(f"{settings.provider_url}/_stats")).json()
    return int(stats.get("calls", {}).get(media_id, 0))


async def set_provider(mode: str, delay_ms: int = 0) -> None:
    async with httpx.AsyncClient(timeout=5.0) as c:
        await c.post(
            f"{settings.provider_url}/_control", json={"mode": mode, "delay_ms": delay_ms}
        )
