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
reproduction. 82 tests pass.

**Not finished yet**, and called out honestly:

| Missing | Notes |
| --- | --- |
| `web` service (the one-page review UI) | The `web` service is declared in `docker-compose.yml` but has no source yet, so **plain `docker compose up` fails at that build**. Use the explicit service list below. The three states it must distinguish (loading / failed / empty) are already enforced and tested at the API layer. |
| Load evidence | The 200-video burst workload, the worker-death demonstration, and the before/after metrics table are not yet recorded. |
| Part 3 answers | Cost and scaling write-up still to come. |
| Migrations | Schema is created with `create_all` behind an advisory lock, not Alembic. Fine for this exercise, wrong for production. |

---

## Requirements

- Docker and Docker Compose (tested with Docker 28.5, Compose 2.40)
- `uv` 0.8+ — only needed to run the tests from the host; the containers carry
  their own uv

Nothing else. No Python or Node installation is required to run the stack.

---

## Quick start

```bash
# 1. Build the two application images (~3 min cold, mostly dependency install)
docker compose build provider api

# 2. Start the backend
docker compose up -d db redis provider api worker

# 3. Confirm it is alive
curl -s localhost:8000/healthz
#   {"ok":true,"screening_mode":"fixed"}
```

Check everything came up:

```bash
docker compose ps
```

All five of `db`, `redis`, `provider`, `api`, `worker` should be `running`, with
`db`, `redis` and `provider` reporting `(healthy)`.

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
| `web` | 5173 | — | Review UI (not implemented yet) |

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
cd api && uv run pytest -q      # 82 passed
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

While the provider is in `timeout` or `error`, uploads are **held**, never
published. When it returns to `ok`, the worker's re-screening sweep picks up
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
# 82 passed
```

Or inside the container, with no host toolchain at all:

```bash
docker compose exec api pytest -q
# 82 passed
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
web/                      review UI (not implemented yet)
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
