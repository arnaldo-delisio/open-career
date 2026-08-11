"""The output gate: deterministic grounding verifier (spec:
decisions/package-generation-design.md, "The output gate"). Pure code, no
model call. Input: the content model plus the generation context that
produced it; every rule runs exclusively against the renderable grounding
view, never the strategy block.

What it guarantees, stated honestly (OC-13): lexical grounding, meaning no
number, date, entity, or content word in the rendered document that is absent
from approved career state. Semantic entailment is the Gauntlet's job (OC-9).

The verifier never rewrites text into compliance silently; it rejects, and
the pipeline retries or falls back to the fact statement verbatim."""

import json
from dataclasses import dataclass

from domain.context import GenerationContext
from domain.cv_model import Bullet, CvExperienceEntry, CvModel, SECTION_ORDER
from domain.grounding_spec import (
    SPEC_VERSION,
    content_lemmas,
    is_entity_like,
    lemma,
    lemma_set,
    normalize,
    normalize_chars,
    numeric_tokens,
    tokenize,
    tokenize_preserving_case,
)

# Rule 4: scope inflation. Ownership verb classes (the named OC-30 regression
# list): a verb of a class may appear in a bullet only if the underlying fact
# statement carries that verb class ("used X" never renders as "built X").
OWNERSHIP_VERB_CLASSES: dict[str, frozenset[str]] = {
    "lead": frozenset({"lead", "spearhead", "head", "direct", "drive"}),
    "build": frozenset({"build", "create", "develop", "architect", "engineer",
                        "design", "implement"}),
    "found": frozenset({"found", "cofound", "co-found", "establish", "start"}),
    "manage": frozenset({"manage", "oversee", "supervise", "coordinate"}),
    "own": frozenset({"own"}),
    "launch": frozenset({"launch", "ship", "deliver", "release"}),
}


@dataclass(frozen=True)
class Finding:
    rule: str
    element: str
    message: str


@dataclass(frozen=True)
class GroundingReport:
    spec_version: str
    findings: tuple[Finding, ...]

    @property
    def passed(self) -> bool:
        return not self.findings

    def to_json(self) -> str:
        return json.dumps({
            "spec_version": self.spec_version,
            "passed": self.passed,
            "findings": [{"rule": f.rule, "element": f.element, "message": f.message}
                         for f in self.findings],
        }, indent=2, sort_keys=True)


