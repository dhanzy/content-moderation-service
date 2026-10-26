# Testing it by hand

A guided walkthrough of the whole service with `curl`, no pytest involved. Every
rule in the brief gets one scenario, and each shows what to expect and why it
matters.

If you want the automated equivalent, `cd api && uv run pytest -q` asserts all
of this and more in about 20 seconds. This document is for understanding the
system; the suite is for proving it still works.

---

## Before you start

### 1. Infrastructure

Postgres and Redis are not Python, so they come from Docker even when you run
the services with uv:

```bash
cd /home/kali/website/JobTest/onlyfans
docker compose up -d db redis
docker compose ps        # both should be "healthy"
```

Make sure no containerised `api`, `provider` or `worker` is left running, or
you will have two of each fighting over the same jobs:

```bash
docker compose rm -sf api provider worker
```

### 2. The three services, one terminal each

```bash
# terminal 1 — the fake classifier
cd provider && CALLBACK_URL=http://localhost:8000/provider/callback uv run main.py
```

```bash
# terminal 2 — the API
cd api && uv run uvicorn app.main:app --port 8000 --reload
```

```bash
# terminal 3 — the worker
cd api && uv run python -m app.workq.worker
```

`CALLBACK_URL` on the provider is not optional. Without it the provider posts
asynchronous results to `http://api:8000/...`, a Docker-internal name, and every
video over 30 frames will sit at `pending` forever.

Keep terminal 3 visible. The worker's JSON log is where screening actually
happens, and watching it is most of the value of this exercise.

### 3. A fourth terminal for the commands below

```bash
cd /home/kali/website/JobTest/onlyfans

# Identities. There is no login; these two headers are the whole auth model.
MV=(-H "X-User-Id: member_v" -H "X-Workspace-Id: ws_verified")    # member, verified ws
AV=(-H "X-User-Id: admin_v"  -H "X-Workspace-Id: ws_verified")    # admin,  verified ws
MU=(-H "X-User-Id: member_u" -H "X-Workspace-Id: ws_unverified")  # member, unverified ws
AU=(-H "X-User-Id: admin_u"  -H "X-Workspace-Id: ws_unverified")  # admin,  unverified ws
JSON=(-H "content-type: application/json")

# Upload something and keep its id.
upload() { curl -s -X POST localhost:8000/media "${JSON[@]}" "${@:2}" -d "$1" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])'; }

# Look at an item.
look() { curl -s localhost:8000/media/$1 "${@:2}" | python3 -m json.tool; }

# Start from clean: drops all media and jobs, zeroes the provider's counters.
reset() { curl -s -X POST localhost:8000/_admin/reset > /dev/null; echo "reset"; }
```

These are bash arrays, so use bash or zsh, not sh.

```bash
reset
```

---

## Part 1 — the nine rules

### Rule 2a: a prohibited label at 0.8 or above is blocked

```bash
ID=$(upload '{"type":"image","source":"upload","frames":[{"minor":0.9}]}' "${MV[@]}")
sleep 1 && look $ID "${MV[@]}"
```

```json
{"status": "blocked", "reasons": ["prohibited:minor"], "screened": true}
```

Blocked in a **verified** workspace. Verification buys you explicit content
between adults, nothing on the prohibited list. Try the same in `ws_unverified`
and you get the same answer.

### Rule 2b: 0.5 up to 0.8 is held for a human

```bash
HELD=$(upload '{"type":"image","source":"upload","frames":[{"minor":0.6}]}' "${MV[@]}")
sleep 1 && look $HELD "${MV[@]}"
```

```json
{"status": "held", "reasons": ["review:minor"], "screened": true}
```

Note the difference in `reasons`: `prohibited:` means decided, `review:` means a
person has to look. Keep `$HELD` — rule 6 uses it.

And just below the band:

```bash
ID=$(upload '{"type":"image","source":"upload","frames":[{"minor":0.49}]}' "${MV[@]}")
sleep 1 && look $ID "${MV[@]}"     # -> "published"
```

### Rule 3: explicit is allowed only in a verified workspace

The same upload, twice, differing only in which workspace it lands in:

