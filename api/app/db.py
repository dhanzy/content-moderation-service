from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models import Base

engine = create_async_engine(settings.database_url, pool_size=20, max_overflow=20, future=True)
Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


# Any constant works; it just has to be the same in every process.
SCHEMA_LOCK_KEY = 727274


async def init_db() -> None:
    """Create tables and seed the two fixed workspaces.

    The api and worker containers start together and both call this, so schema
    creation is serialised behind a Postgres advisory lock. Without it the two
    processes issue CREATE TABLE concurrently and the loser dies on a duplicate
    pg_type entry -- create_all's checkfirst is not atomic.

    create_all is used instead of migrations to keep the exercise small; a real
    service would own an Alembic history. Noted as a limitation in the README.
    """
    async with engine.begin() as conn:
        # Held until this transaction ends, so the second process waits here and
        # then finds the tables already present.
        await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SCHEMA_LOCK_KEY})
        await conn.run_sync(Base.metadata.create_all)
    await seed()


async def seed() -> None:
    """Seed the fixed workspaces and users from the brief.

    Written as upserts rather than read-then-insert so that two processes seeding
    at once cannot collide on a primary key.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models import User, Workspace

    rows = [
        ("ws_verified", True, [("admin_v", "admin"), ("member_v", "member")]),
        ("ws_unverified", False, [("admin_u", "admin"), ("member_u", "member")]),
    ]
    async with Session() as s:
        for ws_id, verified, users in rows:
            await s.execute(
                pg_insert(Workspace)
                .values(id=ws_id, verified=verified)
                .on_conflict_do_update(index_elements=["id"], set_={"verified": verified})
            )
            for uid, role in users:
                await s.execute(
                    pg_insert(User)
                    .values(id=uid, workspace_id=ws_id, role=role)
                    .on_conflict_do_update(
                        index_elements=["id"], set_={"workspace_id": ws_id, "role": role}
                    )
                )
        await s.commit()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with Session() as s:
        yield s
