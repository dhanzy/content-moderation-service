# Load, failure and recovery evidence

What the system actually did under a 200-video burst, a 25-second provider
outage and a worker killed mid-call — measured, not asserted. Includes the
metrics endpoint and how to trace one item through the logs.

[← back to the README](../README.md)

---


A repeatable workload lives in [`load/workload.py`](../load/workload.py). One
workspace submits a burst of videos while the other submits single images, and
the three numbers the brief asks for are reported for the same workload before
and after the fix. Raw output for every run below is in
[`evidence/`](../evidence/).

```bash
# fairness, with a healthy provider
uv run --project api python load/workload.py \
  --videos 200 --images 20 --no-outage --provider-delay-ms 200 --label fixed-fairness

# the same workload through a 25-second provider outage
uv run --project api python load/workload.py \
  --videos 200 --images 20 --provider-delay-ms 200 --outage-s 25 --label fixed

# and against the broken build, for comparison
SCREENING_MODE=broken docker compose up -d api worker
uv run --project api python load/workload.py \
  --videos 200 --images 20 --provider-delay-ms 200 --outage-s 25 --label broken
docker compose up -d api worker          # back to fixed
```

`--provider-delay-ms 200` matters. A provider that answers instantly drains a
200-video burst faster than the other workspace can submit anything, so nothing
ever queues and the fairness question is never actually asked. Two hundred
milliseconds per call is optimistic for a real moderation API scoring a video.

The script reads `SCREENING_MODE` from `/healthz` and records it, so the two
runs cannot be mixed up. In `fixed` mode it exits non-zero if
`published_unchecked` or `explicit_published_in_unverified` came out non-zero,
so it can gate CI; in `broken` mode it reports those numbers without failing,
since being wrong is the point of that run.

### Before and after, same workload

220 items (200 videos from `ws_unverified`, 20 images from `ws_verified`)
through a 25-second outage:

| | broken | fixed |
| --- | --- | --- |
| **media published unchecked** | **13** | **0** |
| **provider calls per upload** | **1.555** | **1.114** |
| max provider calls for one item | 12 | 3 |
| **age of oldest waiting job** (end of run) | **54.1s** | **0.0s** |
| items decided | 220 / 220 | 220 / 220 |
| jobs still churning afterwards | 4 | 0 |

The broken build published 13 items with nothing behind them. Query the database
a minute later and that count has decayed towards zero, because its uncapped
retries eventually score some of those items and backfill `screened_at`. That is
worse, not better: the items were live and unscreened in the meantime, and
nothing in the system recorded that they had been. The fixed build's `0` holds
at every point during and after the run.

**On `1.114` calls per upload in the fixed run.** That is not a rule-8
violation. 21 items were called more than once because their first call *timed
out and returned nothing*, and a call that produced no result has to be retried.
Exactly one call per item ever *succeeded* — `provider_results` is keyed on
`media_id`, so a second successful score cannot be recorded. With a healthy
provider the figure is exactly `1.000`. The broken build's `1.555` is different
in kind: concurrent redeliveries of jobs whose first attempt was still running,
paying again for work it already had, twelve times over for one item.

### `pending` and `held` during an outage

At the 25-second mark of the fixed run, 18 items were `held` and 202 were still
`pending`, with `published_unchecked: 0`. That split is deliberate.

An item is held as soon as an attempt on it **fails** — one provider timeout,
about five seconds. Rule 5 applies from the first failure, not from the end of
the retry budget, so the item reads as `held` with a
`screening_unavailable:timeout` reason and appears in `GET /review` rather than
sitting invisibly in a queue for the minute the retries take. Retrying continues
underneath, and a later success overwrites the hold with the real verdict;
`screened_at` stays null throughout, so a provisional hold can never be mistaken
for a screened item.

The 202 `pending` items had not been attempted yet. Each failed attempt occupies
a consumer for the full client timeout, so a 25-second outage across four
consumers only gets through about twenty of them. Marking an item held before
anything has been tried would be claiming we asked and failed when we had not;
`pending` is the honest answer for work that is still queued.

Because an outage hold has no provider result behind it, **an admin cannot
approve it** — `POST /review/{id}/approve` answers `409` with
`{"reasons": ["never_screened"]}`. A borderline score is a judgement call; an
item with no score at all is not, and publishing it would be publishing
unchecked. The re-screening sweep deals with it when the provider returns.

One caveat: the burst videos in this workload are clean, so
`explicit_published_in_unverified` is 0 in both runs. The frame-truncation
symptom — a video explicit only in its final second, published in an unverified
workspace — is not exercised here. It is covered in
`tests/test_incident.py` and by hand in [TESTING.md](../TESTING.md) § *Part 4*.

### The burst does not starve the other workspace

Healthy provider, 200ms per call. The 20 images were submitted **after** all 200
videos were already queued:

| | n | median | p95 | max |
| --- | --- | --- | --- | --- |
| burst videos (`ws_unverified`) | 200 | 7.97s | 12.70s | 13.27s |
| steady images (`ws_verified`) | 20 | **3.38s** | 5.36s | **5.36s** |

