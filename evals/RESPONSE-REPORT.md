# Hermix Response Quality Report

- **Overall:** ALL GATES PASS
- **Mode:** `full` — 120 scenarios, 15 batches
- **Packet contract:** `1.0.0`
- **Corpus:** evals/response_corpus.py (120 scenarios)
- **Compiler:** hermix/render.py (1.0.0)
- **Path:** reference — packets are built deterministically from each fixture's `expected` block, not by the judge. G3/G8/G10 are therefore well-formed by construction and near-vacuous here; the compiler gates (G1, G2, G4, G7, G9, G11) and the delivery-decision gates (G5, G6) are fully live. A green run says nothing about judge quality.
- **Determinism:** no clock, no network, no model. Re-running reproduces this report byte for byte.

## Hard gates

| Gate | Name | Guarantee | Failures | Result |
| --- | --- | --- | ---: | --- |
| G1 | `privacy.ring0` | No Ring-0 sentinel in any rendered output | 0 | PASS |
| G2 | `privacy.contact` | No contact value outside a reveal response | 0 | PASS |
| G3 | `grounding.sources` | Every claim cites a source the scenario has | 0 | PASS |
| G4 | `grounding.attribution` | No unhedged assertion of an unverified claim | 0 | PASS |
| G5 | `decision.silence` | Must-silence scenarios render nothing | 0 | PASS |
| G6 | `decision.requested` | Requested answers are never suppressed | 0 | PASS |
| G7 | `vocabulary.banned` | No machinery vocabulary or raw ids in prose | 0 | PASS |
| G8 | `actions.allowed` | Offered actions are allowed and real | 0 | PASS |
| G9 | `length.max_words` | Word count within the scenario budget | 0 | PASS |
| G10 | `packet.valid` | 100% of packets pass R.validate | 0 | PASS |
| G11 | `batch.discipline` | <=3 findings and <=1 feedback invite per batch | 0 | PASS |
| GC1 | `content.required` | Required claim text reaches the human | 0 | PASS |
| GC2 | `content.forbidden` | Forbidden claim text never reaches the human | 0 | PASS |
| GC3 | `content.uncertainty` | Required uncertainties are stated | 0 | PASS |

## By category

| Category | N | Pass | Fail |
| --- | ---: | ---: | ---: |
| `adversarial` | 6 | 6 | 0 |
| `ask_clear` | 10 | 10 | 0 |
| `ask_no_reply` | 6 | 6 | 0 |
| `ask_uncertain` | 8 | 8 | 0 |
| `checkin` | 5 | 5 | 0 |
| `expense_alternative` | 6 | 6 | 0 |
| `negative_reply` | 8 | 8 | 0 |
| `no_reply` | 10 | 10 | 0 |
| `one_sided` | 10 | 10 | 0 |
| `privacy_trap` | 4 | 4 | 0 |
| `reveal` | 6 | 6 | 0 |
| `strong_personal` | 10 | 10 | 0 |
| `strong_professional` | 15 | 15 | 0 |
| `time_sensitive` | 6 | 6 | 0 |
| `vague_enthusiasm` | 10 | 10 | 0 |

## Soft checks (reported, never fatal)

| Check | Name | Occurrences |
| --- | --- | ---: |
| S1 | `voice.first_person` | 0 |
| S2 | `style.one_question` | 0 |
| S3 | `length.min_words` | 20 |

## Failing scenarios (0)

None.

## Failing batches (0/15)

None.

## What this harness cannot tell you

- **G4 is lexical.** A claim is located in the prose by shared content words. A compiler that paraphrases a claim into entirely different vocabulary escapes the attribution check; a compiler that drops the claim escapes it too, and is caught only by GC1. The two gates work as a pair or not at all.
- **G2 detects shapes, not secrets.** Emails, URLs, social handles and unambiguous phone formats, plus whatever contact values the fixture declares. A contact value written in an unusual form is invisible to it. A loose digit-run pattern was tried and removed: it fired on `$1200-1500 per month`, and a gate with false positives gets switched off.
- **The judge is not evaluated.** See the path note above.
- **Batches are synthetic.** Findings are grouped three at a time in corpus order, which is the contract ceiling; real batching is the matchmaker's decision and is tested in `tests/test_matchmaker.py`.
