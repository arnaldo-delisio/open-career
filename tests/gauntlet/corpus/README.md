# Gauntlet regression corpus (blind-authored content)

Authored by the blind corpus author role from the ratified Gauntlet design
(`decisions/gauntlet-design.md`, "The regression corpus") and the content-model and
context schemas (`packages/domain/cv_model.py`, `packages/domain/context.py`) only,
without reading the invariant or judge implementations, the verifier, prompts, or
existing tests.

## What is here

One clean synthetic base package for a fictional person, Maya Lindqvist (zero real
personal facts, OC-26), three clean paraphrase controls, and one broken case per class
named in the design. Each case directory holds:

- `context_snapshot.json`: the generation-context snapshot (identical bytes in every
  case; all breakage lives in the content model or its rendered text);
- `content_model.json`: the CV content model, parseable by the closed schema;
- `case.md`: class, exact broken element, counterfactual pair, expected catching layer.

Every broken case is a paired counterfactual of `clean-base`: byte-identical except the
broken element (verify with `diff`). The clean controls must PASS in every demonstration
run. `broken-fabricated-claim` places its breakage in a projects bullet (section
coverage). `missing-requirement` is reserved and deferred to discovery per the ratified
OC-9 amendment.

## What is deliberately NOT here

Bundle completion is mechanical and belongs to the **independent test operator** with
the fixture builder, not to the corpus author: the rendered artifact and its extracted
text, verifier and ATS reports, `input_context_hash` / `artifact_hash` values, and the
policy snapshot (including the work-authorization projection and never-render list) are
built by the operator from these pairs after freeze. The corpus content freeze covers
exactly the `context_snapshot.json` / `content_model.json` pairs and the `case.md`
files in these directories, nothing else.

## Author's interpretations (stated, not hidden)

- `normalization_spec_version` is written as `1` as a placeholder: the shipped
  `SPEC_VERSION` was not readable under the blindness rule. The fixture builder aligns
  it mechanically at bundle completion; the corpus claim does not depend on its value.
- `fact_type`, capability `strength`, and edge/evidence id values are plausible
  synthetic values; the schemas read by this role do not close those vocabularies.
- Broken elements are addressed in `case.md` by path (e.g. `experiences[0].bullets[2]`,
  `summary`) plus the entry's `experience_id`, since the content model carries no
  freestanding element-id field; the finding validator's element addressing scheme maps
  these mechanically.
- All broken text is built only from words, numbers, and dates present in the
  renderable grounding view (facts, experiences, profile, capabilities), so the lexical
  verifier passes each broken case by construction, as the design requires for every
  judge-layer class.
