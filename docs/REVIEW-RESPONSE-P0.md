# Re: Beta-launch review — P0 implemented

**Commit:** `751c4ea` · **Status:** all four P0 items closed
**Verified:** 344 plugin + 98 backend tests, 73 installer assertions, both e2e
suites, all eval gates on the **real** model.

Thanks — this was accurate and specific enough to act on directly. I verified
each P0 claim against the code before touching it; **all four reproduced.** Two
of your recommendations turned out to be load-bearing in ways worth reporting
back, because implementing one of them naively would have made the product
worse. Details below.

---

## 1. Inference billing — fixed, and it was worse than "auto falls back"

Confirmed exactly as described. `_config.llm_mode()` returned `"auto"`, and
`__init__.py` fell through to `_via_ctx` on any non-budget exception.

Your framing of the risk was right, and the failure surface is wider than a
`503`: **every cycle before registration** also billed the user, because
`is_live()` is false until the first card publish. So a brand-new install — the
exact moment a beta user forms their impression — was the *most* likely time to
silently spend their money.

**Done:**
- Default is now `"hub"`. `auto`/`local` remain, opt-in only.
- `/hermix doctor` reports the active mode, and says plainly when a mode spends
  the user's own budget.
- The regression test is phrased as the promise, parameterised over the four
  paths that actually billed people:

```python
@pytest.mark.parametrize("scenario", [
    "hub_503", "hub_network_error", "hub_500", "not_live",
])
def test_out_of_the_box_no_network_work_ever_bills_the_user(...):
    assert llm("sys", "usr") == ""
    assert ctx._llm.calls == [], f"{scenario}: the user's model was billed"
```

**Note on your alternative** ("or change the public promise"): I went with
changing the default rather than the README. The promise is the product's
differentiator, and "no API key, ever" is on the landing page — weakening it to
match the code would have traded the reason people try this for an
implementation convenience.

---

## 2. `.env.example` — fixed, plus a guard so it cannot drift again

Confirmed: `api.hermix.network` (a domain we do not run) and a "device login
(Phase 1)" flow that no longer exists.

Rewritten with the correct hub, an explanation that registration is automatic,
`HERMIX_LLM` documented including who pays, and the beta cadence knobs.

**Beyond your recommendation:** the real defect was that *nothing could catch
this*. Docs have no tests, so drift is invisible until a user hits it. Added
`tests/test_env_example_matches_reality.py` (10 tests), which fails if:

- a documented key is not read anywhere in the code
- the documented hub URL differs from `_config.DEFAULT_API_URL`
- any documented default value differs from what the getter actually returns
- the stale onboarding language (`device login`, `hermix.network`) returns

That last one is a regression test for this specific incident.

---

## 3. Embedding fallback gate — built and exercised in both modes

Your diagnosis was right and it is the failure mode I'd have been least likely
to notice in production, precisely because it looks healthy.

**Done:**
- `/healthz` now returns `engine`, `model`, `indexed_cards`, and a derived
  `degraded` flag. Still unauthenticated — the gate runs before any agent
  exists, so it cannot authenticate.
- `deploy/hostinger/smoke.py` (stdlib only, runs on a bare VPS) asserts
  `engine == fastembed`, then runs a **live semantic canary**: registers two
  cards with deliberately zero shared vocabulary — *"a game studio building
  three-dimensional environments"* vs *"a freelance 3d worlds artist"* — and
  requires discovery to connect them.
- `deploy.sh` aborts the deploy if either check fails, with the prewarm command
  in the error.

I kept your canary pair because the second check matters independently: the
model can load and still be indexed against an empty corpus, and only an
end-to-end query catches that.

**Verified against a live hub, both directions:**

```
real model:   PASS engine=fastembed · PASS canary connected            EXIT=0
forced back:  FAIL engine=fallback                                     EXIT=1
              ...while /healthz still returned HTTP 200
```

That last line is your point made concrete.

**One consequence you didn't raise.** The canary registers two throwaway
accounts per deploy. `/v1/profile/remove` withdraws cards and vectors but
deliberately keeps the account — so `total_agents` (`db.count_accounts()`) would
creep up by two on every deploy. That is the single number an operator uses to
judge a 10–25 person beta, so the gate would have been quietly corrupting the
metric it exists to protect. Fixed: the `smoke-` prefix is excluded from the
agent count and **reserved to loopback**, so it can't be used to register
invisibly.

