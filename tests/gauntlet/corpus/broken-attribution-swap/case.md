# Corpus case: attribution-swap

- **Class**: attribution-swap
- **Broken element**: experiences[0] (exp-aurora), bullets[1]: "Worked on the team that reduced monthly hosting costs by 30 percent..." becomes "Reduced monthly hosting costs by 30 percent by consolidating three Kubernetes clusters into one." The cited fact f-costs explicitly credits "The team"; the bullet claims the outcome personally. The fact attachment (fact_ids: [f-costs], same entry exp-aurora) stays honest, so deterministic traceability passes
- **Counterfactual pair**: derives from `clean-base`; byte-identical except the broken element.
- **Expected catching layer**: Truth Judge (`gauntlet_truth.md`): at least one valid blocking finding citing the broken element (an attribution finding: the fact statement names a conflicting actor, "the team", for the outcome the bullet claims personally)

Same-entry subject misattribution. Cross-entry fact attachment is verifier territory and deliberately not what this case tests.
