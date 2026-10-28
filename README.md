# Content moderation service

Screens uploaded and AI-generated media so that nothing prohibited reaches
anyone, nothing explicit reaches an unverified workspace, and nothing goes live
unchecked — including when the moderation provider is slow, down, or answers
twice.

Python 3.12 / FastAPI / PostgreSQL / Redis, a React + TypeScript review page, and
a fake moderation provider as a separate service. `docker compose up -d` starts
everything. **85 tests pass**, and the load, outage and crash behaviour is
measured rather than asserted — see [What is proven](#what-is-proven).

---

## What went wrong, in one table

The five symptoms from the incident report. Each one is reproducible here
against both the pre-fix and the post-fix handler.

| Symptom | Root cause | Fix | Test |
| --- | --- | --- | --- |
| An explicit video went live in an unverified workspace | Only the first 8 frames were scored, and the video turned explicit in its final second | Every frame is scored and the decision takes the **peak** across all of them; a video that cannot be fully scored is held, never passed | `test_broken_misses_explicit_in_last_frame` / `test_fixed_blocks_explicit_in_last_frame_unverified` |
| Media published while the provider was down | A provider timeout was treated as a pass | A call that returns nothing is a failed attempt. The item is held from the **first** failure, `screened_at` stays null, and the sweep re-screens it on recovery | `test_broken_publishes_unchecked_on_timeout` / `test_fixed_holds_from_the_very_first_failure` |
| An AI generation of "a 16-year-old" was produced | Generation prompts were never screened | Prompts are refused before generation with `422` — on de-leeted text for word rules and untouched digits for age rules | `test_broken_lets_minor_prompt_through` / `test_fixed_refuses_minor_prompt` |
| The provider bill roughly doubled | A redelivered job ran alongside an attempt already in flight | Jobs are leased with a single conditional `UPDATE`, and `provider_results` is keyed on `media_id` so a second successful score cannot be stored | `test_broken_pays_twice_for_a_redelivered_job` / `test_fixed_scores_once_under_concurrent_redelivery` |
| Moderators saw "Nothing to review" while 40 items waited | A failed review-queue request rendered as an empty list | Three states as a discriminated union, and a client that throws on any non-2xx, so a failure cannot arrive as data | `test_broken_shows_failure_as_empty_list` / `test_fixed_reports_review_failure_as_failure` |

The thread running through all five: **nothing counts as screened unless a
provider result was stored for it.** `screened_at` is the only record of that,
and every metric derives from it rather than from job state. Trusting a
*finished job* is what let the first two symptoms hide.

---

## Quick start

```bash
docker compose up -d        # builds on first run, ~4 min cold
curl -s localhost:8000/healthz
#   {"ok":true,"screening_mode":"fixed"}
```

Then open the review queue at **<http://localhost:5173>**.

| Service | Host port | Purpose |
| --- | --- | --- |
| `api` | **8000** | the service under test |
| `provider` | **8001** | fake moderation provider |
| `web` | **5173** | review queue UI |
| `db` / `redis` | 5432 / 6379 | Postgres 16, queue backing store |

There is no login. The caller is two headers, and both workspaces are seeded at
startup:

| Workspace | Verified | Users |
| --- | --- | --- |
| `ws_verified` | yes | `admin_v` (admin), `member_v` (member) |
| `ws_unverified` | no | `admin_u` (admin), `member_u` (member) |

```
-H "X-User-Id: member_v" -H "X-Workspace-Id: ws_verified"
```

A user sending a workspace they do not belong to gets `403` rather than being
silently scoped to their own. `docker compose down -v` stops everything and
discards the data. For ports already in use, a debugger, or running the
processes natively, see [docs/running.md](docs/running.md).

---

## Try it

A media item is a JSON document listing its frames and the scores the provider
will return for each — no real images and no classifier. An image has one frame;
a video has one frame per second.

**The headline case: a video that only turns explicit in its final second**,
in an unverified workspace. Sixty frames, so the provider scores it
asynchronously and posts a signed result back to the callback.

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
The same video in `ws_verified` is `published`.

**[TESTING.md](TESTING.md) walks every rule this way** — one scenario each, plus
the outage, recovery and fairness behaviour, and a side-by-side run of the broken
and fixed builds. Four quick ones:

```bash
# another workspace reading the item -> 404, not 403: it does not confirm existence
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/media/$ID \
  -H 'X-User-Id: member_v' -H 'X-Workspace-Id: ws_verified'

# a generation prompt naming a minor -> 422, no media row, no provider call spent
curl -s -X POST localhost:8000/media -H 'content-type: application/json' \
  -H 'X-User-Id: member_v' -H 'X-Workspace-Id: ws_verified' \
  -d '{"type":"image","source":"generation","prompt":"a 16-year-old girl",
       "frames":[{"explicit":0.1}]}'

# an unsigned provider callback -> 401
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/provider/callback \
  -H 'content-type: application/json' -d '{"media_id":"whatever","frames":[]}'

# every item scored at most once
curl -s localhost:8001/_stats
```

---

## The review queue page

<http://localhost:5173> — React and TypeScript, dependencies locked in
`web/bun.lock`. It shows an admin the held media of their workspace and lets
them approve or reject. The part that matters is that it tells **three states**
apart:

| State | What you see | Why it is separate |
| --- | --- | --- |
| **loading** | grey, "Loading…" | A request in flight is not an answer |
| **failed** | red, with the status and "this is **not** an empty queue" | The incident symptom exactly |
| **empty** | green, "the queue loaded successfully and no media is currently held" | Genuinely nothing waiting |

In the code these are a discriminated union rather than an `items` array beside
a `loading` boolean and an `error` string:

```ts
type QueueState =
  | { kind: "loading" }
  | { kind: "failed"; status: number | null; message: string }
  | { kind: "ready"; items: HeldItem[] };
```

No combination of flags can render a failure as an empty list, because a failure
carries no items at all. `fetchReviewQueue` throws on any non-2xx, so a failure
cannot reach the UI looking like data. To see it:

```bash
REVIEW_FAIL=1 docker compose up -d api     # GET /review now answers 503
docker compose up -d api                   # back to normal
```

The page has buttons to create sample media, so the queue demonstrates itself
without `curl`. An item showing `screened: no` is held because screening was
*unavailable*, not because a human needs to judge it — it has no provider result
behind it, so approving it is refused with `409 never_screened`.

---

## The four decisions worth arguing about

| Decision | Why | Cost I accepted |
| --- | --- | --- |
| **`screened_at` is the only proof of screening** | The brief warns not to treat a finished job as proof, and that conflation is what hid the incident. Every metric derives from this column. | An extra column to keep honest on every path |
| **Hold from the first failed attempt, not at the end of the retry budget** | An item nobody could screen should be visible to a moderator in five seconds, not after a minute of retries. `screened_at` stays null, so a provisional hold is never mistaken for a decision. | It opened a hole — a held item with no score reaching the approve route — closed with `409 never_screened` |
| **A hand-rolled per-workspace round-robin queue, not Celery or arq** | A 200-video burst must not starve another workspace, and fairness has to live in the dispatcher. A library pulling FIFO from one list cannot do it. | Reconciling Redis against Postgres myself; Postgres stays the source of truth, so losing Redis costs latency, not correctness |
| **The broken handler stays in the tree** behind `SCREENING_MODE=broken` | The brief asks for the incident to be reproduced first. Keeping it switchable gives every symptom a matching pair of tests and makes it demonstrable with one variable. | Dead code in the repo, loudly logged as `broken_mode_active` at ERROR |

---

## What is proven

220 items (200 videos from `ws_unverified`, 20 images from `ws_verified`)
through a 25-second provider outage, via [`load/workload.py`](load/workload.py):

| | broken | fixed |
| --- | --- | --- |
| **media published unchecked** | **13** | **0** |
| provider calls per upload | 1.555 | 1.114 |
| max provider calls for one item | 12 | 3 |
| **age of oldest waiting job** (end of run) | **54.1s** | **0.0s** |

Fairness, healthy provider — the 20 images were submitted *after* all 200 videos
were already queued, and every one was decided by **5.4s** while the burst ran
on to **13.3s**. Queued FIFO they would have waited the full 13s.

A worker killed between the provider answering and the result being saved
(`CRASH_AFTER_SCORE`) leaves the item `pending`, not published; after restart the
lease lapses, the job is reclaimed, and the ledger still holds exactly one
successful score — at the cost of one wasted call, which is the one window this
design cannot close.

**[docs/evidence.md](docs/evidence.md)** has the full runs, why `1.114` is not a
rule-8 violation, the `pending`-versus-`held` split during the outage, the
metrics endpoint, and how to trace one item through the logs.

---

## Reproducing the incident

```bash
SCREENING_MODE=broken docker compose up -d api worker   # the pre-fix behaviour
docker compose up -d api worker                         # back to fixed
```

In broken mode the service deliberately treats a provider timeout as a pass,
scores only the first 8 frames of a video, never checks generation prompts, lets
a redelivered job run alongside an attempt already in flight, and renders a
failed review-queue request as an empty list. Both `api` and `worker` log
`broken_mode_active` at ERROR on startup, so a broken build cannot be mistaken
for a healthy one.

## Running the tests

The stack must be up — the suite uses the real Postgres, Redis and fake
provider. It creates its **own** database (`moderation_test`) and uses Redis DB
1, so it never touches the running stack's data and never competes with the
worker for jobs.

```bash
cd api && uv run pytest -q          # 85 passed
docker compose exec api pytest -q   # or inside the container, no host toolchain
```

| File | Covers |
| --- | --- |
| `tests/test_rules.py` | the decision rules as a pure function: thresholds, precedence, every prohibited label, whole-video scanning |
| `tests/test_incident.py` | the five incident symptoms, each asserted against both the broken and the fixed handler |
| `tests/test_access.py` | workspace isolation and admin-only review |
| `tests/test_callback.py` | callback signing, duplicate and partial results |

---

## Further reading

| | |
| --- | --- |
| [docs/cost.md](docs/cost.md) | Build versus buy per category, the monthly bill worked out from frames, prompt-evasion hardening, CSAM hash matching, and what breaks first at 1M uploads/day |
| [docs/evidence.md](docs/evidence.md) | The measured load, outage and crash runs; metrics; tracing one item through the logs |
| [docs/operations.md](docs/operations.md) | First fifteen minutes of an incident, rollout, rollback triggers, verifying recovery |
| [docs/api.md](docs/api.md) | Every route, who may call it, what it answers |
| [docs/running.md](docs/running.md) | Running natively without Docker, every config variable, troubleshooting |
| [TESTING.md](TESTING.md) | Every rule by hand with `curl`, no pytest |
| [AI.md](AI.md) | How AI was used, what I verified, and what I did not |

---


## Project layout

```
docker-compose.yml        all six services
provider/                 fake moderation provider (own uv project)
api/                      the service (own uv project)
  app/
    models.py             schema
    routes/               media, review, callback, internal
    screening/
      rules.py            the decision rules, pure — no I/O
      prompts.py          generation-prompt refusal
      provider.py         provider client, error classification, retry policy
      fixed.py            the real handler
      broken.py           the deliberately broken handler
    workq/
      fairqueue.py        per-workspace round-robin Redis queue
      leases.py           job leasing, so one job runs once
      worker.py           dispatcher, reconciler, re-screening sweep
  tests/
web/src/                  review queue UI — api.ts throws on non-2xx,
                          App.tsx holds the three-state union
load/workload.py          the burst/outage harness behind the numbers above
evidence/                 its recorded output, committed
docs/                     cost, evidence, operations, running
```

---

## Not finished

| Missing | Notes |
| --- | --- |
| No circuit breaker | During an outage each item is held only once its own attempt has failed, costing one provider timeout each. After a run of consecutive failures the dispatcher should hold everything already queued immediately — in the measured outage that left 202 items at `pending` for a minute. Correct, but slower to tell the truth than it needs to be. |
| Migrations | Schema is created with `create_all` behind an advisory lock, not Alembic. |
| The UI runs Vite's dev server | `VITE_API_BASE` is baked in at build time, so a prebuilt bundle could not be re-pointed by compose. A real deployment would build and serve the static output. |
| No UI tests | The three states are modelled so a failure cannot render as empty, and all three were checked in a browser, but no test holds that in place. |
| One worker, one Redis | The design is multi-node ready — Postgres is the source of truth, leases are conditional updates — but I have never run it sharded. The scaling analysis in [docs/cost.md](docs/cost.md) is reasoned, not measured. |

Roughly nine hours, with the last third going to the load harness and the
measured evidence. [AI.md](AI.md) records how AI was used, the wrong suggestions
I caught by running the code, and what I have not verified.
