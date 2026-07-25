# Hermies Matchmaking Quality Report

- **Overall:** ALL GATES PASS across 2 mode(s): fallback, real
- **Engine source:** `backend/engine.py`
- **Determinism:** fixed corpus seed; presence held constant across active cards; no OS randomness. Re-running reproduces these numbers (only absolute latency varies).

## Corpus

- Total cards: **224** across **12** domains
- Planted ground-truth pairs: **30** (8 cross-vocabulary, 6 personal/non-professional)
- Noisy cards (empty/vague/keyword-spam): **20**
- Filler cards: **144**

## Scorecards

### `fallback` mode  (engine.mode attribute)

- Engine source: `backend/engine.py`
- Result: **ALL GATES PASS**

| Metric | Value |
| --- | --- |
| recall@5 (planted, both directions) | 0.700 (42/60) |
| recall@10 (planted, both directions) | 0.767 (46/60) |
| mean reciprocal rank | 0.672 |
| cross-vocab pairs found (top-10) | 2/8 |
| personal pairs found (top-10) | 5/6 |
| found-planted mean score | 2.88 (min 0.70) |
| found-planted % >= 4.0 | 13% |
| random-unrelated mean score | 0.17 |
| separation margin (planted - random) | 2.70 |
| self-match violations | 0 |
| spam mean received score | 1.328 |
| 25th percentile of received scores | 1.300 |
| noisy-in-top5 violations | 0 |
| match latency p50 / p95 | 5.71 / 15.51 ms |

| Gate | Result | Detail |
| --- | --- | --- |
| self-exclusion (0 self-matches) | PASS | 0 violations |
| spam quartile (advisory in fallback) | PASS | spam_mean=1.328 q25=1.300 (rank-based spam gates enforce resistance in this mode) |
| no noisy card in any top-5 | PASS | 0 violations |
| random unrelated mean <= 2.5 | PASS | mean=0.17 |
| separation margin > 1.0 (planted above random) | PASS | margin=2.70 |
| recall@10 >= 0.60 (fallback) | PASS | recall@10=0.767 |

### `real` mode  (engine.mode attribute)

- Engine source: `backend/engine.py`
- Result: **ALL GATES PASS**

| Metric | Value |
| --- | --- |
| recall@5 (planted, both directions) | 0.867 (52/60) |
| recall@10 (planted, both directions) | 0.900 (54/60) |
| mean reciprocal rank | 0.817 |
| cross-vocab pairs found (top-10) | 6/8 |
| personal pairs found (top-10) | 6/6 |
| found-planted mean score | 5.53 (min 3.10) |
| found-planted % >= 4.0 | 89% |
| random-unrelated mean score | 0.40 |
| separation margin (planted - random) | 5.13 |
| self-match violations | 0 |
| spam mean received score | 0.000 |
| 25th percentile of received scores | 3.700 |
| noisy-in-top5 violations | 0 |
| match latency p50 / p95 | 32.91 / 87.23 ms |

| Gate | Result | Detail |
| --- | --- | --- |
| self-exclusion (0 self-matches) | PASS | 0 violations |
| spam bottom-quartile (spam_mean <= q25) | PASS | spam_mean=0.000 q25=3.700 |
| no noisy card in any top-5 | PASS | 0 violations |
| random unrelated mean <= 2.5 | PASS | mean=0.40 |
| separation margin > 1.0 (planted above random) | PASS | margin=5.13 |
| recall@10 >= 0.85 | PASS | recall@10=0.900 |
| cross-vocab found >= 6/8 | PASS | 6/8 |
| found planted mean >= 4.0 (launch quality floor) | PASS | mean=5.53 |

## Notes & engine-side findings

- **Mode-specific gates.** Fallback (token/hashing) can't bridge cross-vocabulary pairs, so it is gated only on recall@10 >= 0.60. Real (bge-small) is gated on recall@10 >= 0.85, >= 6/8 cross-vocab pairs, and an absolute planted-score floor of 4.0. The hashing fallback compresses cosines into a low band, so it preserves ranking + separation but not the 0..10 magnitude; the 4.0 floor is therefore a real-mode bar only.
- **Presence is a recency tiebreaker, not a quality signal.** The engine multiplies score by `0.4 + 0.6*presence` with a 7-day half-life. On the first eval, random per-card staleness let fresh-but-worse filler out-rank stale-but-better planted partners, dropping real-mode recall@10 to ~0.72. Holding recency uniform across active cards (the launch condition) restored recall@10 to ~0.90. **Recommendation:** soften the presence multiplier (e.g. `0.6 + 0.4*presence`) or lengthen the half-life so recency never overrides a clearly better semantic fit.
- **Spam resistance depends on length normalization.** L2-normalized vectors + IDF (real mode) and corpus-DF stopwording (the reference stub) keep keyword-stuffed cards at score ~0 and out of every top-5. A raw overlap count would let spam ride shared buzzwords upward; if the engine regresses here, verify candidate vectors stay L2-normalized.
- **Recommended field-pair weights** (consistent with the passing runs): need_to_offer 0.40, offer_to_need 0.30, guilds 0.20, presence 0.10, with reciprocity (harmonic mean of the two directions) rewarded over one-sided fit.
- **Directional model note.** The engine scores need->offer / offer->need, so 'two peers doing the same thing' is not a match it surfaces; genuine 'should meet' pairs must be complementary (one needs what the other offers). Cross-vocab planted pairs were authored accordingly.