Every image was decided by 5.4s while the burst ran on to 13.3s. Queued FIFO
they would have waited behind all 200 videos for the full ~13s. That gap is the
per-workspace round-robin dispatcher doing its job.

During the outage neither workspace is screened, which is correct — there is no
decision to be had from a provider that is down. What matters is that nothing is
published in the meantime, and that recovery needs no human: within 16 seconds
of the provider returning, all 220 items had been screened, every provisional
hold had been cleared, and the oldest waiting job was back to 0.

### A worker that dies after the provider has scored

The one window where a call can genuinely be paid for twice.
`CRASH_AFTER_SCORE` kills the worker between the provider answering and the
result being saved:

```bash
docker compose stop worker
# submit one item, then:
docker compose run --rm --no-deps -e CRASH_AFTER_SCORE=<media-id> worker
```

Immediately after the crash:

```
provider calls:        1        <- money spent
media status:          pending  screened=False
provider_results rows: 0        <- nothing saved
job_attempts:          no=1 state=in_flight
```

The item is `pending`, not published. The orphaned `in_flight` attempt with no
matching `provider_results` row is the signature of exactly this failure, and it
is why the attempt row is written *before* the outbound call.

After restarting the worker, the lease lapses and the job is reclaimed:

```
media status:          published  screened=True  frames_scored=3
provider calls:        2        <- one wasted call, the cost of the crash
provider_results rows: 1        <- still exactly one successful score
job_attempts:          no=1 in_flight, no=2 scored
published_unchecked:   0
```

**What this proves:** the recovery path works. Nothing is published unchecked,
the job is picked up again without intervention, and the stored-result ledger
still holds exactly one successful score.

**What it does not prove.** `os._exit` is a clean process kill: the kernel
closes the sockets, Postgres sees the connection drop and rolls back, and the
machine, clock and disk are all fine afterwards. A real crash is messier — a
host lost mid-write, a partitioned network where the provider's response is in
flight and the TCP connection neither completes nor fails for minutes, a
container killed by the OOM reaper mid-transaction, clock skew that makes a
lease look expired when its holder is still alive. The lease is wall-clock
based, so a severe clock jump between two workers could in principle let both
believe they hold the same job; the conditional `UPDATE` means only one of them
can commit a state change, but both could have called the provider by then.

Closing the wasted-call window properly needs the provider to accept an
idempotency key and dedupe server-side. The client already sends one
(`Idempotency-Key: <media_id>` in `screening/provider.py`); the fake provider
deliberately ignores it and counts every call, which is what makes the cost
visible here rather than hidden.

---

## Metrics

```bash
curl -s localhost:8000/_metrics | python3 -m json.tool
```

```json
{
  "media_total": 1,
  "media_by_status": {"blocked": 1},
  "published_unchecked": 0,
  "explicit_published_in_unverified": 0,
  "oldest_waiting_job_age_s": 0.0,
  "provider_results_stored": 1,
  "media_with_duplicate_calls": 0,
  "provider_calls_per_upload": 1.0
}
```

The two numbers that matter most:

- **`published_unchecked`** — media that is live with no provider decision
  behind it. Must always be 0. It is derived from `screened_at`, never from job
  state, because a finished job is not proof that anything was screened; that
  conflation is what hid the incident.
- **`explicit_published_in_unverified`** — the specific breach the incident
  produced. Must always be 0.

`provider_calls_*` come from the provider's own counters, which are global to
the provider process. Call `POST /_admin/reset` first if you want these to
describe only your own run — it zeroes the counters and returns the provider to
`ok`.

**`queue_depth._delayed` is not a backlog.** It counts entries scheduled in the
Redis delayed set, which includes the timeout guard placed against every
asynchronous callback. Submit a long video, let its callback land, and the guard
stays scheduled until its own deadline passes — so `_delayed` can read `2` while
`job_states` is `{"done": 2}` and nothing at all is waiting. The dispatcher drops
each guard when it fires and finds the job already finished, and the count
returns to zero on its own. **`oldest_waiting_job_age_s` is the number to trust
for backlog**, because it is derived from job state in Postgres rather than from
what is sitting in Redis.

---

## Following one item through the logs

Logs are JSON, one object per line, carrying `workspace_id`, `media_id`,
`job_id` and `attempt_no` wherever they are known.

```bash
# everything about one media item, across the api and the worker
docker compose logs api worker | grep '"media_id": "<id>"'

# just the screening lifecycle
docker compose logs -f worker | grep -E 'provider_call|retry_scheduled|held_no_decision|decision_applied'

# the events that should never appear in a healthy build
docker compose logs | grep -E 'timeout_treated_as_pass|review_failure_shown_as_empty|broken_mode_active'
```

Event names worth knowing: `media_accepted`, `provider_call_started`,
`provider_call_failed`, `retry_scheduled`, `scored_from_cache`,
`duplicate_delivery_dropped`, `decision_applied`, `held_no_decision`,
`outage_rescreen_queued`, `callback_rejected_bad_signature`.

---

