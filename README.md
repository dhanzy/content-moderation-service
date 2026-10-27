# Content moderation service

Screens uploaded and AI-generated media so that nothing prohibited reaches
anyone, nothing explicit reaches an unverified workspace, and nothing goes live
unchecked — including when the moderation provider is slow, down, or answers
twice.

Python 3.12 / FastAPI / PostgreSQL / Redis, with a fake moderation provider as a
separate service. Dependencies are managed with [uv](https://docs.astral.sh/uv/)
and locked in `uv.lock`.

---

## Status

**Working and verified:** all nine screening rules, prompt refusal, the signed
async callback path, workspace isolation, role checks, per-workspace fair
queueing, per-error-class retries, outage re-screening, and the incident
reproduction. 85 tests pass, and the 200-video burst, outage recovery and
worker-death scenarios are measured in
[Load, failure and recovery evidence](#load-failure-and-recovery-evidence).

**Not finished yet**, and called out honestly:

| Missing | Notes |
| --- | --- |
| No circuit breaker | During an outage each item is held only once its own attempt has failed, which costs one provider timeout each. After a run of consecutive failures the dispatcher should mark everything already queued as held immediately instead of paying a timeout per item — in the measured outage that left 202 items sitting at `pending` for a minute. The behaviour is correct (nothing publishes) but the queue is slower to tell the truth than it needs to be. |
| Migrations | Schema is created with `create_all` behind an advisory lock, not Alembic. Fine for this exercise, wrong for production. |
| The UI runs Vite's dev server | `web` serves through `bun run dev` rather than a built bundle behind a real web server, because `VITE_API_BASE` is baked in at build time and a prebuilt bundle could not be re-pointed by compose. A real deployment would build and serve the static output. |

**Time spent:** about nine hours, against the six to seven the brief suggests.
The service, the rules and the tests came in roughly inside that window; the
overrun is the load harness and the measured evidence below. I would rather hand
over numbers I have actually taken than claims about how it would behave.

---

## What went wrong, in one table

The five symptoms from the incident report, each reproducible in this repo
against both the pre-fix and the post-fix handler.

| Symptom | Root cause | Fix | Test |
| --- | --- | --- | --- |
| An explicit video went live in an unverified workspace | Only the first 8 frames of a video were scored, and the video turned explicit in its final second | Every frame is scored and the decision takes the **peak** score across all of them; a video that cannot be fully scored is held, never passed | `test_broken_misses_explicit_in_last_frame` / `test_fixed_blocks_explicit_in_last_frame_unverified` |
| Media published while the provider was down | A provider timeout was treated as a pass | A call that returns nothing is a failed attempt. The item is held from the **first** failure, `screened_at` stays null, and the sweep re-screens it on recovery | `test_broken_publishes_unchecked_on_timeout` / `test_fixed_holds_from_the_very_first_failure` |
| An AI generation of "a 16-year-old" was produced | Generation prompts were never screened at all | Prompts are refused before generation with `422`, on de-leeted text for word rules and untouched digits for age rules | `test_broken_lets_minor_prompt_through` / `test_fixed_refuses_minor_prompt` |
| The provider bill roughly doubled | A redelivered job ran alongside an attempt already in flight, paying twice for the same work | Jobs are leased with a single conditional `UPDATE`, and `provider_results` is keyed on `media_id` so a second successful score cannot be stored | `test_broken_pays_twice_for_a_redelivered_job` / `test_fixed_scores_once_under_concurrent_redelivery` |
| Moderators saw "Nothing to review" for a morning while 40 items waited | A failed review-queue request was rendered as an empty list | Three states as a discriminated union — loading, failed, genuinely empty — and a client that throws on any non-2xx, so a failure cannot arrive as data | `test_broken_shows_failure_as_empty_list` / `test_fixed_reports_review_failure_as_failure` |

The thread running through all five: **nothing is treated as screened unless a
provider result was stored for it.** `screened_at` is the only record of that,
and every metric derives from it rather than from job state. A finished job was
what the old code trusted, and that is what let the first two symptoms hide.

---

## Requirements

- Docker and Docker Compose (tested with Docker 28.5, Compose 2.40)
- `uv` 0.8+ — only needed to run the tests or the load harness from the host;
  the containers carry their own uv
- `bun` 1.3+ — only needed to work on the UI outside Docker

Nothing else. No Python or Node installation is required to run the stack.

---

## Quick start

```bash
docker compose up -d        # builds on first run, ~4 min cold
docker compose ps
```

All six of `db`, `redis`, `provider`, `api`, `worker` and `web` should be
`running`, with `db`, `redis` and `provider` reporting `(healthy)`.

```bash
curl -s localhost:8000/healthz
#   {"ok":true,"screening_mode":"fixed"}
```

Then open the review queue at **<http://localhost:5173>**.

To stop, and to wipe the database volume:

```bash
docker compose down          # stop
docker compose down -v       # stop and discard all data
```

Prefer to run the Python processes directly, with a debugger or `--reload`? See
[Running without Docker](#running-without-docker).

---

## Services and ports

| Service | Host port | In-network address | Purpose |
| --- | --- | --- | --- |
| `api` | **8000** | `api:8000` | The service under test |
| `provider` | **8001** | `provider:8001` | Fake moderation provider |
| `db` | 5432 | `db:5432` | PostgreSQL 16 |
| `redis` | 6379 | `redis:6379` | Queue backing store |
| `web` | **5173** | — | Review queue UI (React + TypeScript, bun) |

`8000` and `8001` are exactly as the brief specifies.

If something already holds 5432 or 6379 on your machine — another Postgres, a
dev Redis — change the host side of the mapping in `docker-compose.yml` (for
example `"55432:5432"`) and set `DATABASE_URL` or `REDIS_URL` in `api/.env` to
match. Nothing inside the Compose network changes: the services always reach
each other as `db:5432` and `redis:6379`.

---

## Running without Docker

Everything here is plain Python; Compose is a convenience, not a requirement.
Running the three processes directly is worth it for a debugger, for
`--reload`, or when you just want to watch one worker's log without
`docker compose logs` in the way.

You still need a **PostgreSQL 16** and a **Redis 7** from somewhere. Pick one of
the three options below, then use the same three commands to start the services.

This repository is **two separate uv projects** — `api/` and `provider/` each
have their own `pyproject.toml` and `uv.lock` — so each command runs from its
own directory.

### Option A — infrastructure in Docker, services native (recommended)

Keeps the database and queue disposable while the code you are working on runs
on the host. This is the path verified below.

```bash
# Only the two stateful services
docker compose up -d db redis
```

### Option B — everything native

No Docker at all. Install the servers, then create the role and database the
application expects:

```bash
# Debian / Ubuntu / Kali
sudo apt install postgresql redis-server
sudo systemctl enable --now postgresql redis-server

# macOS
brew install postgresql@16 redis
brew services start postgresql@16 && brew services start redis
```

```bash
sudo -u postgres psql -c "CREATE ROLE mod LOGIN PASSWORD 'mod';"
sudo -u postgres psql -c "CREATE DATABASE moderation OWNER mod;"
```

The defaults already expect 5432 and 6379, so nothing further is needed.

> Not exercised on the development machine, which carries the `psql` and
> `redis-cli` clients but no server packages. Options A and C were both run end
> to end; treat the install commands above as the standard recipe rather than
> something this repository has proven.

### Option C — a Postgres and Redis you already run

Point the environment at them. Any Postgres 16 with a database the role can
create tables in will do, and any Redis — the queue uses ordinary lists, sets
and a sorted set, with no modules or persistence requirements.

### Environment

**For options A and C with the default ports, there is nothing to configure.**
`app/config.py` already defaults to `localhost:5432` and `localhost:6379`, so
the services start with an empty shell.

To change anything, copy the template and edit it:

```bash
cp api/.env.example api/.env
```

`api/.env` is loaded automatically by `app/config.py`, so there is no need to
export anything or pass `--env-file`. It is loaded with **`override=False`**,
which means a variable already set in the real environment always wins — under
Compose every setting arrives that way, so the file can never quietly shadow
what `docker-compose.yml` says. The file is gitignored; `.env.example` is the
committed template and documents every available setting.

The database and its tables are created on startup, so there is no migration
step; an empty database is enough.

Two settings cause confusing failures when wrong, so they are worth knowing:

- **`CALLBACK_URL`** is read by the **provider**, not the API, and names where
  it posts asynchronous results. Under Compose it is `http://api:8000/...`; on
  the host it must be `http://localhost:8000/...`, or videos over 30 frames get
  a clean-looking `202` and then sit at `pending` forever. The provider is a
  separate uv project and does **not** read `api/.env`, so pass it on the
  command line (shown below).
- **`PROVIDER_SECRET`** must be identical for the provider and the API. If they
  differ, every callback is correctly rejected with a `401` and you get the same
  stuck-at-`pending` symptom from a different cause. Check for
  `callback_rejected_bad_signature` in the API log to tell the two apart.

### Start the three services

One terminal each, so you can read their logs. Keep the environment exported in
all three.

```bash
# 1. fake moderation provider  ->  :8001
#    CALLBACK_URL is required here: the provider does not read api/.env.
cd provider && CALLBACK_URL=http://localhost:8000/provider/callback uv run main.py
```

```bash
# 2. API  ->  :8000
cd api && uv run uvicorn app.main:app --port 8000 --reload
```

```bash
# 3. worker (dispatcher, reconciler, re-screening sweep)
cd api && uv run python -m app.workq.worker
```

```bash
# 4. the review UI  ->  :5173  (optional; the API works without it)
cd web && bun install && bun run dev
```

`uv run` resolves each project's locked dependencies into a local `.venv` on
first use, so there is no separate install step. `uv sync` up front if you would
rather not wait on the first command.

Verify:

```bash
curl -s localhost:8000/healthz   # {"ok":true,"screening_mode":"fixed"}
curl -s localhost:8001/_health   # {"ok":true,"mode":"ok"}
```

Then work through [Try it](#try-it) — the behaviour is identical, including the
asynchronous `202`-plus-signed-callback path for videos over 30 frames.

To reproduce the incident, start the API and worker with the flag set:

```bash
cd api && SCREENING_MODE=broken uv run uvicorn app.main:app --port 8000
cd api && SCREENING_MODE=broken uv run python -m app.workq.worker
```

### Tests

The suite needs Postgres, Redis and the provider reachable, and it always uses
its own `moderation_test` database and Redis DB 1 regardless of what
`DATABASE_URL` points at, so it never disturbs the data your running stack is
using.

```bash
cd api && uv run pytest -q      # 85 passed
```

It reads `api/.env` too, and derives its connection details from `DATABASE_URL`
and `REDIS_URL`, swapping in the test database and Redis index. Without either,
it falls back to the Compose host ports, which can be overridden on their own:

```bash
PG_HOST_PORT=55432 REDIS_HOST_PORT=16379 uv run pytest -q
```

### Stopping

There is no `compose down` for host processes. `Ctrl-C` in each terminal, or by
port:

```bash
fuser -k -n tcp 8000    # api
fuser -k -n tcp 8001    # provider
```

The worker holds no port; stop it with `Ctrl-C`, or find it with
`pgrep -af 'app.workq.worker'`.

### Do not run both at once

A containerised worker and a host worker pointed at the same database will both
consume from the queue. Nothing breaks — job leasing means each job still runs
exactly once, and the provider is still called once per item — but which worker
picks up a given job becomes a coin toss and the logs get hard to follow. Stop
the ones you are not using:

```bash
docker compose stop api worker provider
```

---

## Seeded identities

Created on startup. There is no login: the caller is identified by two headers.

| Workspace | Verified | Users |
| --- | --- | --- |
| `ws_verified` | yes | `admin_v` (admin), `member_v` (member) |
| `ws_unverified` | no | `admin_u` (admin), `member_u` (member) |

```
-H "X-User-Id: member_v" -H "X-Workspace-Id: ws_verified"
```

A user sending a workspace they do not belong to gets `403`, rather than being
silently scoped to their own.

---

## Media format

No real images and no classifier. A media item is a JSON document listing its
frames and the scores the provider will return for each. An image has one
frame; a video has one frame per second. A label left out of a frame scores 0.

```json
{
  "type": "video",
  "source": "generation",
  "prompt": "two adults on a beach at sunset",
  "frames": [{"explicit": 0.02}, {"explicit": 0.95, "self_harm": 0.1}]
}
```

`type` is `image` or `video`; `source` is `upload` or `generation`.

---

## Try it

Each block below is one of the incident symptoms, run against the live stack.

### A video that only turns explicit at the very end

Sixty frames, clean for fifty-nine of them, in an **unverified** workspace. Over
30 frames, so the provider scores it asynchronously and posts a signed result
back to the callback.

```bash
BODY=$(python3 -c 'import json; print(json.dumps({
  "type":"video","source":"upload",
  "frames":[{"explicit":0.01}]*59 + [{"explicit":0.95}]}))')

ID=$(curl -s -X POST localhost:8000/media \
  -H 'content-type: application/json' \
  -H 'X-User-Id: member_u' -H 'X-Workspace-Id: ws_unverified' \
  -d "$BODY" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')

sleep 3
curl -s localhost:8000/media/$ID \
  -H 'X-User-Id: member_u' -H 'X-Workspace-Id: ws_unverified'
```

```json
{"id":"...","status":"blocked","reasons":["explicit_in_unverified_workspace"],
 "explicit":true,"frames_total":60,"frames_scored":60,"screened":true}
```

`frames_scored: 60` is the point: every frame counted, not just the beginning.

### A workspace cannot see another workspace's media

```bash
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/media/$ID \
  -H 'X-User-Id: member_v' -H 'X-Workspace-Id: ws_verified'
# 404
```

`404` rather than `403`, so the response does not confirm the item exists
elsewhere.

### A generation prompt asking for a minor is refused

```bash
curl -s -X POST localhost:8000/media -H 'content-type: application/json' \
  -H 'X-User-Id: member_v' -H 'X-Workspace-Id: ws_verified' \
  -d '{"type":"image","source":"generation","prompt":"a 16-year-old girl",
       "frames":[{"explicit":0.1}]}'
# {"reasons":["prompt_refused:minor"]}   HTTP 422
```

No media row is created, so nothing is generated and no provider call is spent.

### An unsigned provider callback is rejected

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/provider/callback \
  -H 'content-type: application/json' -d '{"media_id":"whatever","frames":[]}'
# 401
```

### Each item is scored at most once

```bash
curl -s localhost:8001/_stats
# {"calls":{"<media id>":1}, "mode":"ok"}
```

---

## The review queue page

<http://localhost:5173> — React and TypeScript, dependencies managed with
[bun](https://bun.sh) and locked in `web/bun.lock`.

It does one job: show an admin the held media of their workspace, and let them
approve or reject it. The part that matters is that it tells **three states**
apart, which Required work 1 asks for explicitly:

| State | What you see | Why it is separate |
| --- | --- | --- |
| **loading** | grey panel, "Loading…" | A request in flight is not an answer |
| **failed** | red panel, "Could not load the queue", with the status and "this is **not** an empty queue" | The incident symptom: a failed request rendered as an empty list, so moderators saw "Nothing to review" for a morning while 40 items waited |
| **empty** | green panel, "Nothing to review — the queue loaded successfully and no media is currently held" | Genuinely nothing waiting |

In the code these are a discriminated union rather than an `items` array beside
a `loading` boolean and an `error` string:

```ts
type QueueState =
  | { kind: "loading" }
  | { kind: "failed"; status: number | null; message: string }
  | { kind: "ready"; items: HeldItem[] };
```

There is no combination of flags that can render a failure as an empty list,
because a failure carries no items at all. "Empty" is `ready` with nothing in
it, and the compiler keeps the two apart. `fetchReviewQueue` throws on any
non-2xx, so a failure cannot reach the UI looking like data.

To see the failed state for yourself:

```bash
REVIEW_FAIL=1 docker compose up -d api     # GET /review now answers 503
docker compose up -d api                   # back to normal
```

The page also has buttons to create sample media — a held item, a blocked one, a
clean one, the 60-frame video that is explicit only at the end, and a generation
prompt that must be refused — so the queue can be demonstrated without reaching
for `curl`. Switching identity in the dropdown shows the role rules: as
`member_v` the queue answers `403` and renders as a failure, which is correct.

An item showing `screened: no` is held because screening was unavailable rather
than because a human needs to judge it. It has no provider result behind it, so
approving it is refused with `409 never_screened`.

---

## API reference

| Route | Who | Returns |
| --- | --- | --- |
| `POST /media` | any member | `202` `{"id","status":"pending"}`; `422` `{"reasons"}` when a generation prompt is refused |
| `GET /media/{id}` | members of the owning workspace | `200` `{"id","status","reasons",...}`; `404` for anyone else |
| `GET /review` | admins | `200` with the held media of their workspace; `503` when the queue cannot be read |
| `POST /review/{id}/approve` | admins | `200`; `403` for members; `409` if not held or if an absolute rule forbids it |
| `POST /review/{id}/reject` | admins | `200`; `403` for members |
| `POST /provider/callback` | the provider | `200`; `401` without a valid signature |
| `GET /_metrics` | ops | counts, unchecked publishes, oldest waiting job |
| `POST /_admin/reset` | ops | clears media, jobs and queue; resets the provider's counters |
| `GET /healthz` | ops | liveness and active screening mode |

Provider control, used by the checks:

| Route | Returns |
| --- | --- |
| `POST localhost:8001/_control` `{"mode":"ok"\|"slow"\|"timeout"\|"error","delay_ms":0}` | sets provider behaviour |
| `GET localhost:8001/_stats` | `{"calls":{"<media id>":1}}` |

`mode` also accepts `rate_limit` and `malformed`, used by our own tests to
exercise 429 and unparseable-body handling. Interactive docs are at
<http://localhost:8000/docs> and <http://localhost:8001/docs>.

---

## Driving the provider

```bash
# make scoring fail
curl -s -X POST localhost:8001/_control \
  -H 'content-type: application/json' -d '{"mode":"error"}'

# make it hang (this is the outage; a request will block for 600s)
curl -s -X POST localhost:8001/_control \
  -H 'content-type: application/json' -d '{"mode":"timeout"}'

# make it slow but working
curl -s -X POST localhost:8001/_control \
  -H 'content-type: application/json' -d '{"mode":"slow","delay_ms":3000}'

# back to normal — the worker notices and re-screens anything held by the outage
curl -s -X POST localhost:8001/_control \
  -H 'content-type: application/json' -d '{"mode":"ok"}'
```

While the provider is in `timeout` or `error`, an upload is **held as soon as an
attempt on it fails**, stays `pending` until something has actually been tried,
and is **never published** either way. The distinction is deliberate and is
measured in
[`pending` and `held` during an outage](#pending-and-held-during-an-outage):
marking an item held before anything has been tried would be claiming we asked
and failed when we had not.

When the provider returns to `ok`, the worker's re-screening sweep picks up
everything that was held because screening was unavailable and runs it again
without waiting for a moderator.

---

## Reproducing the incident

The pre-fix behaviour is kept in the tree rather than described, behind one
environment variable:

```bash
SCREENING_MODE=broken docker compose up -d api worker
```

In that mode the service deliberately: treats a provider timeout as a pass,
scores only the first 8 frames of a video, never checks generation prompts, lets
a redelivered job run alongside an attempt already in flight, and renders a
failed review-queue request as an empty list. Both `api` and `worker` log
`broken_mode_active` at ERROR on startup so a broken build cannot be mistaken
for a healthy one.

Back to the fixed handler:

```bash
docker compose up -d api worker     # SCREENING_MODE defaults to "fixed"
```

Each symptom has a matching pair of tests — one proving the broken handler
reproduces it, one proving the fix prevents it — in
`api/tests/test_incident.py`, so the before-and-after is reproducible without
redeploying.

---

## Running the tests

The stack must be up first: the suite uses the real Postgres, Redis and fake
provider. It creates its **own** database (`moderation_test`) and uses Redis DB
1, so it never touches the running stack's data and never competes with the
worker for jobs.

From the host:

```bash
cd api
uv sync                 # first time only
uv run pytest -q
# 85 passed
```

Or inside the container, with no host toolchain at all:

```bash
docker compose exec api pytest -q
# 85 passed
```

Useful selections:

```bash
uv run pytest tests/test_rules.py -q        # pure rule logic, no I/O
uv run pytest -m incident -q                # just the broken-handler symptoms
uv run pytest -q -k callback                # signature handling
uv run pytest -v                            # see every test name
```

What the files cover:

| File | Covers |
| --- | --- |
| `tests/test_rules.py` | the decision rules as a pure function: thresholds, precedence, every prohibited label, whole-video scanning |
| `tests/test_incident.py` | the five incident symptoms, each asserted against both the broken and the fixed handler |
| `tests/test_access.py` | workspace isolation (rule 1) and admin-only review (rule 6) |
| `tests/test_callback.py` | callback signing (rule 7), duplicate and partial results |

To exercise the same ground by hand with `curl` instead, see
[TESTING.md](TESTING.md) — one scenario per rule, plus the outage, recovery and
fairness behaviour, and a side-by-side run of the broken and fixed builds.

---

## Load, failure and recovery evidence

A repeatable workload lives in [`load/workload.py`](load/workload.py). One
workspace submits a burst of videos while the other submits single images, and
the three numbers the brief asks for are reported for the same workload before
and after the fix. Raw output for every run below is in
[`evidence/`](evidence/).

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
`tests/test_incident.py` and by hand in [TESTING.md](TESTING.md) § *Part 4*.

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

## Part 3 — cost, evasion and scale

### Paid API, own models, or a mix

A mix — and the split is decided by **who can legally or practically hold the
data**, not by price. Price only settles the one commodity category.

| Category | Where | Why |
| --- | --- | --- |
| explicit / nudity grading | paid API | Commodity, better calibrated than anything I would train, and at this volume the bill is noise. |
| apparent age (`minor`) | paid API **plus a second, independent model** | The highest cost of error in the system. One model's confidence is not evidence; disagreement between two holds the item. |
| known CSAM | specialist hash-matching service, only | The hash lists are not distributable. Self-hosting is not an option even in principle — see below. |
| `sexual_violence`, `bestiality`, `self_harm` | paid API labels where the vendor publishes them, otherwise a fine-tuned open classifier | Rare enough that per-call price is irrelevant; recall matters more than cost. |
| `real_person` / likeness | **self-hosted** | This is matching a face against the roster of performers who have consented, and that roster is our own private data. No vendor has it, and shipping it to one creates a worse problem than it solves. |
| generation prompts | own rules plus an LLM classifier | Text, pre-generation, fractions of a cent. Already here as `screening/prompts.py`. |

The principle: **buy the commodity classifiers, host what depends on our own
private data, and never host the thing whose input data we are not allowed to
hold.**

### Rough monthly cost for 10,000 images and 1,000 one-minute videos

**Frames are the unit, not items.** At one frame per second, 1,000 one-minute
videos are 60,000 frames against the images' 10,000. The videos are 9% of the
uploads and 86% of the bill.

| | Items | Frames at 1 fps |
| --- | --- | --- |
| images | 10,000 | 10,000 |
| one-minute videos | 1,000 | 60,000 |
| **total** | **11,000** | **70,000** |

Unit prices move and differ by vendor, so treat the rate as the assumption and
the arithmetic as the point. Commodity image moderation sits in the region of
**$0.50–$2.00 per 1,000 frames**; $1.00 is a fair working figure.

| Line | Volume | Rate | Monthly |
| --- | --- | --- | --- |
| cheap pass, every frame | 70,000 frames | $1.00 / 1k | **$70** |
| second opinion on `minor`, only for frames the first model does not call confidently adult (~15%) | 10,500 frames | $1.00 / 1k | **$11** |
| CSAM hash lookups | 11,000 items | often free to qualifying platforms | **$0–$500** |
| likeness matching, self-hosted | 70,000 frames | own CPU | **~$20** |
| prompt screening (LLM, text) | ~3,000 generations | ~$0.0002 each | **~$1** |
| **inference total** | | | **~$100 / month** plus whatever the hash service charges |

About **one cent per upload**. Dedupe on a perceptual hash before billing
anything — consecutive near-identical frames of a static scene collapse to one —
and 60,000 video frames typically bill as 30–40,000, taking it nearer $70.

**But the inference bill is not the cost of this system.** One moderator-hour is
roughly the entire monthly model spend. At 11,000 items a month, a 3% hold rate
is 330 human reviews, about eight hours of work:

> The lever worth optimising is the **hold rate**, not the price per frame.

Which is why the re-screening sweep earns more than it looks like it does. Items
held because screening was *unavailable* resolve themselves when the provider
returns and never reach a person. In the measured outage that was 18 items a
sweepless queue would have handed to a moderator for no reason at all.

It is also why I would not self-host at this volume. One always-on GPU is
$400–$700 a month before any engineering; the API bill is $70. Self-hosting
becomes the right answer at roughly **500,000 frames a month**, about seven times
this volume, and even then I would move the cheap pass first and keep paying for
the second opinion on `minor`.

### How many frames to sample

**1 fps is the floor for the prohibited categories, and the incident is the
argument.** That video was explicit *only in its final second*. Sampling one
frame every five seconds would have had a one-in-five chance of catching it, and
a 20% chance of missing prohibited content is not a cost saving.

1 fps does not scale to a 60-minute video (3,600 frames), so it is adaptive:

1. **Cheap pass over every frame at 1 fps.** Self-hosted, so the per-frame cost
   is near zero and "every frame" is affordable.
2. **Perceptual-hash dedupe.** Collapse near-identical consecutive frames. Safe,
   because it keys on the content being the same rather than on a frame's
   position in the file.
3. **Paid shortlist.** Scene-change keyframes, every frame the cheap pass scored
   above a low threshold, and a fixed random sample on top, so that a blind spot
   in the cheap model is not a blind spot in the whole pipeline.
4. **Never a fixed cap.** "The first N frames" is symptom 2 of this incident. If
   the budget cannot cover an item, the item **holds** — it does not pass. That
   is already enforced by `decide([])` returning a hold rather than a publish.

The rule underneath: the frame rate is a **recall** decision and the model tier
is a **cost** decision. Trading recall for cost is how the incident happened.

### Catching prompts that evade the check

`screening/prompts.py` catches literal and lightly obfuscated phrasing today —
and it is where I found the worst bug in this build. Normalising leetspeak turned
`16-year-old` into `i6-year-old`, so the exact incident phrase was **allowed by
the code written to stop it**. That is the shape of this whole problem: every
layer of cleverness adds a place for a rule to go quietly dead. Production is
five things, in descending order of what they buy:

**1. Screen the output, not just the prompt.** This is the one that matters. A
clean prompt can produce a prohibited image, by accident or by using a model or
LoRA that skews young without being asked to. Prompt screening is a cheap
pre-filter that saves generation cost and gives the user an immediate, specific
refusal; **the image screen is the control.** It also removes the incentive —
getting a prompt past the filter buys nothing when the result is screened
anyway. A prompt-only system is one a determined user eventually beats.

**2. Screen the concatenation of everything the user controls.** Splitting across
fields works precisely because each field is checked alone. Prompt, negative
prompt, title, caption, tags, filename, character name, LoRA name and the
preceding turns of a chat-style generation UI all join into one string, and the
rules run on that. `"a girl"` in one field and `"age: 16"` in another has to read
as one sentence.

**3. Normalise in layers, and run each rule on the layer it needs.** NFKC, fold
Unicode confusables (Cyrillic `а`, dotless `ı`), strip zero-width and combining
marks, collapse repeated characters — and keep the digits-intact text alongside
the de-leeted one, because age rules need the digits and word rules need the
de-leeting. Beyond leetspeak: word numbers (`sixteen`, already handled),
ordinals, indirect ages (`turns 17 next month`, `class of 2012`, a birth year),
and the euphemism vocabulary — which has to be maintained as data with an owner
and a review cadence, not invented once and left to rot.

**4. An LLM classifier over the joined text.** Pattern matching cannot reach
paraphrase, translation, or a description that states no age at all. This is
cheap and it is the only thing that catches a sentence built specifically to
avoid every keyword. It does not replace the rules — those are deterministic,
testable, fast, and what I would defend in an audit — it runs after them and may
only escalate, never clear.

**5. Measure it, and treat the account as a signal.** A red-team corpus in the
repo, run in CI, with a recorded false-negative rate. I have none today, which is
stated in [AI.md](AI.md); "I have only tried the cases I thought of" is not a
safety claim. Separately: five refusals in an hour from one account is
information about the *account*, not about five prompts. Rate-limit, then
escalate the account for human review. Someone iterating against the filter is
the cheapest signal available, and this build throws it away.

### Adding CSAM hash matching

**Where it goes: first, and before anything becomes durable.** On upload, before
generation, before the scoring provider, before the file is stored anywhere it
could be served from. Compute the hashes the licensed list requires (PhotoDNA,
PDQ, MD5) and query the matcher.

**On a match, this is not an ordinary `blocked`:**

- The item goes to restricted quarantine storage and is **preserved, not
  deleted** — deletion destroys evidence and is itself an offence in some
  jurisdictions.
- The account is locked, not warned.
- A report goes to NCMEC, or the local equivalent, inside the statutory window.
- **No moderator review and no admin override.** There is nothing to judge.
  `POST /review/{id}/approve` refuses the category outright, the way it already
  answers `409` for the absolute rules.
- The match record is itself sensitive: hashes do not go into application logs,
  and quarantine access is audited.

**What it does not do.** Hash matching finds *known* material. A novel image
hashes to nothing, and everything a text-to-image model produces is novel by
definition. So it runs *alongside* the apparent-age classifier and never instead
of it — the classifier is the only control that reaches new material, and prompt
refusal the only one that reaches it before it exists. Treating a hash miss as a
clean bill of health would be the same class of error as treating a timeout as a
pass.

**When the service is down: hold, never publish.** The same rule as every other
screening dependency, and the architecture already does this. A failed call is a
failed attempt; the item is held with `screening_unavailable:csam_hash`,
`screened_at` stays null, retries continue, and approve is refused with
`never_screened`, because no human can clear a check that never ran.

Two deliberate differences from the scoring provider being down:

- **A moderator cannot defer this one.** A normal held item is something a human
  can look at and decide. This is a check with no human equivalent, so the item
  simply waits.
- **New generations are rejected outright rather than queued.** Accepting uploads
  and holding them is fine — that backlog drains on recovery. But *generating*
  media we cannot check is manufacturing unscreened inventory, so generation
  returns an error while the matcher is down. Holding is the safe degradation for
  work already in flight; it is not a licence to create more of it.

### At one million uploads a day, what breaks first

At the same 90/10 mix, 1M uploads/day is ~11.6 items/s average and perhaps 40/s
at peak — but roughly **7 million frames a day**, ~80/s average and ~300/s peak.
In the order things actually fall over:

**1. The Redis fair queue — and not on throughput.** One Redis handles these
operations at this rate without noticing. It breaks on two other things: the
round-robin dispatcher walks the set of currently-active workspace lists on every
pop, so with tens of thousands of simultaneously active workspaces that scan
dominates the pop; and one Redis is a single failure domain holding the entire
backlog. Fix: shard on `hash(workspace_id)`, one Redis and one worker pool per
shard, so the round robin only ever covers the workspaces in its own shard.
Fairness inside a shard is unchanged, and fairness across shards comes from
giving each shard equal capacity. This is the first real piece of work, and
nothing in the current build is sharded.

**2. Postgres write amplification — and the safety metrics before it.** Per item:
a media row, a job, N attempt rows, a result row, and the status updates. Five
million small writes a day is not frightening in itself, but `job_attempts` and
`provider_results` grow without bound, and `published_unchecked` is a **query** —
instant over a thousand rows, minutes over a hundred million. The two numbers
that prove safety becoming too slow to watch is a safety problem, not a
performance one. Fix: partition by day, expire attempts after a week, and
maintain `published_unchecked` as an incrementally-updated counter rather than a
scan. Then shard the write path by workspace, because one primary has one writer.

**3. The reconciler's polling sweep.** It looks for expired leases and outage
holds every few seconds — free at this size, crushing at a hundred million rows.
It needs an index on `(state, lease_expires_at)` and bounded batches. Cheap
enough that I would add it before it hurts rather than after.

**4. The provider bill, which flips the build-versus-buy answer.** 7M frames a
day at $1 per 1,000 is ~$7,000 a day, north of $200,000 a month. Two or three
GPUs serve 300 frames/s of a cheap classifier for a couple of thousand. The
crossover sits far below this point, so by the time you are here the cheap pass
is self-hosted, the paid API is reserved for the shortlist and the second opinion
on `minor`, and the cost conversation is about GPU utilisation rather than
per-call pricing.

**5. Moderators — which is really the answer.** A 3% hold rate on 1M items a day
is **30,000 human reviews a day**: around 750 person-hours, something like a
hundred moderators per shift. No amount of engineering moves that number; only a
lower hold rate does, which means better calibration and automatic resolution of
everything held for "screening unavailable" rather than for a genuine judgement
call. Engineering scales the pipeline. The pipeline's output is human work, and
in practice that is the constraint that binds first.

**And the honest answer about this build specifically:** it is single-node
correct and multi-node *ready* — Postgres is the source of truth and leases are
conditional updates, so a second worker is safe — but I have only ever run one
worker process against one Redis. The sharding above is reasoned, not measured,
and that is stated in [AI.md](AI.md).

---

## A short production plan

### The first fifteen minutes

Mitigate before diagnosing. The safe degradation is a backlog; the unsafe one is
publishing.

1. `GET /_metrics`. `published_unchecked` and `explicit_published_in_unverified`
   must both be 0. **Non-zero is a breach, not a bug** — media is live that
   nothing screened, and that is handled with a containment step, not a ticket.
2. **Stop publishing.** Today the blunt version is `docker compose stop worker`:
   nothing is screened, nothing is published, items accumulate at `pending`, and
   Postgres keeps every one of them. A real deployment wants this as a flag, so
   ingest keeps working while the decision path is frozen.
3. `oldest_waiting_job_age_s`, and `media_by_status` for the shape. A sudden
   **collapse** in `held` is as alarming as a spike — checks silently not running
   is how this incident hid.
4. The never-events:
   `docker compose logs | grep -E 'timeout_treated_as_pass|review_failure_shown_as_empty|broken_mode_active'`.
   Any hit names the fault immediately.
5. `curl -s localhost:8001/_health` and the provider's own error rate, to tell
   "our bug" from "their outage". Those have different fixes and only one of them
   is ours.

### Rolling the fix out

The screening handler is selected by `SCREENING_MODE`, so a fix ships as a new
handler with the previous one still selectable — **rollback is a variable, not a
redeploy.** `api` and `worker` deploy separately, so the decision path can be
rolled without touching ingest. One shard at a time, watching
`published_unchecked` and the per-shard held rate between each.

### When to roll back

Decided in advance, so nobody is making the call while the fire is burning:

- `published_unchecked > 0` at any point, or
- `explicit_published_in_unverified > 0` at any point, or
- the held rate moves by more than 2x in **either** direction — a collapse means
  the checks stopped running, which is this incident exactly.

Any one of those, roll back. No discussion, no "let's watch it another ten
minutes".

### Verifying recovery

"The errors stopped" is not recovery. The window itself has to be re-examined:

1. **Re-derive the verdicts.** Every item screened during the window has stored
   provider scores. Re-run the decision function over those scores and compare
   against the item's live status; any disagreement is an item published on a
   wrong verdict, and it gets pulled. This is only possible because the raw
   scores are kept rather than just the conclusion.
2. **`published_unchecked = 0` sustained**, not sampled once. In the broken build
   that number decays towards zero on its own as late retries backfill
   `screened_at` — which is exactly how a breach hides from a single check.
3. **`oldest_waiting_job_age_s` back to baseline, and the held backlog drained by
   the re-screening sweep rather than by hand.** That last clause is the real
   test: recovery means no human had to do anything.

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

## Configuration

Set on the `api` and `worker` services in `docker-compose.yml`, or in `api/.env`
for host runs (see [`api/.env.example`](api/.env.example)). A real environment
variable always takes precedence over the file.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCREENING_MODE` | `fixed` | `broken` reproduces the incident |
| `MAX_ATTEMPTS` | `5` | retry budget per job before it is held |
| `LEASE_SECONDS` | `30` | how long a worker holds a job before it can be reclaimed |
| `WORKER_CONCURRENCY` | `4` | consumer tasks per worker process |
| `PROVIDER_TIMEOUT_S` | `5.0` | client timeout on a provider call |
| `PROVIDER_SECRET` | `dev-provider-secret` | HMAC key for callback signatures |
| `REVIEW_FAIL` | unset | fault injection: makes `GET /review` fail, to exercise the failed-vs-empty distinction |
| `CRASH_AFTER_SCORE` | unset | fault injection: a media ID, or `*`, to kill the worker after scoring but before saving |

---

## Project layout

```
docker-compose.yml        all services
provider/                 fake moderation provider (own uv project)
  main.py                 echoes frame scores, counts calls, fails on demand
api/                      the service (own uv project)
  .env.example            template for local overrides (copy to .env)
  app/
    main.py               FastAPI app and startup
    config.py             settings, and the .env loader
    models.py             schema
    deps.py               caller identity from headers
    routes/
      media.py            POST /media, GET /media/{id}
      review.py           GET /review, approve, reject
      callback.py         signed provider callbacks
      internal.py         /_metrics, /_admin/reset
    screening/
      rules.py            the decision rules, pure
      prompts.py          generation-prompt refusal
      provider.py         provider client, error classification, retry policy
      apply.py            storing a result and applying a verdict
      fixed.py            the real handler
      broken.py           the deliberately broken handler
    workq/
      fairqueue.py        per-workspace round-robin Redis queue
      leases.py           job leasing, so one job runs once
      worker.py           dispatcher, reconciler, re-screening sweep
  tests/
web/                      review queue UI (own bun project)
  src/api.ts              API client; throws on non-2xx so a failure is never data
  src/App.tsx             the page, and the three-state union
```

---

## Troubleshooting

**`port is already allocated` on startup.** Something else holds one of 5432,
6379, 8000 or 8001. Find it with
`ss -ltnp | grep -E ':(5432|6379|8000|8001)'`. For Postgres and Redis, remap the
host side in `docker-compose.yml` and set `DATABASE_URL`/`REDIS_URL` in
`api/.env` to match.

**`address already in use` when starting a host process.** A container is
probably still holding the port. `docker compose ps`, then
`docker compose rm -sf api provider worker`. Worth checking carefully: a
containerised provider left running will answer `/_health` and `/score` while
posting its callbacks to `api:8000`, which does not exist outside the Compose
network — so long videos stay `pending` and it looks like a code problem.

**`ConnectionRefusedError` / `Connection refused` on startup.** The database or
Redis is not running, or is on a different port than the service expects.
`docker compose up -d db redis`, and check `DATABASE_URL`/`REDIS_URL` against
the host ports in `docker-compose.yml`.

**The worker container keeps restarting.** `docker compose logs worker`. It has
`restart: unless-stopped` so a transient startup failure heals itself; a
persistent crash loop is a real error.

**Tests fail with `fake provider not reachable`.** The suite skips rather than
lying when the provider is down. Start the stack first.

**Tests fail with `attached to a different loop`.** The Redis client and the
database engine bind to the event loop that created them, and pytest gives each
test a fresh loop. The `db` fixture drops both between tests; if you add a
fixture that opens either one, it has to do the same.

**Uploads sit at `pending` forever.** The worker is not running
(`docker compose ps`), or the provider is in `timeout` mode — check
`curl -s localhost:8001/_health`.

**A `422` you did not expect.** Generation prompts are screened before anything
else happens. The response body lists the categories in `reasons`.
