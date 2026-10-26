"""A small Redis queue with per-workspace fairness.

Why hand-rolled instead of Celery/RQ/arq: the requirement is that a burst of 200
videos from one workspace must not stop the other workspace's single images from
being screened. Off-the-shelf workers pull FIFO from one list, so 200 videos
queued first are 200 videos screened first and the second workspace waits behind
all of them. Fairness has to live in the dispatcher, so the dispatcher is ours.

The trade-off is spelled out in the README: we give up the mature retry,
scheduling and monitoring that a library ships with, and we take on the job of
reconciling Redis against Postgres. Postgres remains the source of truth -- Redis
only holds a hint of what to look at next, so losing Redis entirely costs
latency, not correctness.

Keys
  q:ws:<workspace_id>  list   job IDs ready to run for that workspace
  q:active             set    workspaces that currently have queued work
  q:delayed            zset   "workspace_id|job_id" scored by due unix ts
  q:rr                 int    round-robin cursor
  q:inflight:<media>   string short-lived guard against enqueueing a duplicate
"""
import time

from redis.asyncio import Redis

from app.config import settings

_redis: Redis | None = None


def redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis


def ws_key(workspace_id: str) -> str:
    return f"q:ws:{workspace_id}"


async def enqueue(workspace_id: str, job_id: str) -> None:
    r = redis()
    pipe = r.pipeline()
    pipe.rpush(ws_key(workspace_id), job_id)
    pipe.sadd("q:active", workspace_id)
    await pipe.execute()


async def enqueue_delayed(workspace_id: str, job_id: str, delay_s: float) -> None:
    await redis().zadd("q:delayed", {f"{workspace_id}|{job_id}": time.time() + delay_s})


async def promote_due(limit: int = 500) -> int:
    """Move delayed jobs whose backoff has elapsed into their workspace queue."""
    r = redis()
    now = time.time()
    due = await r.zrangebyscore("q:delayed", 0, now, start=0, num=limit)
    moved = 0
    for member in due:
        # Remove first: whoever wins the removal owns the re-enqueue, so two
        # reconcilers cannot both promote the same job.
        if await r.zrem("q:delayed", member):
            workspace_id, _, job_id = member.partition("|")
            await enqueue(workspace_id, job_id)
            moved += 1
    return moved


async def dequeue(timeout_s: float = 1.0) -> tuple[str, str] | None:
    """Pop one job, rotating across workspaces that have work.

    Returns (workspace_id, job_id) or None. LPOP is atomic, so two workers can
    never receive the same job ID; the round-robin cursor is advisory, and a race
    on it costs a little fairness, never correctness.
    """
    r = redis()
    deadline = time.time() + timeout_s

    while True:
        workspaces = sorted(await r.smembers("q:active"))
        if workspaces:
            cursor = await r.incr("q:rr")
            start = cursor % len(workspaces)
            order = workspaces[start:] + workspaces[:start]
            for workspace_id in order:
                job_id = await r.lpop(ws_key(workspace_id))
                if job_id:
                    return workspace_id, job_id
                # Queue drained: stop rotating onto it until it has work again.
                if not await r.llen(ws_key(workspace_id)):
                    await r.srem("q:active", workspace_id)

        if time.time() >= deadline:
            return None
        # Nothing ready. Sleep briefly rather than spinning; the queue is not
        # latency-critical at this size.
        import asyncio

        await asyncio.sleep(0.05)


async def depth() -> dict[str, int]:
    r = redis()
    out: dict[str, int] = {}
    for workspace_id in await r.smembers("q:active"):
        out[workspace_id] = await r.llen(ws_key(workspace_id))
    out["_delayed"] = await r.zcard("q:delayed")
    return out


async def clear() -> None:
    """Test helper: drop all queue state."""
    r = redis()
    keys = await r.keys("q:*")
    if keys:
        await r.delete(*keys)


def reset_client() -> None:
    """Forget the cached client without awaiting anything.

    An asyncio Redis client binds to the event loop that created it, so a client
    built under one loop raises "attached to a different loop" when reused under
    another. Long-lived processes have one loop and never hit this; the test
    suite gets a fresh loop per test, so it drops the reference first. Sync on
    purpose -- it has to be callable when the old loop is already gone.
    """
    global _redis
    _redis = None


async def aclose() -> None:
    """Close the client and forget it."""
    global _redis
    if _redis is not None:
        try:
            await _redis.aclose()
        except Exception:
            pass
        _redis = None
