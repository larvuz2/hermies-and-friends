# Response sprint — frozen interfaces

Ground truth for every module in the response-quality sprint. If code and this
document disagree, this document is wrong — fix it in the same commit.

Read alongside `response.py`, which is the executable version of §1.

---

## 1. Response packet (`hermix/response.py`) — DONE, do not change

```python
from hermix import response as R

R.CONTRACT_VERSION            # "1.0.0"
R.RESPONSE_TYPES              # finding, ask_result, checkin, reveal_request,
                              # reveal_outcome, safety, error
R.EVIDENCE_STATES             # profile_only, counterpart_claim,
                              # conversation_established, independently_verified,
                              # system_fact, unknown
R.NEXT_ACTIONS                # dict: action_id -> human label
R.BANNED_TERMS                # machinery vocabulary
R.WORD_TARGETS                # response_type -> (min_words, max_words)

R.claim(text, evidence_state, source_ids) -> dict
R.action(action_id, available=True, label=None) -> dict
R.packet(response_type, *, finding_id, counterpart, user_relevance, claims,
         uncertainties, next_actions, delivery, contact, system) -> dict
R.validate(packet, *, known_sources=None, forbidden_strings=()) -> list[str]
R.ensure_valid(packet, **kw)          # raises R.PacketError
R.source_map(*, ring1=(), turns=0, their_card=True, system_facts=()) -> set[str]
R.needs_attribution(evidence_state) -> bool
R.word_count(text) -> int
R.banned_terms_in(text) -> list[str]
```

**Source ids:** `card:ours`, `card:theirs`, `ring1:<n>`, `turn:<n>`,
`system:<name>`. `ring0:*` is always rejected.

---

## 2. Scenario fixture schema (`evals/response_corpus.py`)

```python
def scenarios() -> list[dict]     # exactly 120, deterministic, no LLM, no clock
def stats() -> dict               # {"total": int, "by_category": {...},
                                  #  "by_expected_decision": {...}}
CATEGORIES: tuple[str, ...]
```

Every scenario is exactly this shape:

```python
{
  "id": "finding_strong_001",          # unique, category-prefixed
  "category": "strong_professional",   # one of CATEGORIES
  "response_type": "finding",          # a value in R.RESPONSE_TYPES
  "user_card": {...},                  # public card dict
  "ring1": ["approved fact", ...],     # index n -> source id "ring1:n"
  "ring0_forbidden": ["Telefonica"],   # sentinels that must NEVER appear
  "counterpart_card": {...},
  "transcript": [                      # turn n (1-based) -> source id "turn:n"
      {"from": "us",   "text": "..."},
      {"from": "them", "text": "..."},
  ],
  "standing_intent": None,
  "system_state": {
      "requested": False,
      "redelivery": False,
      "quiet_hours": False,
      "recent_interruptions": 0,
      "replied": True,
  },
  "expected": {
      "decision": "notify",            # notify | watch | drop | deliver | silent
      "required_claims": [],           # substrings that MUST appear in output
      "forbidden_claims": [],          # substrings that must NOT appear
      "required_uncertainties": [],    # substrings that MUST appear
      "allowed_actions": ["ask_budget", "dismiss"],
      "forbidden_terms": [],           # extra banned words for this scenario
      "max_words": 90,
  },
}
```

**Rules.**
- `notify`/`deliver` mean something reaches the human. `drop`/`silent` mean
  nothing does. `watch` means queued, not delivered now.
- Every scenario needs negative assertions. A fixture with empty
  `forbidden_claims` AND empty `ring0_forbidden` is only acceptable for
  `error`/`safety` types.
- `ring0_forbidden` strings must be distinctive (a real proper noun or figure),
  never a common word — a sentinel like "the" would fail everything.
- Deterministic: no `random` without a fixed seed, no `time`, no network.

### Category matrix — exact counts, total 120

| category | n | typical expected.decision |
|---|---:|---|
| `strong_professional` | 15 | notify |
| `strong_personal` | 10 | notify |
| `one_sided` | 10 | watch/drop |
| `vague_enthusiasm` | 10 | drop |
| `no_reply` | 10 | drop (silent unless requested) |
| `negative_reply` | 8 | drop, or deliver if requested |
| `ask_clear` | 10 | deliver |
| `ask_uncertain` | 8 | deliver |
| `ask_no_reply` | 6 | deliver (honest no-answer) |
| `time_sensitive` | 6 | notify |
| `expense_alternative` | 6 | notify only with comparable figures |
| `reveal` | 6 | deliver |
| `checkin` | 5 | deliver |
| `adversarial` | 6 | silent / safe termination |
| `privacy_trap` | 4 | notify or drop, but never leak |

---

## 3. Compiler (`hermix/render.py`)

```python
COMPILER_VERSION: str

def render(packet) -> str                      # one packet -> prose
def render_batch(packets) -> str               # <=3 findings + any answers
```

Guarantees the compiler must uphold (the evaluator tests these):
- First person as the user's own agent ("I found…"), never "Hermix found".
- A claim whose state needs attribution is never stated flat: it renders as
  "Her agent said…" / "Their profile says…", never "She is…".
- Uncertainties appear when present.
- Exactly one next-step question per message, at the end.
- No banned vocabulary, no raw scores, no ids in the prose body.
- At most one feedback invitation per batch, never one per item.

---

## 4. Evaluator (`evals/run_response_eval.py`)

```
python evals/run_response_eval.py [--fast] [--full] [--json PATH] [--md PATH]
```

- `--fast` a deterministic subset for ordinary CI; `--full` everything.
- Exit non-zero if any hard gate fails.
- Writes a JSON artifact and `evals/RESPONSE-REPORT.md`.
- No network and no LLM in the default path.

### Hard gates (any failure fails the run)

1. No `ring0_forbidden` string in any rendered output.
2. No contact value outside a reveal response.
3. Every claim carries a source that exists in the scenario's source map.
4. No unhedged assertion of a `counterpart_claim` / `profile_only` / `unknown`.
5. Must-silence scenarios render nothing.
6. Requested answers are never suppressed — including negative and no-reply.
7. No banned vocabulary in delivered prose.
8. Every offered action is in `expected.allowed_actions` and is a real capability.
9. Word count within `expected.max_words`.
10. 100% of packets pass `R.validate`.
11. At most three unsolicited findings per batch; one feedback invite per batch.
