# Corpus case: fabricated-claim

- **Class**: fabricated-claim
- **Broken element**: projects[0] (exp-openroute), bullets[0]: text and fact_ids. Clean text: "Maintains OpenRoute, an open-source route planning library, and reviews community pull requests." (fact_ids [f-openroute-maint]). Broken text: "Maintains OpenRoute, the open-source route planning library, for the two Dutch logistics startups that adopted it, and reviews their community pull requests." (fact_ids [f-openroute-maint, f-openroute-adopt])
- **Counterfactual pair**: derives from `clean-base`; identical except the broken element.
- **Expected catching layer**: Truth Judge (`gauntlet_truth.md`): at least one valid blocking finding citing the broken element
- **Input gate**: builds with `verifier_passed: true` (spec_version 3); the breakage is invisible to lexical grounding by construction.

The facts state two separate things: Maya maintains OpenRoute and reviews community pull requests, and OpenRoute was adopted by two Dutch logistics startups. The bullet recombines them into a claim no fact makes: that she maintains the library **for** those startups (a client relationship) and that the pull requests she reviews are **theirs**. Every content word, number, and entity comes from the two cited facts, so lexical grounding passes.

This is also the section-coverage case: the breakage lives in a projects bullet.
