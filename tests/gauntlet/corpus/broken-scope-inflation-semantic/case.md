# Corpus case: scope-inflation-semantic

- **Class**: scope-inflation-semantic
- **Broken element**: experiences[0] (exp-aurora), bullets[4]: text and fact_ids. Clean: "Explored a streaming replacement for the nightly batch pipeline." (fact_ids [f-explore]). Broken: "Supported the deployment pipeline of the platform team as its one engineer." (fact_ids [f-platform])
- **Counterfactual pair**: derives from `clean-base`; identical except the broken element.
- **Expected catching layer**: Truth Judge (`gauntlet_truth.md`): at least one valid blocking finding citing the broken element
- **Input gate**: builds with `verifier_passed: true` (spec_version 3); the breakage is invisible to lexical grounding by construction.

The fact says Maya was **one of five** engineers on the platform team supporting the deployment pipeline. The bullet renders "one" as sole ownership: she was the team's one engineer. Ownership implied by construction, not by any verb on the verifier's ownership list ("supported" is in no class), and the number "one" is lexically present in the cited fact, so the deterministic scope rule and the number rule both pass. This is the VERIFY_SOUL incident class, OC-30.

Revision note: the original said "Operated the deployment pipeline..."; "operate" is in no approved state, so the entity and content-word rules rejected it. The inflation mechanism is now carried by the sole-engineer construction instead of an ungrounded verb, which is a stronger form of the same class.
