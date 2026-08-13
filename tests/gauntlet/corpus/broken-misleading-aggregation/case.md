# Corpus case: misleading-aggregation

- **Class**: misleading-aggregation
- **Broken element**: experiences[0] (exp-aurora), bullets[2]: "Rewrote the routing cache, cutting median API latency from 220 ms to 90 ms." becomes "Rewrote the routing cache, cutting median API latency by 220 ms." (fact_ids unchanged: [f-latency])
- **Counterfactual pair**: derives from `clean-base`; identical except the broken element.
- **Expected catching layer**: Truth Judge (`gauntlet_truth.md`): at least one valid blocking finding citing the broken element
- **Input gate**: builds with `verifier_passed: true` (spec_version 3); the breakage is invisible to lexical grounding by construction.

Both numbers ground in the cited fact, but the reduction it supports is 130 ms (220 to 90); rendering the starting value as the size of the reduction inflates the implied figure. Unchanged from the original authoring; it already passed the input gate.
