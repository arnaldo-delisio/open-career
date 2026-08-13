# Corpus case: clean (paraphrase 1)

- **Class**: clean control
- **Broken element**: none
- **Counterfactual pair**: paraphrase variant of `clean-base` (summary and two bullets
  reworded; all claims stay within the facts).
- **Expected catching layer**: none; the run must PASS.
- **Input gate**: builds with `verifier_passed: true` (spec_version 3).

Revision note 1: the original wording used "based in Amsterdam"; the verifier's
lemmatizer maps "based" to `bas`, which no approved state carries, so a clean control was
failing the input gate. Reworded to "in Amsterdam" with no other change.

Revision note 2: this control's summary says "working on order, checkout, and routing
systems". A demonstration run's Consistency Judge correctly flagged that as unsupported
present tense when `exp-aurora` still ended in 2024-06. The canonical timeline was
repaired instead of the sentence: `exp-aurora` is now a current role (2021-03, open), so
the present tense is supported against the 2026-08 generation date (corpus README, "The
canonical timeline").
