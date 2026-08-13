# Corpus case: inconsistent-date

- **Class**: inconsistent-date
- **Broken element**: experiences[0] (exp-aurora), bullets[0]: "the migration" becomes "the 2018 migration". "2018" is individually grounded (BrightCart start 2018-09; University of Rotterdam end 2018-06) but the Aurora Logistics entry runs 2021-03 to 2024-06, so the narrative contradicts: the bullet dates Aurora work three years before the role began
- **Counterfactual pair**: derives from `clean-base`; byte-identical except the broken element.
- **Expected catching layer**: Consistency Judge (`gauntlet_consistency.md`): at least one valid blocking finding citing the broken element (a finding naming two elements: the bullet and the exp-aurora entry dates)

Every date grounds lexically and stage zero's date-coherence rule sees no ordering violation (entry start still precedes end; no date postdates generated_at); only the cross-claim narrative is broken.