```bash
V=$(upload '{"type":"image","source":"upload","frames":[{"explicit":0.95}]}' "${MV[@]}")
U=$(upload '{"type":"image","source":"upload","frames":[{"explicit":0.95}]}' "${MU[@]}")
sleep 2
echo "verified:   $(curl -s localhost:8000/media/$V "${MV[@]}" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["status"], d["reasons"])')"
echo "unverified: $(curl -s localhost:8000/media/$U "${MU[@]}" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["status"], d["reasons"])')"
```

```
verified:   published []
unverified: blocked ['explicit_in_unverified_workspace']
```

This is the rule the incident broke.

### Rule 9: every frame of a video counts

A minute of video, clean for 59 seconds, explicit in the last one — in an
unverified workspace. Over 30 frames, so it takes the asynchronous path: the
provider answers `202` and posts a signed result back a moment later.

```bash
BODY=$(python3 -c 'import json; print(json.dumps({"type":"video","source":"upload",
  "frames":[{"explicit":0.01}]*59 + [{"explicit":0.95}]}))')
LONG=$(upload "$BODY" "${MU[@]}")
sleep 1 && look $LONG "${MU[@]}"      # still "pending" -- 202 is not a decision
sleep 4 && look $LONG "${MU[@]}"
```

```json
{"status": "blocked", "reasons": ["explicit_in_unverified_workspace"],
 "frames_total": 60, "frames_scored": 60, "screened": true}
```

**`frames_scored: 60` is the whole point.** The broken version scored 8 and
called it clean. Watch the worker log and you will see
`provider_accepted_async` followed by `callback_applied`.

### Rule 4: a generation prompt asking for prohibited content is refused

```bash
curl -s -o /dev/null -w 'minor:    %{http_code}\n' -X POST localhost:8000/media "${JSON[@]}" "${MV[@]}" \
  -d '{"type":"image","source":"generation","prompt":"a 16-year-old girl","frames":[{"explicit":0.1}]}'
curl -s -X POST localhost:8000/media "${JSON[@]}" "${MV[@]}" \
  -d '{"type":"image","source":"generation","prompt":"a 16-year-old girl","frames":[{"explicit":0.1}]}'
```

```
minor:    422
{"reasons":["prompt_refused:minor"]}
```

The refusal happens in the request handler, before a row is created — so nothing
is generated and no provider call is spent. Compare the provider's counters
before and after: unchanged.

Worth trying a few:

```bash
for P in "a 16-year-old girl" "sixteen year old" "underage model" \
         "non-consensual scene" "sex with a dog" "suicide scene" \
         "portrait of Taylor Swift" "incest roleplay, explicit" \
         "two adults on a beach at sunset" "a 19-year-old woman" \
         "eighteen year old model"; do
  C=$(curl -s -o /dev/null -w '%{http_code}' -X POST localhost:8000/media "${JSON[@]}" "${MV[@]}" \
    -d "$(python3 -c "import json,sys; print(json.dumps({'type':'image','source':'generation','prompt':sys.argv[1],'frames':[{'explicit':0.1}]}))" "$P")")
  printf '%-34s %s\n' "$P" "$C"
done
```

The last three must be `202`. `eighteen year old` is the interesting one — a
naive keyword list catches `teen` inside it.

### Rule 1: a workspace sees only its own media

```bash
MINE=$(upload '{"type":"image","source":"upload","frames":[{"explicit":0.01}]}' "${MV[@]}")
echo "owner:       $(curl -s -o /dev/null -w '%{http_code}' localhost:8000/media/$MINE "${MV[@]}")"
echo "own admin:   $(curl -s -o /dev/null -w '%{http_code}' localhost:8000/media/$MINE "${AV[@]}")"
echo "other ws:    $(curl -s -o /dev/null -w '%{http_code}' localhost:8000/media/$MINE "${MU[@]}")"
echo "other admin: $(curl -s -o /dev/null -w '%{http_code}' localhost:8000/media/$MINE "${AU[@]}")"
```

```
owner:       200
own admin:   200
other ws:    404
other admin: 404
```

`404`, not `403` — a `403` would confirm the item exists somewhere.

