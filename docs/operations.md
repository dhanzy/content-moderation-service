# Production plan

What to check and mitigate in the first fifteen minutes of an incident, how to
roll a fix out, the rollback triggers decided in advance, and how to verify
recovery rather than assume it.

[← back to the README](../README.md)

---


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

