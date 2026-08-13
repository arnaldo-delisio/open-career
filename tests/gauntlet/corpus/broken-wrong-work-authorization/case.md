# Corpus case: wrong-work-authorization

- **Class**: wrong-work-authorization
- **Broken element**: summary: the sentence "Visa sponsorship required in the Netherlands." is appended (nothing else changes)
- **Counterfactual pair**: derives from `clean-base`; identical except the broken element.
- **Expected catching layer**: stage zero, rule `work-authorization` (deterministic; no judge runs, disposition `fail`)
- **Input gate**: builds with `verifier_passed: true` (spec_version 3); the breakage is invisible to lexical grounding by construction.

The profile carries authorized_in_country "yes" and needs_sponsorship "no"; this authorization-class assertion matches no allowed form of the projection built from those fields. Every word of it grounds lexically via the profile's own work-authorization note ("Authorized to work in the Netherlands; no visa sponsorship required."), so the case reaches stage zero as the design requires.

Revision note: the original wrote "Requires visa sponsorship..."; the spec lemmatizes "required" to `requir` and "requires" to `require`, so the profile's own word did not cover the inflected form and the verifier rejected it. Reworded to the past participle; the assertion and its contradiction of the profile are identical.