class GroundingVerifier:
    def __init__(self, context: GenerationContext):
        self._context = context
        self._view = context.renderable_grounding_view()
        self._corpus = self._build_corpus()
        self._corpus_numeric = numeric_tokens(self._corpus)
        self._corpus_lemmas = lemma_set(self._corpus)
        self._corpus_tokens = {t.casefold() for t in tokenize(self._corpus)}
        self._corpus_content = content_lemmas(self._corpus)

    def _build_corpus(self) -> str:
        """All renderable grounding text: fact statements, experience rows,
        profile values, selected capability names. Nothing else."""
        parts = [f["statement"] for f in self._view["facts"].values()]
        for e in self._view["experiences"].values():
            parts.extend(str(v) for v in e.values() if v)
        parts.extend(str(v) for v in self._view["profile"].values() if v)
        parts.extend(c["name"] for c in self._view["capabilities"].values())
        return "\n".join(parts)

    # -- public ------------------------------------------------------------

    def verify(self, cv: CvModel) -> GroundingReport:
        findings: list[Finding] = []
        findings.extend(self._check_meta(cv))
        findings.extend(self._check_header(cv))
        findings.extend(self._check_summary(cv))
        findings.extend(self._check_skills(cv))
        for section, entries in (("experiences", cv.experiences),
                                 ("projects", cv.projects), ("education", cv.education)):
            for entry in entries:
                findings.extend(self._check_entry(section, entry))
        return GroundingReport(spec_version=SPEC_VERSION, findings=tuple(findings))

    # -- rules -------------------------------------------------------------

    def _check_meta(self, cv: CvModel) -> list[Finding]:
        findings = []
        if cv.meta.section_order != SECTION_ORDER:
            findings.append(Finding("section-order", "meta",
                                    f"section order must be {list(SECTION_ORDER)}"))
        if cv.meta.role_family_id != self._context.role_family_id:
            findings.append(Finding("traceability", "meta",
                                    "role_family_id does not match the generation context"))
        return findings

    def _check_header(self, cv: CvModel) -> list[Finding]:
        """Header fields must equal profile fields exactly (from user_profile,
        nothing model-decided)."""
        findings = []
        profile = self._view["profile"]
        checks = (("name", cv.header.name, profile.get("full_name")),
                  ("email", cv.header.email, profile.get("email")),
                  ("phone", cv.header.phone, profile.get("phone")),
                  ("location", cv.header.location, profile.get("location")))
        for field, rendered, canonical in checks:
            if rendered is not None and _neq(rendered, canonical):
                findings.append(Finding(
                    "header", f"header.{field}",
                    f"'{rendered}' does not match the profile value '{canonical}'"))
        link_values = {normalize_chars(str(profile.get(f) or ""))
                       for f in ("linkedin_url", "github_url", "portfolio_url", "website_url")}
        for link in cv.header.links:
            if normalize_chars(link) not in link_values:
                findings.append(Finding("header", "header.links",
                                        f"link '{link}' is not a profile link"))
        return findings

    def _check_summary(self, cv: CvModel) -> list[Finding]:
        """Summary: numbers, entities, and content words against the whole
        renderable grounding view (never the strategy block)."""
        findings = []
        findings.extend(self._check_numbers("summary", cv.summary, self._corpus_numeric))
        findings.extend(self._check_entities("summary", cv.summary))
        missing = content_lemmas(cv.summary) - self._corpus_content
        if missing:
            findings.append(Finding(
                "content-words", "summary",
                f"content words not grounded in approved state: {sorted(missing)}"))
        return findings

    def _check_skills(self, cv: CvModel) -> list[Finding]:
        """Every skill's capability ids must be covered in the selection report
        (at least one eligible chain ending in an approved active fact) and the
        display name must come from the canonical capability row."""
        findings = []
        capabilities = self._view["capabilities"]
        for skill in cv.skills:
            element = f"skills[{skill.name}]"
            names = set()
            for capability_id in skill.capability_ids:
                row = capabilities.get(capability_id)
                if row is None or not self._context.selection.facts_for_capability(capability_id):
                    findings.append(Finding(
                        "skills", element,
                        f"capability '{capability_id}' has no eligible evidence chain"
                        " ending in an approved active fact (uncovered capabilities"
                        " live only in the gap report)"))
                    continue
                names.add(normalize(row["name"]))
            if names and normalize(skill.name) not in names:
                findings.append(Finding(
                    "skills", element,
                    f"display name '{skill.name}' is not the canonical name of any"
                    f" listed capability"))
        return findings

    def _check_entry(self, section: str, entry: CvExperienceEntry) -> list[Finding]:
        findings = []
        element = f"{section}[{entry.experience_id}]"
        experience = self._view["experiences"].get(entry.experience_id)
        if experience is None:
            findings.append(Finding("traceability", element,
                                    "experience_id is not a confirmed experience row"))
            return findings
        # Skeleton fields directly against the confirmed canonical row: a
        # valid canonical date needs no repeating fact.
        for field, rendered, canonical in (
                ("title", entry.title, experience["title"]),
                ("org", entry.org, experience["org"]),
                ("start_date", entry.start_date, experience["start_date"]),
                ("end_date", entry.end_date, experience["end_date"])):
            if rendered is not None and _neq(rendered, canonical):
                findings.append(Finding(
                    "skeleton", f"{element}.{field}",
                    f"'{rendered}' does not match the canonical row value '{canonical}'"))
        if entry.location is not None:
            findings.append(Finding("skeleton", f"{element}.location",
                                    "experience rows carry no location column"))
        # Experience gate: renders only with at least one reachable approved
        # fact attached to it that renders.
        if not entry.bullets:
            findings.append(Finding(
                "experience-gate", element,
                "an experience renders only with at least one approved, reachable"
                " fact rendered under it"))
        for i, bullet in enumerate(entry.bullets):
            findings.extend(self._check_bullet(f"{element}.bullet[{i}]", entry, bullet))
        return findings

    def _check_bullet(self, element: str, entry: CvExperienceEntry,
                      bullet: Bullet) -> list[Finding]:
        findings = []
        facts = self._view["facts"]
        # Rule 3: traceability. fact_ids nonempty (model schema), resolving to
        # approved active facts reachable by the walk, attached to this entry.
        source_statements = []
        if not bullet.fact_ids:
            findings.append(Finding("traceability", element, "bullet has no fact_ids"))
        for fact_id in bullet.fact_ids:
            fact = facts.get(fact_id)
            if fact is None:
                findings.append(Finding(
                    "traceability", element,
                    f"fact '{fact_id}' is not an approved active fact reachable by"
                    " this family's walk"))
                continue
            if fact["experience_id"] != entry.experience_id:
                findings.append(Finding(
                    "traceability", element,
                    f"fact '{fact_id}' does not attach to experience"
                    f" '{entry.experience_id}' (facts with no or other attachment"
                    " are never placed under an entry)"))
                continue
            source_statements.append(fact["statement"])
        if not source_statements:
            return findings
        source_text = "\n".join(source_statements)
        # Rule 1: numbers and dates against this bullet's facts only.
        findings.extend(self._check_numbers(element, bullet.text,
                                            numeric_tokens(source_text)))
        # Rule 2: entities against the context-wide allowlist.
        findings.extend(self._check_entities(element, bullet.text))
        # Rule 2a: content words against the bullet's source facts, its
        # experience row, and the profile.
        experience = self._view["experiences"][entry.experience_id]
        allowed = (content_lemmas(source_text)
                   | content_lemmas("\n".join(str(v) for v in experience.values() if v))
                   | content_lemmas("\n".join(str(v) for v in
                                              self._view["profile"].values() if v)))
        missing = content_lemmas(bullet.text) - allowed
        if missing:
            findings.append(Finding(
                "content-words", element,
                f"content words not in the bullet's sources: {sorted(missing)}"))
        # Rule 4: scope inflation, against the fact statements only.
        findings.extend(self._check_scope(element, bullet.text, source_text))
        return findings

    def _check_numbers(self, element: str, text: str,
                       allowed: set[tuple[str, str, str]]) -> list[Finding]:
        missing = numeric_tokens(text) - allowed
        if missing:
            rendered = ", ".join(f"{c}{v}{u}" for c, v, u in sorted(missing))
            return [Finding("numbers-dates", element,
                            f"numbers or dates absent from the element's sources: {rendered}")]
        return []

    def _check_entities(self, element: str, text: str) -> list[Finding]:
        """Unknown proper nouns and unknown capitalized/technical tokens fail
        closed against the allowlist built from the renderable view."""
        findings = []
        for token in tokenize_preserving_case(text):
            if not is_entity_like(token):
                continue
            folded = token.casefold()
            if folded in self._corpus_tokens or lemma(folded) in self._corpus_lemmas:
                continue
            findings.append(Finding(
                "entities", element,
                f"'{token}' is not in the allowlist built from approved state"))
        return findings

    def _check_scope(self, element: str, text: str, source_text: str) -> list[Finding]:
        findings = []
        bullet_lemmas = lemma_set(text)
        source_lemmas = lemma_set(source_text)
        for class_name, verbs in OWNERSHIP_VERB_CLASSES.items():
            if bullet_lemmas & verbs and not (source_lemmas & verbs):
                used = sorted(bullet_lemmas & verbs)
                findings.append(Finding(
                    "scope-inflation", element,
                    f"ownership verb class '{class_name}' ({used}) does not appear"
                    " in the underlying fact statement"))
        return findings


def _neq(rendered: str | None, canonical: str | None) -> bool:
    return normalize_chars(str(rendered or "")) != normalize_chars(str(canonical or ""))
