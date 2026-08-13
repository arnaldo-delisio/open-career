# Corpus case: false-causality

- **Class**: false-causality
- **Broken element**: experiences[0] (exp-aurora), bullets[0]: the migration claim gains ", reducing monthly hosting costs by 30 percent" (fact_ids extended to [f-migration, f-costs]). Both facts are separately true; no fact links the migration to the cost reduction (f-costs attributes it to cluster consolidation by the team)
- **Counterfactual pair**: derives from `clean-base`; byte-identical except the broken element.
- **Expected catching layer**: Truth Judge (`gauntlet_truth.md`): at least one valid blocking finding citing the broken element

Two separately true facts joined into an unsupported causal chain (the design's own example).
