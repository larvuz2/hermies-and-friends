# Response quality — specification and release record

**Standard.** Every sentence shown to a user is grounded, explicitly attributed,
explicitly uncertain, or a deterministic system fact — and every unsolicited
message earns its interruption.

That is a stronger claim than "the messages sound good", and it is the one this
architecture can actually defend.

---

## 1. The decision that shapes everything

The judge no longer writes what the user reads.

**Before:** transcript → findings note → judge JSON with a `pitch` → formatter
printed the pitch. The user read prose a model wrote while being asked to make
it *compelling* over hedged evidence. Nothing could check it, because there was
nothing to check it against.

**Now:** numbered transcript → findings note with turn citations → structured
judgement (claims + evidence states + source ids) → validated packet →
deterministic compiler → prose.

```
judgement.py   the model produces auditable data, never delivered prose
response.py    the contract: a claim cannot exist without a real source
render.py      the compiler: attribution and uncertainty are functions, not style
```

Two properties follow that no amount of prompt engineering buys:

- **A fabricated claim is caught mechanically.** Its likeliest shape is
  plausible text with an invented citation (`turn:9` in a four-turn
  conversation). `known_sources` makes that a validation failure.
- **Attribution cannot be lost.** "Her agent said…" is emitted by a function.
  There is no temperature setting that drops it because the sentence read
  better without it.

## 2. Evidence states

`profile_only` · `counterpart_claim` · `conversation_established` ·
`independently_verified` · `system_fact` · `unknown`

Not a boolean. The difference between "their profile says", "they told us", and
"we worked it out together" is exactly what a user needs to judge risk, and
`verified: true/false` destroyed it. The compiler attributes the first two and
`unknown`; it states the rest plainly.

`independently_verified` is **refused at parse time** — we never check anything
against a third source, so a model asserting we did is claiming a capability the
product does not have.

## 3. Fail direction

Everything degrades toward *not interrupting*:

| Situation | Result |
|---|---|
| Unparseable judgement | `watch` |
| Claim cites a non-existent turn | claim dropped |
| All claims dropped | `notify` → `watch` |
| No grounded relevance | `notify` → `watch` |
| No available action | `notify` → `watch` |
| Packet fails validation | not rendered |

The single exception is a **requested answer**, which must always reach the
human — including a negative one, and including "I could not find out". An
honest no-answer is a successful response; silence there is the failure.

## 4. What is enforced, not trusted

| Promise | Enforced by |
|---|---|
| Max 3 findings per interruption | `_config.max_findings_per_batch`, `_emit` |
| One feedback invitation per batch | `render.render_batch` |
| Answers never rationed | `_emit` exempts `requested` |
| Retries not re-charged | `redelivery` flag |
| No Ring-0 in delivered text | `response.validate` sentinels |
| Contact only in reveals | `response.validate` |
| No machinery vocabulary | `response.banned_terms_in` |
| Preview never implies release | reveal renderer + validator |

## 5. Results

120-scenario corpus, 15 categories, deterministic, no LLM in the default path.

```
scenarios passing all gates : 120/120
batches passing G11         : 15/15
hard gates                  : 11/11 pass
longest delivered message   : 45 words
```

Run it:

```bash
python evals/run_response_eval.py --fast      # CI subset, 40 scenarios
python evals/run_response_eval.py --full      # all 120
python evals/run_response_eval.py --selftest  # proves the harness catches faults
```

The selftest is the important one. It plants three faults — a Ring-0 sentinel, a
counterpart claim stated as fact, and a must-silence scenario that speaks — and
requires the harness to catch each *and* stay quiet on the clean version. A
harness that cannot catch planted faults is decoration.

### Calibration found on the first full run

Four gates failed initially. All four were harness or fixture bugs, and they are
worth recording because each is a way a gate becomes useless:

1. **104 false positives** — `@handle` treated as contact data. An agent handle
   is public network identity; the hub routes by it. A gate that fires 104 times
   on correct output gets switched off, and a switched-off gate protects nobody.
2. **`$0.19/min` read as a raw score** — the very figure that makes an
   expense-alternative finding actionable.
3. **Reveal attribution missed** — "Mira's agent is open to an introduction"
   attributes by *subject*, not verb.
4. **Bare token `"shared"`** — fired on "Nothing has been shared yet", the
   sentence that makes a preview safe.

Two real compiler bugs were also found and fixed: an unattributed claim was not
capitalised, and a two-option close rendered "A, or B, or leave it?".

## 6. Versioning and rollback

Three versions travel with every response and appear in telemetry:

- `response.CONTRACT_VERSION` — packet shape
- `render.COMPILER_VERSION` — rendered output
- `judgement.JUDGE_PROMPT_VERSION` — the judge contract

**Rollback.** The compiler is opt-in per item: `_format_notification` uses it
only when *every* item carries a valid packet, and falls through to the legacy
formatter otherwise. To revert delivery wholesale without reverting the
architecture, make `_packet_for_finding` return `None` — one line, and every
message reverts to the previous format with no data migration. The corpus,
gates and contract stay in place.

## 7. Telemetry

`telemetry.py` records shape and outcome only: response type, requested flag,
word count, claim and uncertainty counts, evidence-state histogram, offered
actions, delivery/acknowledgement, feedback category, and all three versions.

It never records transcripts, dossier content, contact details, or the delivered
prose. `is_safe()` re-checks each event, because a future caller could put free
text into a permitted field. The temptation to log "just the message, so we can
see what went wrong" is the thing to refuse: a delivered message quotes the
counterpart and describes the user's own situation. When a message is wrong, the
user can send it to us themselves, knowingly.

## 8. Not done

- **Human blinded review** — `docs/RESPONSE-REVIEW-PACK.md` is generated and
  ready; it needs three human reviewers. Thresholds are stated in the pack.
- **Model-based scoring (Layer 2)** — the deterministic layer is complete;
  rubric scoring across the seven dimensions is not wired.
- **Shadow evaluation in beta (Layer 4)** — needs the hub deployed.