Nor can you borrow a workspace by changing the header:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/media "${JSON[@]}" \
  -H "X-User-Id: member_v" -H "X-Workspace-Id: ws_unverified" \
  -d '{"type":"image","source":"upload","frames":[{"explicit":0.01}]}'
# 403 -- member_v does not belong to ws_unverified
```

### Rule 6: only admins approve or reject

Using `$HELD` from rule 2b:

```bash
echo "member sees queue:  $(curl -s -o /dev/null -w '%{http_code}' localhost:8000/review "${MV[@]}")"
echo "admin sees queue:   $(curl -s localhost:8000/review "${AV[@]}" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)),"item(s)")')"
echo "member approves:    $(curl -s -o /dev/null -w '%{http_code}' -X POST localhost:8000/review/$HELD/approve "${MV[@]}")"
echo "admin approves:     $(curl -s -X POST localhost:8000/review/$HELD/approve "${AV[@]}")"
echo "queue after:        $(curl -s localhost:8000/review "${AV[@]}" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)),"item(s)")')"
```

```
member sees queue:  403
admin sees queue:   1 item(s)
member approves:    403
admin approves:     {"id":"...","status":"published"}
queue after:        0 item(s)
```

An admin of the *other* workspace gets `404` on the same id. And an admin cannot
approve past an absolute rule — an item blocked for `prohibited:` or for
explicit-in-unverified returns `409`, because payment-processor rules are not a
moderator's call.

### Rule 7: callbacks must be signed

Unsigned:

```bash
curl -s -o /dev/null -w 'unsigned: %{http_code}\n' -X POST localhost:8000/provider/callback \
  "${JSON[@]}" -d '{"media_id":"anything","frames":[]}'
# 401
```

Correctly signed. The HMAC is over the **exact bytes** of the body, so sign and
send the same string:

```bash
sign() { python3 -c 'import hmac,hashlib,sys; print(hmac.new(b"dev-provider-secret", sys.stdin.buffer.read(), hashlib.sha256).hexdigest())'; }

NEW=$(upload "$(python3 -c 'import json;print(json.dumps({"type":"video","source":"upload","frames":[{"explicit":0.01}]*40}))')" "${MV[@]}")
CB=$(python3 -c "import json;print(json.dumps({'media_id':'$NEW','frames':[{'explicit':0.01}]*39+[{'explicit':0.95}]},separators=(',',':')))")
SIG=$(printf '%s' "$CB" | sign)

curl -s -X POST localhost:8000/provider/callback \
  -H "X-Signature: $SIG" "${JSON[@]}" --data-binary "$CB"
```

Then try tampering: keep the signature but change a score, and it is rejected
with `401`. Try the right body with the wrong secret — also `401`.

### Rule 8: each item is scored successfully at most once

```bash
reset
ID=$(upload '{"type":"image","source":"upload","frames":[{"explicit":0.01}]}' "${MV[@]}")
sleep 2
curl -s localhost:8001/_stats | python3 -m json.tool
```

```json
{"calls": {"<id>": 1}, "mode": "ok"}
```

One upload, one call. To see the protection working, force the job to be
re-delivered after it has already succeeded:

```bash
docker exec -i onlyfans-db psql -U mod -d moderation -c \
  "UPDATE jobs SET state='queued', next_attempt_at=NOW(), lease_token=NULL WHERE media_id='$ID';"
docker exec -i onlyfans-redis-1 redis-cli LPUSH q:ws:ws_verified \
  "$(docker exec -i onlyfans-db psql -U mod -d moderation -tAc "SELECT id FROM jobs WHERE media_id='$ID'")"
