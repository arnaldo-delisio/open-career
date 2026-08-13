# Corpus case: clean control

- **Class**: clean control
- **Broken element**: none
- **Counterfactual pair**: derives from ``clean-base` (paraphrase variant: summary and two bullets reworded; all claims stay within the facts)`; identical except the broken element.
- **Expected catching layer**: none; the run must PASS
- **Input gate**: builds with `verifier_passed: true` (spec_version 3); the breakage is invisible to lexical grounding by construction.

Revision note: the original wording used "based in Amsterdam"; the verifier's lemmatizer maps "based" to `bas`, which no approved state carries, so a clean control was failing the input gate. Reworded to "in Amsterdam" with no other change.
