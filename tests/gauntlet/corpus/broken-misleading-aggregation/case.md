# Corpus case: misleading-aggregation

- **Class**: misleading-aggregation
- **Broken element**: experiences[0] (exp-aurora), bullets[2]: "from 220 ms to 90 ms" becomes "by 220 ms". Both numbers ground lexically in f-latency, but the actual reduction the fact supports is 130 ms (220 to 90); rendering the starting value as the size of the reduction inflates the implied figure
- **Counterfactual pair**: derives from `clean-base`; byte-identical except the broken element.
- **Expected catching layer**: Truth Judge (`gauntlet_truth.md`): at least one valid blocking finding citing the broken element

Grounded numbers recombined into a larger implied figure; no ungrounded token exists for the verifier to catch.
