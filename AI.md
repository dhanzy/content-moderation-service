# AI use

## Tools

I used Claude Code (Opus) as the main driver for this task, in an agentic setup
where it could edit files and run commands directly. I used it for most of the
implementation and the tests. I directed the structure, made the architectural
calls, and verified the behaviour myself by running the stack.

I did not use it to decide anything about the screening rules. Those came
straight from the brief, and I checked each one against the text as I went.

## What I decided, rather than accepted

A few things I specified up front or changed afterwards:

- **uv for dependency management**, with `pyproject.toml` and a committed
  `uv.lock` per project, and the uv binary copied from its published image in
  the Dockerfiles rather than pip-installed, so the build does not depend on
  resolving its own tooling. `api/` and `provider/` are separate projects
  because they are separate services.
- **The broken handler stays in the tree** behind `SCREENING_MODE=broken`,
  rather than living in git history. The brief asks for the incident to be
  reproduced first; keeping it switchable means every symptom gets a pair of
  tests, and I can reproduce it on the call with one environment variable.
- **A hand-rolled queue instead of Celery or arq.** The requirement is that a
  200-video burst must not starve another workspace, and fairness has to live in
  the dispatcher. A library pulling FIFO from one list cannot do that. I took on
  reconciling Redis against Postgres as the cost, and kept Postgres as the
  source of truth so losing Redis costs latency rather than correctness.
- **`screened_at` as the only record of whether something was screened.** The
  brief warns not to treat a finished job as proof. All metrics derive from this
  column, never from job state.
- **Standard host ports** for Postgres and Redis. The first version published
  them on 55432/56379 to dodge a collision on my machine; I changed it back
  because a reviewer should not have to decode unusual ports, and documented the
  remap instead.
- **`.env` auto-loading via python-dotenv**, with `override=False` so a real
  environment variable always wins. I wanted the services to start from an empty
  shell locally without that ever being able to shadow what
  `docker-compose.yml` says in a container.

## Incorrect suggestions I caught

These are the ones that mattered. All were found by running the code, not by
reading it.

1. **The age check was silently dead.** The prompt screener normalises leetspeak
   so that `m1nor` is caught, mapping `1` to `i`. That same pass turned
   `16-year-old` into `i6-year-old`, so `"a 16-year-old girl"` was **allowed** —
   the exact incident symptom, reintroduced by the code meant to prevent it.
   Word rules now run on the de-leeted text and age rules on the digits
   untouched. I added word-numbers too (`sixteen year old`), with word
   boundaries so `eighteen`, `canteen` and `eight hundred` do not trip.

2. **`decide([])` returned `published`.** An empty frame list meant "nothing
   wrong" instead of "nothing checked". That is a fail-open default sitting
   directly under the rule that says never publish unchecked. It now holds.

3. **The test fixture would have truncated the live database.** The suite set
   its database URL with `setdefault`, but inside the `api` container
   `DATABASE_URL` is already set — so running the tests there would have wiped
   real data and fought the worker for jobs. It now forces its own
   `moderation_test` database and Redis index regardless of the environment. I
   confirmed afterwards that live data survives a container test run.

4. **`python-dotenv` was only a transitive dependency** of
   `uvicorn[standard]`, while `app/config.py` imported it directly. That breaks
   the day uvicorn drops the extra. Declared it properly.

5. **`.env` was not gitignored** while being auto-loaded — and this repository
   ships with `.git`. Fixed, with `.env.example` as the committed template.

6. **The api and worker raced on `CREATE TABLE`** at startup and the worker died
   on a duplicate `pg_type` entry; `create_all`'s `checkfirst` is not atomic
   across processes. Serialised behind a Postgres advisory lock, with upserts
   for seeding.

7. **A module-level Redis client leaked across event loops** in the test suite,
   failing with "attached to a different loop". Each test gets a fresh loop, so
   the fixture now drops the client and disposes the engine between tests.

8. **A leftover containerised provider gave me a false result.** It answered
   `/_health` and `/score` normally while posting callbacks to `api:8000`, a
   name that does not resolve outside the Compose network, so long videos sat at
   `pending` and it looked like a code bug. I had also reported a native startup
   as working when the host provider had actually failed to bind behind that
   container. Re-ran from a clean slate and documented the trap in the README
   troubleshooting section.

The pattern in most of these is the same: the generated code was plausible and
wrong at a boundary — empty input, a second process, a shared singleton, a
normalisation step applied too widely. Reading it would not have caught them.

## What I verified

- All nine rules end to end against the running stack, with `curl`, in both the
  containerised and the local uv setup. The 60-frame video that is explicit only
  in its final second is `blocked` with `frames_scored: 60` in an unverified
  workspace, and `published` in a verified one.
- The asynchronous path for real: a `202`, then the provider's HMAC-signed
  callback applied to the item. Including on the host, where the callback has to
  reach `localhost` rather than a container.
- Exactly one provider call per upload, checked against the provider's own
  counters, including under four concurrent deliveries of the same job and after
  forcing a job back to `queued` once it had already succeeded.
- 82 tests passing, from the host and inside the `api` container.
- That compose environment beats `api/.env`, by setting a sentinel value and
  watching it win.

## What I have not verified

- **The load and recovery evidence is missing.** The 200-video burst workload,
  the worker-death demonstration and the before-and-after metrics table are not
  recorded. The fairness, retry and re-screening code is written and the
  mechanisms are tested individually, but I have not run the workload or
  produced the numbers. This is the largest gap and I ran out of time on it.
- **The review UI does not exist.** The three states it must distinguish —
  loading, failed, genuinely empty — are enforced and tested at the API layer
  (`503` versus `200 []`), but nothing renders them. `docker compose up` fails
  on that service; the README says so and gives the working service list.
- **The fully-native install path is untested.** My machine has the `psql` and
  `redis-cli` clients but no server packages, so the `apt install` instructions
  in the README are the standard recipe rather than something I proved.
- **Prompt screening is a pattern matcher and I have not measured it.** It
  catches literal and lightly obfuscated phrasing. It will miss paraphrase,
  translation and novel slang, and I have no false-negative rate for it. I have
  only tried the cases I thought of. The production answer is in part 3 of the
  README.
- **The crash-injection test proves less than it looks.** Killing the worker
  with `os._exit` after the provider has scored an item but before the result is
  saved exercises my own recovery path, but it does not reproduce a real
  machine loss: no torn writes, no partial network state, no clock skew. The
  one window where a call can genuinely be paid for twice is still there, and I
  describe it rather than claim to have closed it.
- **No migrations.** The schema is created with `create_all`. Fine here, wrong
  for production.
- **Part 3 of the README is not written.** The cost and scaling answers are
  outstanding.

## What I can explain

Everything in the submission. The parts I would single out as worth asking me
about are the exactly-once mechanism and the one window it cannot close, why the
queue is hand-rolled, and why `screened_at` rather than job state is the only
thing I trust to answer "was this actually screened".
