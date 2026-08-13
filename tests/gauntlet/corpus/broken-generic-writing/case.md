# Corpus case: generic-writing

- **Class**: generic-writing
- **Broken element**: two elements, per the design's definition of this class ("a bullet and summary of grounded filler"): (1) summary becomes "Engineer working with teams and supporting engineers in the Netherlands."; (2) experiences[0] (exp-aurora), bullets[4] becomes "Worked with the team, supporting engineers on the platform." (fact_ids [f-platform])
- **Counterfactual pair**: derives from `clean-base`; identical except the broken element.
- **Expected catching layer**: Writing Judge (`gauntlet_writing.md`): at least one valid blocking finding citing the broken element (blocking under the defined boundary: no concrete action, object, or outcome content)
- **Input gate**: builds with `verifier_passed: true` (spec_version 3); the breakage is invisible to lexical grounding by construction.

Every content word grounds in approved state, yet neither element states a concrete action, object, or outcome beyond role boilerplate.

Interpretation: the pairing rule says one broken element, but the design's generic-writing class explicitly names a bullet AND a summary; this case breaks exactly those two and nothing else. At least one valid blocking finding citing either broken element demonstrates the class.

Revision note: the original filler used "systems" and "clients", which are not in the bullet's source fact, so the verifier rejected it. The replacement filler is built only from the cited fact, the experience row, and the profile.
