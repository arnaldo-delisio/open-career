# Gauntlet regression corpus (blind-authored content)

Authored by the blind corpus author role from the ratified Gauntlet design
(`decisions/gauntlet-design.md`, "The regression corpus") and the content-model and
context schemas (`packages/domain/cv_model.py`, `packages/domain/context.py`), plus the
grounding verifier and its normalization spec (`packages/domain/grounding.py`,
`packages/domain/grounding_spec.py`) as the **input gate every case must clear**. The
author remained blind to the catching layers: the invariant module, the judges, the
judge prompts, and every gauntlet test.

## What is here

One clean synthetic base package for a fictional person, Maya Lindqvist (zero real
personal facts, OC-26), three clean paraphrase controls, and one broken case per class
named in the design. Each case directory holds:

- `context_snapshot.json`: the generation-context snapshot (identical bytes in every
  case; all breakage lives in the content model or its rendered text);
- `content_model.json`: the CV content model, parseable by the closed schema;
- `case.md`: class, exact broken element, counterfactual pair, expected catching layer.

Every broken case is a paired counterfactual of `clean-base`: identical except the
broken element (verify with `diff`). The clean controls must PASS in every demonstration
run. `broken-fabricated-claim` places its breakage in a projects bullet (section
coverage). `missing-requirement` is reserved and deferred to discovery per the ratified
OC-9 amendment.

## The input gate

Every case, clean and broken alike, must clear the shipped deterministic verifier, or its
declared catching layer is never exercised: a lexical rejection fails stage zero on
audit-integrity and no judge ever sees the package. All fifteen cases were checked with

    uv run python scripts/build_corpus_bundle.py tests/gauntlet/corpus/<case> --out <tmp>

and all fifteen report `verifier_passed: True` at `spec_version: 3`. Any future edit to a
case must re-run that check. Concretely this means every broken bullet uses only the
numbers of its own cited facts and only content-word lemmas from those facts, its
experience row, or the profile; every capitalized token exists in approved state; and no
breakage may lean on an ownership verb class the verifier already owns. The breakages
are therefore semantic by necessity, which is exactly the corpus's purpose.

## What is deliberately NOT here

Bundle completion is mechanical and belongs to the **independent test operator** with
the fixture builder, not to the corpus author: the rendered artifact and its extracted
text, verifier and ATS reports, `input_context_hash` / `artifact_hash` values, and the
policy snapshot (including the work-authorization projection and never-render list) are
built by the operator from these pairs after freeze. The corpus content freeze covers
exactly the `context_snapshot.json` / `content_model.json` pairs and the `case.md`
files in these directories, nothing else.

## Author's interpretations (stated, not hidden)

- `fact_type`, capability `strength`, and edge/evidence id values are plausible
  synthetic values; the schemas read by this role do not close those vocabularies.
- Broken elements are addressed in `case.md` by path (e.g. `experiences[0].bullets[2]`,
  `summary`) plus the entry's `experience_id`, since the content model carries no
  freestanding element-id field; the finding validator's element addressing scheme maps
  these mechanically.
- **Two classes moved host element** to clear the input gate, with the class and the
  breakage preserved; each case.md states the move and the reason.
  - `inconsistent-date` moved from an experience bullet to the summary. Bullet numbers
    are checked against that bullet's own source facts, and no fact statement in this
    corpus carries a year (years live only in canonical experience rows, which are not a
    numeric source for bullets), so a year-bearing date contradiction is **not
    constructible in a bullet** under spec 3. The summary, whose numbers check against
    the whole renderable view, is the only element where the class exists.
  - `invented-metric` moved to an Aurora bullet, because no BrightCart fact carries a
    number for an invented metric to borrow, and the number rule is per-bullet.
- **One class changed form**, not strength: `negation-reversal` is now a polarity
  negation ("kept uptime without maintaining the systems") rather than a modality
  upgrade ("explored" rendered as "delivered"). Upgrading to a stronger verb is
  structurally unavailable under the gate, since the stronger verbs are precisely the
  members of the verifier's ownership verb classes; the class is caught by polarity
  instead. No class was found impossible outright.
