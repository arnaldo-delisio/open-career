# Corpus case: fabricated-claim

- **Class**: fabricated-claim
- **Broken element**: projects[0] (exp-openroute), bullets[0]: text changed from "Maintains OpenRoute, an open-source route planning library, and reviews community pull requests." to "Built OpenRoute, the open-source route planning library adopted by Dutch logistics startups." (fact f-openroute-maint says she maintains and reviews; no fact says she built it; every content word is lexically grounded elsewhere: "built" in f-clients, "adopted"/"Dutch logistics startups" in f-openroute-adopt)
- **Counterfactual pair**: derives from `clean-base`; byte-identical except the broken element.
- **Expected catching layer**: Truth Judge (`gauntlet_truth.md`): at least one valid blocking finding citing the broken element

This is the section-coverage case: the breakage lives in a projects bullet. It is the lexically-grounded semantic fabrication the verifier passes by construction: supported words recombined into a claim the facts do not make.
