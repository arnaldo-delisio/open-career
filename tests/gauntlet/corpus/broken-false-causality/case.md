# Corpus case: false-causality

- **Class**: false-causality
- **Broken element**: experiences[0] (exp-aurora), bullets[0]: "Led the migration of the order service from Python 2 to Python 3." becomes "Led the migration of the order service from Python 2 to Python 3, reducing monthly hosting costs by 30 percent." (fact_ids [f-migration] becomes [f-migration, f-costs])
- **Counterfactual pair**: derives from `clean-base`; identical except the broken element.
- **Expected catching layer**: Truth Judge (`gauntlet_truth.md`): at least one valid blocking finding citing the broken element
- **Input gate**: builds with `verifier_passed: true` (spec_version 3); the breakage is invisible to lexical grounding by construction.

Both facts are separately true; no fact links the migration to the cost reduction, which f-costs attributes to the team consolidating clusters. Unchanged from the original authoring; it already passed the input gate.
