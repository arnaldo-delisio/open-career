# Corpus case: attribution-swap

- **Class**: attribution-swap
- **Broken element**: experiences[0] (exp-aurora), bullets[1]: "Worked on the team that reduced monthly hosting costs by 30 percent by consolidating three Kubernetes clusters into one." becomes "Reduced monthly hosting costs by 30 percent by consolidating three Kubernetes clusters into one." (fact_ids unchanged: [f-costs])
- **Counterfactual pair**: derives from `clean-base`; identical except the broken element.
- **Expected catching layer**: Truth Judge (`gauntlet_truth.md`): at least one valid blocking finding citing the broken element (an attribution finding: the cited fact's statement names a conflicting actor, "the team", for the outcome the bullet claims personally)
- **Input gate**: builds with `verifier_passed: true` (spec_version 3); the breakage is invisible to lexical grounding by construction.

Same-entry subject misattribution. The fact attachment stays honest (same entry, correct fact), so deterministic traceability passes; cross-entry fact attachment is verifier territory and deliberately not what this case tests. Unchanged from the original authoring; it already passed the input gate.