**On your fallback numbers:** your `0.767` / `2 of 8` was environmental — the
model download hit `403` in your sandbox. On the real model here, the checked-in
figures reproduce exactly: **recall@10 = 0.900, cross-vocab 6/8, spam 0.000, all
gates pass.** No action needed; noting it so it isn't logged as a regression.

---

## 4. Notification ceiling — implemented, but the naive version was a trap

Adopted your beta cadence: `MATCH_EVERY_HOURS=6`, `MAX_NEW_DIGS_PER_CYCLE=2`,
`MAX_NOTIFY_PER_DAY=1`.

I changed the **code defaults** rather than using remote config as you
suggested. Remote knobs require a deployed, configured hub, and the failure mode
is silent — if the config fetch fails, a fresh install falls back to the
aggressive defaults, which is exactly the cohort you're protecting.

**Three things you should know, because your recommendation as written would
have shipped a worse product than having no cap at all.** Your review said "keep
requested asks and reveal outcomes exempt" — the code did **not** do that, and I
only found it by reading the enforcement path before changing the default.

**(a) The cap counted findings, not interruptions.**

```python
room = max(0, cap - used)
if passes and len(send) < room:      # len(send) is ITEMS
```

With `cap=1`, four strong findings would deliver **one** and hold three — the
same interruption for a quarter of the value, and the direct opposite of your
"batch at most 3 findings". The ceiling now gates whether we may interrupt *at
all*; a permitted interruption still carries the whole batch.

**(b) An unsolicited finding could swallow an answer the human asked for.**
`requested` bypassed the *bar* but not `room`. With `cap=1`, an unprompted
finding delivered at 09:00 would consume the day's only slot, and an ask result
at 09:05 — something the human is actively waiting on — was held until tomorrow.
Answers are now exempt from the ceiling entirely.

**(c) Undelivered findings would be swallowed by the ceiling.**
`INFLIGHT_EXPIRY_SECONDS` is 6h, well inside the 24h cap window, so the retry of
a finding that *never reached the human* was re-gated as a fresh interruption
and went silent. That inverts the outbox's stated design — "a duplicate is
recoverable, a silently swallowed finding is not". Redeliveries are now marked
and exempt: it's the same interruption re-attempted, not a new one.

**Related, and required for (b) to be coherent:** answers no longer charge
`notify_log` at all. That log drives *both* the pressure curve and the ceiling,
so asking a question used to raise the bar against yourself and consume the
day's finding.

Six tests pin all of this (`test_matchmaker.py`). I verified each is
non-vacuous by reverting the fix and confirming the test fails.

---

## Not done, and why

- **P1/P2** — untouched. Happy to take them next; **#5 (response-quality evals)**
  and **#9 (atomic budget reservation)** look like the highest value.
- **P2 #11 (stale eval constants)** — the report regenerated identically except
  for latency, so I reverted it rather than commit my laptop's timings over
  numbers measured on representative hardware. The doc drift you identified is
  real and still open.
- **Your #6 (templated notification voice)** — I agree with the diagnosis and
  deliberately did not fold it in here. It changes what every user reads, and
  bundling a copy rewrite into a commit about billing and safety defaults would
  make both harder to review or revert.

## Still open before invites (not from your review)

Two operator-side items you couldn't have seen from the repo: **credential
rotation** (an OpenRouter key and an admin password were exposed during
development), and **the hub deploy itself** — production is many commits behind,
so none of the above is live yet. The smoke gate will run on that deploy.

## Correction to one framing

Your report lists the notification defaults under "no code change needed —
remote knobs already exist". True for the *values*, but not for the *behaviour*:
as shown in (a)–(c), setting `MAX_NOTIFY_PER_DAY=1` through remote config alone
would have introduced all three bugs above in production, with no test coverage
and no obvious symptom — the product would simply have gone quieter than
intended and swallowed answers users were waiting for. Worth flagging in case
that recommendation gets applied to another deployment from the review as
written.
