# Running without Docker

Three ways to run the stack without `docker compose up`, plus every
configuration variable and the failures worth knowing about. The containerised
path in the [README](../README.md#quick-start) needs none of this.

[← back to the README](../README.md)

---


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

Then work through [Try it](../README.md#try-it) — the behaviour is identical, including the
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

## Configuration

Set on the `api` and `worker` services in `docker-compose.yml`, or in `api/.env`
for host runs (see [`api/.env.example`](../api/.env.example)). A real environment
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
