# Cost, evasion and scale

Build-versus-buy per screening category, the monthly bill for 10,000 images and
1,000 one-minute videos worked out from frames, how a determined user would get
a banned prompt past the checker, adding CSAM hash matching, and what breaks
first at a million uploads a day.

[← back to the README](../README.md)

---


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
stated in [AI.md](../AI.md); "I have only tried the cases I thought of" is not a
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
and that is stated in [AI.md](../AI.md).

---

