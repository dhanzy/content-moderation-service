#!/usr/bin/env python3
"""Repeatable workload for the queue requirements in part 2 of the brief.

One workspace uploads a burst of videos while the other uploads single images,
and we measure whether the small workspace's work still gets screened -- during
a provider outage and after it ends.

Three numbers are reported for the same workload before and after the fix, which
is what the brief asks for:

  * media published unchecked   (live with no provider decision behind it)
  * provider calls per upload   (the invoice)
  * age of the oldest waiting job

Usage
  uv run python load/workload.py                      # fixed mode, with outage
  uv run python load/workload.py --no-outage          # clean run
  uv run python load/workload.py --videos 200
  uv run python load/workload.py --label broken       # name the output file

The stack must already be running. Point it elsewhere with --api / --provider.
The script never assumes which SCREENING_MODE the service is in; it reads it
from /healthz and records it, so the two runs cannot be mixed up.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

BURST_WS = "ws_unverified"
BURST_USER = "member_u"
STEADY_WS = "ws_verified"
STEADY_USER = "member_v"

TERMINAL = {"published", "blocked", "held"}


def headers(user: str, workspace: str) -> dict[str, str]:
    return {"X-User-Id": user, "X-Workspace-Id": workspace}


@dataclass
class Item:
    media_id: str
    workspace: str
    kind: str  # "video" | "image"
    submitted_at: float
    decided_at: float | None = None
    status: str | None = None

    @property
    def latency(self) -> float | None:
        if self.decided_at is None:
            return None
        return self.decided_at - self.submitted_at


@dataclass
class Run:
    label: str
    screening_mode: str
    videos: int
    images: int
    outage: bool
    items: list[Item] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    metrics_before: dict = field(default_factory=dict)
    metrics_after: dict = field(default_factory=dict)
    provider_stats: dict = field(default_factory=dict)
    wall_time: float = 0.0

    def note(self, event: str, **fields) -> None:
        entry = {"t": round(time.time() - self.t0, 2), "event": event, **fields}
        self.events.append(entry)
        print(f"  [{entry['t']:>6.2f}s] {event} " + " ".join(f"{k}={v}" for k, v in fields.items()))

    t0: float = field(default_factory=time.time)


def video_payload(frames: int) -> dict:
    """A clean video. Frames are kept under the provider's async threshold so the
    measurement isolates queue behaviour from callback latency; the asynchronous
    path has its own coverage in the test suite."""
    return {"type": "video", "source": "upload", "frames": [{"explicit": 0.01}] * frames}


def image_payload() -> dict:
    return {"type": "image", "source": "upload", "frames": [{"explicit": 0.02}]}


async def submit(client: httpx.AsyncClient, payload: dict, user: str, ws: str, kind: str) -> Item | None:
    try:
        response = await client.post("/media", json=payload, headers=headers(user, ws), timeout=30.0)
        response.raise_for_status()
        return Item(response.json()["id"], ws, kind, time.time())
    except Exception as exc:  # pragma: no cover - diagnostics only
        print(f"    submit failed: {exc}", file=sys.stderr)
        return None


async def poll_until_decided(
    client: httpx.AsyncClient, items: list[Item], deadline_s: float, run: Run
) -> None:
    """Poll each item until it reaches a terminal status or we run out of time."""
    pending = {i.media_id: i for i in items}
    end = time.time() + deadline_s
    while pending and time.time() < end:
        for media_id, item in list(pending.items()):
            user = BURST_USER if item.workspace == BURST_WS else STEADY_USER
            try:
                response = await client.get(
                    f"/media/{media_id}", headers=headers(user, item.workspace), timeout=10.0
                )
                status = response.json()["status"]
            except Exception:
                continue
            if status in TERMINAL:
                item.status = status
                item.decided_at = time.time()
                pending.pop(media_id, None)
        if pending:
            await asyncio.sleep(0.25)
    if pending:
        run.note("poll_timeout", undecided=len(pending))


async def settle_outage_holds(
    client: httpx.AsyncClient, run: Run, settle_s: float
) -> None:
    """After recovery, wait for provisionally-held items to be re-screened.

    An item held because screening was unavailable is not a finished verdict: the
    re-screening sweep will take it again once the provider is healthy. Treating
    `held` as terminal ends the run before recovery has happened and makes the
    oldest-waiting-job figure describe the middle of the outage rather than the
    end of the run. Items held for a human reason (`review:`) are left alone --
    those are genuinely terminal.
    """
    end = time.time() + settle_s
    while time.time() < end:
        outstanding = []
        for item in run.items:
            if item.status != "held":
                continue
            user = BURST_USER if item.workspace == BURST_WS else STEADY_USER
            try:
                body = (
                    await client.get(
                        f"/media/{item.media_id}",
                        headers=headers(user, item.workspace),
                        timeout=10.0,
                    )
                ).json()
            except Exception:
                continue
            # Status first. An item that has already been re-screened has lost
            # its screening_unavailable reason, so filtering on reasons before
            # status leaves it recorded as held forever -- which is how this
            # function first reported 20 held items that the database said were
            # published.
            if body["status"] != "held":
                item.status = body["status"]
                item.decided_at = time.time()
                continue

            reasons = body.get("reasons") or []
            if not any(str(r).startswith("screening_unavailable") for r in reasons):
                continue  # held for a human to judge: genuinely terminal
            outstanding.append(item.media_id)
        if not outstanding:
            run.note("outage_holds_settled")
            return
        await asyncio.sleep(1.0)
    run.note("settle_timeout", still_held=len(outstanding))


async def metrics(client: httpx.AsyncClient) -> dict:
    try:
        return (await client.get("/_metrics", timeout=20.0)).json()
    except Exception as exc:  # pragma: no cover
        return {"error": str(exc)}


async def set_provider(provider: str, mode: str, delay_ms: int = 0) -> None:
    async with httpx.AsyncClient(timeout=15.0) as c:
        await c.post(f"{provider}/_control", json={"mode": mode, "delay_ms": delay_ms})


async def set_provider_healthy(args: argparse.Namespace) -> None:
    """Put the provider into its 'working' state for this run.

    With --provider-delay-ms the working state is `slow` rather than `ok`. A
    provider that answers instantly empties a 200-video burst faster than the
    other workspace can submit anything, so nothing ever queues and the
    fairness question is never actually asked. A couple of hundred milliseconds
    per call -- which is optimistic for a real moderation API scoring a video --
    makes the backlog real and the starvation measurable.
    """
    if args.provider_delay_ms > 0:
        await set_provider(args.provider, "slow", args.provider_delay_ms)
    else:
        await set_provider(args.provider, "ok")


async def provider_stats(provider: str) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as c:
        return (await c.get(f"{provider}/_stats")).json()


async def run_workload(args: argparse.Namespace) -> Run:
    api = args.api.rstrip("/")

    async with httpx.AsyncClient(base_url=api) as client:
        mode = (await client.get("/healthz", timeout=15.0)).json().get("screening_mode", "unknown")
        run = Run(
            label=args.label or mode,
            screening_mode=mode,
            videos=args.videos,
            images=args.images,
            outage=not args.no_outage,
        )
        print(f"\n=== workload: {run.label}  (service reports SCREENING_MODE={mode}) ===")

        # Clean slate, including the provider's call counters, so "calls per
        # upload" describes only this run.
        await client.post("/_admin/reset", timeout=60.0)
        await set_provider_healthy(args)
        run.metrics_before = await metrics(client)
        run.note("reset_done")

        if run.outage:
            await set_provider(args.provider, "timeout")
            run.note("provider_outage_begins", mode="timeout")

        # The burst: one workspace submits every video as fast as it can.
        burst_payload = video_payload(args.video_frames)
        run.note("burst_submit_begin", videos=args.videos)
        burst_items = [
            item
            for item in await asyncio.gather(
                *(
                    submit(client, burst_payload, BURST_USER, BURST_WS, "video")
                    for _ in range(args.videos)
                )
            )
            if item is not None
        ]
        run.note("burst_submit_done", accepted=len(burst_items))

        # The other workspace trickles in single images while the burst is
        # queued. These are the ones that must not be starved.
        steady_items: list[Item] = []
        for _ in range(args.images):
            item = await submit(client, image_payload(), STEADY_USER, STEADY_WS, "image")
            if item is not None:
                steady_items.append(item)
            await asyncio.sleep(args.image_interval)
        run.note("steady_submit_done", images=len(steady_items))

        if run.outage:
            # Let the outage bite, measure, then recover.
            await asyncio.sleep(args.outage_s)
            during = await metrics(client)
            run.note(
                "during_outage",
                published_unchecked=during.get("published_unchecked"),
                oldest_waiting_s=during.get("oldest_waiting_job_age_s"),
                by_status=json.dumps(during.get("media_by_status", {})),
            )
            run.events[-1]["metrics"] = during
            await set_provider_healthy(args)
            run.note("provider_recovered",
                     mode="slow" if args.provider_delay_ms else "ok",
                     delay_ms=args.provider_delay_ms)

        run.items = burst_items + steady_items
        await poll_until_decided(client, run.items, args.deadline, run)

        if run.outage:
            # A provisional hold is not a finished verdict; give the
            # re-screening sweep time to clear them before measuring.
            await settle_outage_holds(client, run, args.settle_s)

        run.metrics_after = await metrics(client)
        run.provider_stats = await provider_stats(args.provider)
        run.wall_time = time.time() - run.t0
        run.note("done", wall_s=round(run.wall_time, 2))

    return run


def summarise(run: Run) -> dict:
    def stats_for(kind: str) -> dict:
        lats = [i.latency for i in run.items if i.kind == kind and i.latency is not None]
        if not lats:
            return {"count": 0}
        lats.sort()
        return {
            "count": len(lats),
            "median_s": round(statistics.median(lats), 2),
            "p95_s": round(lats[min(int(len(lats) * 0.95), len(lats) - 1)], 2),
            "max_s": round(max(lats), 2),
        }

    calls = run.provider_stats.get("calls", {}) or {}
    call_values = [int(v) for v in calls.values()]
    decided = [i for i in run.items if i.status is not None]
    statuses: dict[str, int] = {}
    for item in run.items:
        statuses[item.status or "undecided"] = statuses.get(item.status or "undecided", 0) + 1

    after = run.metrics_after
    return {
        "label": run.label,
        "screening_mode": run.screening_mode,
        "outage": run.outage,
        "submitted": len(run.items),
        "decided": len(decided),
        "wall_s": round(run.wall_time, 2),
        "statuses": statuses,
        # The three numbers the brief asks to compare.
        "published_unchecked": after.get("published_unchecked"),
        "provider_calls_per_upload": (
            round(sum(call_values) / len(run.items), 3) if run.items and call_values else None
        ),
        "oldest_waiting_job_age_s": after.get("oldest_waiting_job_age_s"),
        # Supporting evidence.
        "explicit_published_in_unverified": after.get("explicit_published_in_unverified"),
        "media_with_duplicate_calls": sum(1 for v in call_values if v > 1),
        "max_calls_for_one_item": max(call_values) if call_values else 0,
        "latency_burst_videos": stats_for("video"),
        "latency_steady_images": stats_for("image"),
    }


def print_summary(summary: dict) -> None:
    print(f"\n{'=' * 64}\n  {summary['label']}  (mode={summary['screening_mode']}, "
          f"outage={summary['outage']})\n{'=' * 64}")
    rows = [
        ("media published unchecked", summary["published_unchecked"]),
        ("explicit published in unverified ws", summary["explicit_published_in_unverified"]),
        ("provider calls per upload", summary["provider_calls_per_upload"]),
        ("oldest waiting job age (s)", summary["oldest_waiting_job_age_s"]),
        ("items with >1 provider call", summary["media_with_duplicate_calls"]),
        ("max calls for a single item", summary["max_calls_for_one_item"]),
        ("submitted / decided", f"{summary['submitted']} / {summary['decided']}"),
        ("wall time (s)", summary["wall_s"]),
        ("statuses", json.dumps(summary["statuses"])),
    ]
    for name, value in rows:
        print(f"  {name:<38} {value}")
    print("\n  time from upload to decision")
    for name, key in (("burst videos", "latency_burst_videos"), ("steady images", "latency_steady_images")):
        s = summary[key]
        if s.get("count"):
            print(f"    {name:<16} n={s['count']:<5} median={s['median_s']}s  "
                  f"p95={s['p95_s']}s  max={s['max_s']}s")
        else:
            print(f"    {name:<16} none decided")
    print()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--provider", default="http://localhost:8001")
    parser.add_argument("--videos", type=int, default=200, help="burst size from ws_unverified")
    parser.add_argument("--video-frames", type=int, default=20, help="frames per burst video")
    parser.add_argument("--images", type=int, default=20, help="single images from ws_verified")
    parser.add_argument("--image-interval", type=float, default=0.2)
    parser.add_argument("--provider-delay-ms", type=int, default=0,
                        help="per-call provider latency; makes the burst actually queue")
    parser.add_argument("--outage-s", type=float, default=20.0, help="how long the provider stays down")
    parser.add_argument("--no-outage", action="store_true", help="run with a healthy provider throughout")
    parser.add_argument("--deadline", type=float, default=240.0, help="max seconds to wait for decisions")
    parser.add_argument("--settle-s", type=float, default=120.0,
                        help="after recovery, how long to wait for outage holds to be re-screened")
    parser.add_argument("--label", default="", help="name for the output file")
    parser.add_argument("--out-dir", default="evidence")
    args = parser.parse_args()

    run = await run_workload(args)
    summary = summarise(run)
    print_summary(summary)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"workload-{run.label}{'-outage' if run.outage else ''}.json"
    out_path.write_text(
        json.dumps(
            {
                "summary": summary,
                "events": run.events,
                "metrics_after": run.metrics_after,
                "items": [
                    {
                        "media_id": i.media_id,
                        "workspace": i.workspace,
                        "kind": i.kind,
                        "status": i.status,
                        "latency_s": round(i.latency, 3) if i.latency is not None else None,
                    }
                    for i in run.items
                ],
            },
            indent=2,
        )
    )
    print(f"  written: {out_path}\n")

    # Non-zero exit when a safety invariant was violated, so this can gate CI.
    if run.screening_mode == "fixed":
        if summary["published_unchecked"]:
            print("FAIL: media published without being screened", file=sys.stderr)
            return 1
        if summary["explicit_published_in_unverified"]:
            print("FAIL: explicit media published in an unverified workspace", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
