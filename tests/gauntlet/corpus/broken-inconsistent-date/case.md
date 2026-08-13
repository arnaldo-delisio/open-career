# Corpus case: inconsistent-date

- **Class**: inconsistent-date
- **Broken element**: summary: "Backend engineer in Amsterdam. ..." becomes "Backend engineer in Amsterdam since 2015. ..." (the rest of the summary is unchanged)
- **Counterfactual pair**: derives from `clean-base`; identical except the broken element.
- **Expected catching layer**: Consistency Judge (`gauntlet_consistency.md`): at least one valid blocking finding citing the broken element (a finding naming two elements: the summary and the canonical experience dates it contradicts)
- **Input gate**: builds with `verifier_passed: true` (spec_version 3); the breakage is invisible to lexical grounding by construction.

2015 grounds lexically in approved state (the University of Rotterdam row starts 2015-09), and stage zero's date-coherence rule sees no violation: every entry's start still precedes its end, the order is reverse-chronological, and no date postdates generated_at. Only the narrative is broken: in 2015 Maya was starting a BSc, and her first role begins 2018-09, so "backend engineer since 2015" contradicts the entries beneath it.

**Interpretation, stated explicitly**: the original placed this in an experience bullet ("the 2018 migration"), which cannot pass the input gate. Bullet-level numbers are checked against that bullet's own source facts, and no fact statement in the corpus carries a year at all (years live only in the canonical experience rows, which are not a numeric source for bullets). A year-bearing inconsistent-date breakage is therefore impossible in a bullet under this spec; the summary, whose numbers are checked against the whole renderable view, is the only element where the class can be built. The class and the breakage are unchanged; the host element moved from a bullet to the summary.
