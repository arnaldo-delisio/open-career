# Corpus case: clean (base)

- **Class**: clean control
- **Broken element**: none
- **Counterfactual pair**: this is the base every broken case derives from.
- **Expected catching layer**: none; the run must PASS (stage zero pass, all judges PASS,
  no blocking findings).
- **Input gate**: builds with `verifier_passed: true` (spec_version 3).

The canonical timeline is load-bearing here, see the corpus README: `exp-aurora` is a
current role (2021-03, open), so present-tense employment claims in this base and in its
paraphrase variants are supported against a 2026-08 generation date. An earlier revision
closed that role in 2024-06, and a real demonstration run correctly flagged the resulting
present-tense summary as a timeline inconsistency inside a clean control.
