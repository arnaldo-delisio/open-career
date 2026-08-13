# Corpus case: cross-section-contradiction

- **Class**: cross-section-contradiction
- **Broken element**: summary: "Backend engineer in Amsterdam." becomes "Backend engineer in Rotterdam." ("Rotterdam" grounds lexically via the University of Rotterdam row)
- **Counterfactual pair**: derives from `clean-base`; identical except the broken element.
- **Expected catching layer**: Consistency Judge (`gauntlet_consistency.md`): at least one valid blocking finding citing the broken element (a finding naming two elements: the summary and the profile/header location field)
- **Input gate**: builds with `verifier_passed: true` (spec_version 3); the breakage is invisible to lexical grounding by construction.

A location narrative conflict between the summary and the profile, with every token individually grounded. Unchanged from the original authoring; it already passed the input gate.