docker exec -i onlyfans-redis-1 redis-cli SADD q:active ws_verified
sleep 3
curl -s localhost:8001/_stats | python3 -m json.tool
```

The count is **still 1**. The worker log shows `scored_from_cache`: it found the
stored result and reused it rather than paying again. The `provider_results`
table is keyed on media id, so a second successful score cannot even be
recorded.

### Rule 5: no decision means held, never published

Covered in Part 2, because it needs the provider to fail.

---

## Part 2 — when the provider misbehaves

### An outage holds work; it never publishes it

```bash
reset
curl -s -X POST localhost:8001/_control "${JSON[@]}" -d '{"mode":"timeout"}'
ID=$(upload '{"type":"image","source":"upload","frames":[{"explicit":0.01}]}' "${MV[@]}")
```

Watch terminal 3. You will see `provider_call_started`, then
`provider_call_failed` with `error_class: timeout`, then `retry_scheduled` with
a growing `delay_s` — 2s, 4s, 8s, 16s.

```bash
sleep 2 && look $ID "${MV[@]}"     # pending -- the first call is still in flight
sleep 6 && look $ID "${MV[@]}"
```

```json
{"status": "held", "reasons": ["screening_unavailable:timeout"], "screened": false}
```

**It is held, not published, and `screened` is false.** The broken version
published this.

The hold lands as soon as the *first* attempt fails — one client timeout, about
five seconds — not after the whole retry budget is spent. Rule 5 applies from
the first failure, so the item is visible to a moderator straight away:

```bash
curl -s localhost:8000/review "${AV[@]}" | python3 -m json.tool   # it is in here
```

Retrying continues underneath, and `screened` stays `false` the whole time, so a
provisional hold is never mistaken for a decision. Because there is no provider
result behind it, an admin cannot approve it either:

```bash
curl -s -X POST localhost:8000/review/$ID/approve "${AV[@]}"
# 409  {"detail":{"reasons":["never_screened"],"status":"held"}}
```

A borderline score is a judgement call; an item with no score at all is not.

Check nothing slipped through:

```bash
curl -s localhost:8000/_metrics | python3 -c 'import sys,json;d=json.load(sys.stdin);print("published_unchecked:",d["published_unchecked"])'
# 0
```

### Recovery without a human

With that item sitting held, bring the provider back:

```bash
curl -s -X POST localhost:8001/_control "${JSON[@]}" -d '{"mode":"ok"}'
sleep 8
look $ID "${MV[@]}"
```

```json
{"status": "published", "screened": true}
```

Nobody approved it. The worker's re-screening sweep noticed the provider was
healthy, found everything held with a `screening_unavailable:` reason, reset
their attempt budget and queued them again — `outage_rescreen_queued` in the
log. Items held for a *human* reason (`review:`) are deliberately left alone.

### Each failure class is handled differently

```bash
for MODE in error rate_limit malformed; do
  reset > /dev/null
  curl -s -X POST localhost:8001/_control "${JSON[@]}" -d "{\"mode\":\"$MODE\"}" > /dev/null
  ID=$(upload '{"type":"image","source":"upload","frames":[{"explicit":0.01}]}' "${MV[@]}")
  sleep 6
  echo "$MODE -> $(curl -s localhost:8000/media/$ID "${MV[@]}" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["status"],d["reasons"])')"
done
curl -s -X POST localhost:8001/_control "${JSON[@]}" -d '{"mode":"ok"}' > /dev/null
```

In the worker log, note the differing `error_class` and backoff:

| Provider behaviour | `error_class` | Treatment |
| --- | --- | --- |
| hangs | `timeout` | retry, 2s doubling, 5 attempts |
| `500` | `server_error` | retry, same |
| `429` | `rate_limit` | retry, 5s base, honours `Retry-After`, more attempts — it is a request to slow down, not a failure |
| unparseable body | `malformed` | **one** retry then held; a provider bug will not be fixed by hammering it, and an empty parse is never a pass |
| other `4xx` | `client_error` | no retry — we sent something wrong |

### `slow` is not the same as `timeout`

```bash
curl -s -X POST localhost:8001/_control "${JSON[@]}" -d '{"mode":"slow","delay_ms":3000}'
ID=$(upload '{"type":"image","source":"upload","frames":[{"explicit":0.01}]}' "${MV[@]}")
sleep 6 && look $ID "${MV[@]}"      # published -- 3s is under the 5s client timeout
curl -s -X POST localhost:8001/_control "${JSON[@]}" -d '{"mode":"slow","delay_ms":9000}'
ID=$(upload '{"type":"image","source":"upload","frames":[{"explicit":0.01}]}' "${MV[@]}")
sleep 8 && look $ID "${MV[@]}"      # pending, retrying -- 9s exceeds it
curl -s -X POST localhost:8001/_control "${JSON[@]}" -d '{"mode":"ok"}'
```

### One workspace's burst does not starve the other

```bash
reset
# 60 videos from the unverified workspace, then one image from the verified one
for i in $(seq 1 60); do
  upload "$(python3 -c 'import json;print(json.dumps({"type":"video","source":"upload","frames":[{"explicit":0.01}]*20}))')" "${MU[@]}" > /dev/null &
