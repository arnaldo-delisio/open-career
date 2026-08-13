# Corpus case: negation-reversal

- **Class**: negation-reversal
- **Broken element**: experiences[0] (exp-aurora), bullets[3]: "Maintained the legacy billing systems and kept uptime at 99.9 percent." becomes "Kept uptime at 99.9 percent without maintaining the legacy billing systems." (fact_ids unchanged: [f-oncall])
- **Counterfactual pair**: derives from `clean-base`; identical except the broken element.
- **Expected catching layer**: Truth Judge (`gauntlet_truth.md`): at least one valid blocking finding citing the broken element
- **Input gate**: builds with `verifier_passed: true` (spec_version 3); the breakage is invisible to lexical grounding by construction.

The fact states that Maya maintained the legacy billing systems and kept uptime at 99.9 percent. The bullet keeps every word and flips the polarity of half the claim with a function word: the maintenance responsibility the fact asserts is now explicitly denied, and the uptime is implicitly credited to something else. This is the design's own example shape (a supported claim with its polarity flipped), built entirely from the fact's own vocabulary.

Revision note: the original flipped "explored" to "delivered"; "deliver" is both ungrounded in the cited fact and a member of the verifier's `launch` ownership class, so the deterministic layer caught it and the judge was never exercised. A modality flip toward a stronger verb is structurally unavailable under the input gate, since the stronger verbs are exactly the ones the scope rule owns; polarity negation with function words is the form of this class that survives the gate.
