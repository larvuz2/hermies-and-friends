# Hermies Matchmaking — Production Calibration

Recommended threshold values on the engine's **0..10** score, derived from the
eval corpus (224 cards, 30 planted "should-obviously-meet" pairs, 20 noisy).
All numbers below are from **real-model mode** (`fastembed` / bge-small), which
is what production runs; the hashing fallback compresses scores into a lower
band (see the last section).

## Observed score distribution (real mode)

| Population | mean | p25 | median | p95 | max |
| --- | --- | --- | --- | --- | --- |
| Genuine planted matches (surfaced) | 5.5 | 4.6 | 5.8 | — | 7.0 (min 3.1) |
| Random unrelated pairs | 0.38 | ~0 | 0 | 4.1 | 5.8 |
| Noisy / spam candidates | ~0.0 | 0 | 0 | 0 | 0 |

Genuine matches sit at **~4.6–7.0**; noise sits at **0**; random pairs are
mostly 0 with a thin tail (same-domain coincidental affinity) reaching ~5. The
two populations overlap only in a narrow 3–5 band, so that band is where the
thresholds below trade recall against precision.

## Recommended thresholds

| Setting | Recommended | One-line justification |
| --- | --- | --- |
| **Signal-display floor** (min score to surface a candidate at all) | **2.5** | Below the weakest genuine match (3.1) with margin, well above the random mean (0.38) and all spam (0) — surfaces real matches without showing noise. |
| **`HERMIES_MIN_SCORE`** (plugin cheap-filter default, currently 3) | **Keep 3.0** (2.5 for max recall) | 3.0 sits just under the weakest genuine match (3.1) and far above the random mean — the current default is well-placed; drop to 2.5 only if missing a borderline match costs more than a little extra noise. |
| **Handshake-worthy threshold** (spend a real intro / consider interrupting) | **5.0** | Captures the bulk of genuine mutual fits (median 5.8) while excluding all but ~1% of random pairs (random p99 = 5.0); a handshake spends social capital, so bias to precision. Pair with the LLM judge as the second gate. |

## Strong / medium / weak labels shown to users

| Label | Range | Meaning |
| --- | --- | --- |
| **Strong** | **>= 5.0** | Confident mutual fit; handshake-worthy. Genuine matches cluster here (median 5.8); ~99% of random pairs never reach it. |
| **Medium** | **3.0 – 5.0** | Plausible; one clear direction of fit. Worth showing / watching, not auto-introducing. Most genuine matches down to 3.1 live at or above this floor. |
| **Weak** | **< 3.0** | Dominated by coincidental or noisy overlap. Suppress or bury at the bottom. |

## Mode caveat (important)

These absolute cutoffs are for **real-model (fastembed) mode**. The dependency-
light **hashing fallback compresses the band**: genuine matches there average
~2.9 (min ~0.7) rather than ~5.5. If a deployment ever runs in fallback mode,
either (a) scale every threshold above by ~0.5x, or (b) switch to **rank-based**
cutoffs (e.g. top-5 with a positive score) instead of absolute thresholds. The
engine advertises its mode via `engine.mode` (`"fallback"` vs `"fastembed"`), so
the plugin can pick the right threshold set at runtime. Production should run
fastembed; treat fallback as degraded (matching stays up, thresholds soften).
