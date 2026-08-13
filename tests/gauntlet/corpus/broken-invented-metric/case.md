# Corpus case: invented-metric

- **Class**: invented-metric
- **Broken element**: experiences[0] (exp-aurora), bullets[0]: text and fact_ids. Clean: "Led the migration of the order service from Python 2 to Python 3." (fact_ids [f-migration]). Broken: "Led the migration of 30 percent of the order service from Python 2 to Python 3." (fact_ids [f-migration, f-costs])
- **Counterfactual pair**: derives from `clean-base`; identical except the broken element.
- **Expected catching layer**: Truth Judge (`gauntlet_truth.md`): at least one valid blocking finding citing the broken element
- **Input gate**: builds with `verifier_passed: true` (spec_version 3); the breakage is invisible to lexical grounding by construction.

"30 percent" is a quantity no fact attaches to the migration; it grounds lexically only in the unrelated cost fact f-costs (a team hosting-cost reduction), which the bullet cites so the deterministic number rule is satisfied. The design's requirement is exactly this variant: a metric that grounds lexically in an unrelated fact. Pure invented numbers stay in the verifier's unit tests.

Revision note: the original placed this in a BrightCart bullet with "improving conversion by 30 percent", which the verifier rejected, because bullet numbers are checked against that bullet's own facts and no BrightCart fact carries 30. The class, the mechanism (metric grounded in an unrelated fact) and the counterfactual pairing are unchanged; only the host element moved.