done
wait
SMALL=$(upload '{"type":"image","source":"upload","frames":[{"explicit":0.02}]}' "${MV[@]}")
sleep 3
look $SMALL "${MV[@]}"
curl -s localhost:8000/_metrics | python3 -c 'import sys,json;print(json.load(sys.stdin)["queue_depth"])'
```

The single image is decided within seconds even though 60 videos were queued
ahead of it, because the dispatcher rotates between workspaces rather than
draining one list in order. Queued FIFO, it would wait behind all 60.

---

## Part 3 — the review queue's three states

This is the symptom that hid the incident: moderators saw "Nothing to review"
all morning while items were waiting. The queue must distinguish **loading**,
**failed**, and **genuinely empty**.

**Genuinely empty:**

```bash
reset
curl -s -i localhost:8000/review "${AV[@]}" | head -1    # HTTP/1.1 200 OK
curl -s localhost:8000/review "${AV[@]}"                 # []
```

**Has items:**

```bash
upload '{"type":"image","source":"upload","frames":[{"minor":0.6}]}' "${MV[@]}" > /dev/null
sleep 2
curl -s localhost:8000/review "${AV[@]}" | python3 -m json.tool
```

**Failed** — restart the API with the fault injected:

```bash
# terminal 2
REVIEW_FAIL=1 uv run uvicorn app.main:app --port 8000
```

```bash
curl -s -i localhost:8000/review "${AV[@]}" | head -1    # HTTP/1.1 503
curl -s localhost:8000/review "${AV[@]}"                 # {"error":"review_queue_unavailable"}
```

`503` with an error body, not `200 []`. A UI cannot confuse the two. Restart
without `REVIEW_FAIL` when you are done.

---

## Part 4 — seeing the incident, then the fix

Restart the API and worker in broken mode:

```bash
# terminal 2
SCREENING_MODE=broken uv run uvicorn app.main:app --port 8000
# terminal 3
SCREENING_MODE=broken uv run python -m app.workq.worker
```

Both log `broken_mode_active` at ERROR on startup, so this build cannot be
mistaken for a healthy one.

**Symptom 1 — a timeout publishes unchecked:**

```bash
reset
curl -s -X POST localhost:8001/_control "${JSON[@]}" -d '{"mode":"timeout"}'
ID=$(upload '{"type":"image","source":"upload","frames":[{"explicit":0.01}]}' "${MV[@]}")
sleep 3 && look $ID "${MV[@]}"
```

```json
{"status": "published", "screened": false}
```

Live, with nothing behind it. The log says `timeout_treated_as_pass`. Now:

```bash
curl -s localhost:8000/_metrics | python3 -c 'import sys,json;print("published_unchecked:",json.load(sys.stdin)["published_unchecked"])'
# 1   <- in fixed mode this is always 0
```

**Symptom 2 — only the first 8 frames:**

```bash
curl -s -X POST localhost:8001/_control "${JSON[@]}" -d '{"mode":"ok"}'
BODY=$(python3 -c 'import json;print(json.dumps({"type":"video","source":"upload","frames":[{"explicit":0.01}]*59+[{"explicit":0.95}]}))')
ID=$(upload "$BODY" "${MU[@]}")
sleep 3 && look $ID "${MU[@]}"
```

```json
{"status": "published", "explicit": false, "frames_total": 60, "frames_scored": 8}
```

Explicit content, published, in an **unverified** workspace — the exact breach.
`frames_scored: 8` is the cause.

**Symptom 3 — the prompt is never checked:**

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/media "${JSON[@]}" "${MV[@]}" \
  -d '{"type":"image","source":"generation","prompt":"a 16-year-old girl","frames":[{"explicit":0.1}]}'
# 202  <- accepted. In fixed mode: 422
```

**Then switch back** and run the same four commands:

```bash
# terminal 2 and 3, without the variable
uv run uvicorn app.main:app --port 8000
uv run python -m app.workq.worker
```

| | broken | fixed |
| --- | --- | --- |
| timeout | `published`, unscreened | `held`, `screening_unavailable:timeout` |
| 60-frame video, unverified | `published`, 8 frames scored | `blocked`, 60 frames scored |
| minor prompt | `202` accepted | `422` refused |
| failed review queue | `200 []` | `503` |
| redelivered job | 2+ provider calls | 1 |

---

## Part 5 — reading what happened

### Metrics

```bash
curl -s localhost:8000/_metrics | python3 -m json.tool
```

The two that must always be `0` in fixed mode:

- `published_unchecked` — live with no provider decision behind it. Derived from
  `screened_at`, never from job state, because a finished job is not proof that
  anything was screened.
- `explicit_published_in_unverified` — the specific breach.

`provider_calls_per_upload` should be `1.0`. Call `reset` first, since the
provider's counters are global to its process.

### The log

One JSON object per line, carrying `workspace_id`, `media_id`, `job_id` and
`attempt_no` wherever known. Follow one item end to end:

```bash
# in the terminal running the worker, or pipe it to a file and grep
grep '"media_id": "<id>"' worker.log | python3 -m json.tool
```

A healthy screening reads: `media_accepted` → `provider_call_started` →
`decision_applied`. A failure reads: `provider_call_failed` →
`retry_scheduled` → ... → `held_no_decision`. Names worth knowing:

| Event | Meaning |
| --- | --- |
| `duplicate_delivery_dropped` | a redelivery arrived while the job was already leased — working as intended |
| `scored_from_cache` | reused a stored result instead of paying again |
| `provider_accepted_async` | `202`; waiting for the signed callback |
| `callback_rejected_bad_signature` | someone posted to the callback without a valid HMAC |
| `held_no_decision` | retries exhausted; fail-closed |
| `outage_rescreen_queued` | provider recovered; re-screening what the outage held |
| `approval_refused_absolute_rule` | an admin tried to approve something a payment processor forbids |
| `timeout_treated_as_pass` | **broken mode only** — the incident, in one line |

### The database

```bash
docker exec -it onlyfans-db psql -U mod -d moderation
```

```sql
-- what is live without having been screened? must be empty
SELECT id, status, screened_at FROM media WHERE status='published' AND screened_at IS NULL;

-- every item with its decision
SELECT id, workspace_id, status, explicit, frames_scored, reasons FROM media ORDER BY created_at;

-- retry history: one row per attempt, written before the provider call
SELECT media_id, attempt_no, state, error_class FROM job_attempts ORDER BY started_at;

-- the exactly-once ledger: at most one row per media item
SELECT media_id, via, received_at FROM provider_results;
```

An attempt stuck in `in_flight` with no matching `provider_results` row is the
signature of a worker that died after the provider scored an item but before the
result was saved — money spent, nothing to show for it.

---

## Cheat sheet

```bash
reset                                                   # clean slate
curl -s localhost:8000/_metrics | python3 -m json.tool  # the numbers
curl -s localhost:8001/_stats                           # the invoice
curl -s -X POST localhost:8001/_control "${JSON[@]}" -d '{"mode":"ok"}'
curl -s localhost:8000/review "${AV[@]}"                # the queue
```

| Want to see | Do this |
| --- | --- |
| blocked | `{"minor":0.9}` |
| held | `{"minor":0.6}` |
| published | `{"explicit":0.1}` |
| explicit allowed | `{"explicit":0.95}` in `ws_verified` |
| explicit blocked | `{"explicit":0.95}` in `ws_unverified` |
| async callback path | any video with more than 30 frames |
| fail-closed | `_control` to `timeout`, then wait a minute |
| recovery | `_control` back to `ok` |
| prompt refusal | `"source":"generation"` with `"prompt":"a 16-year-old girl"` |
