"""Package generation: drafting loop with reject-and-retry, verbatim
fallback, and the deterministic content-model builder (spec:
decisions/package-generation-design.md).

The model selects, orders, and phrases (OC-5); the verifier rejects anything
whose numbers, dates, entities, or content words are absent from approved
state. A failed check retries generation with the failure named (bounded);
then the element falls back to the fact statement verbatim or is dropped
with the gap reported. The verifier never rewrites silently."""

import json
from dataclasses import dataclass, replace

from domain.context import GenerationContext
from domain.cv_model import (
    Bullet,
    CvExperienceEntry,
    CvHeader,
    CvMeta,
    CvModel,
    CvModelError,
    SkillItem,
    parse_cv_model,
)
from domain.grounding import GroundingReport, GroundingVerifier
from domain.ports import ModelAdapter

MAX_DRAFT_ATTEMPTS = 3  # initial draft plus bounded retries with the failure named

# Kind -> content-model section for experience containers.
_SECTION_FOR_KIND = {"role": "experiences", "venture": "experiences",
                     "other": "experiences", "project": "projects",
                     "education": "education"}


@dataclass(frozen=True)
class DraftResult:
    cv: CvModel
    report: GroundingReport
    attempts: int
    fallback_used: bool
    dropped: tuple[str, ...]

    def trail_json(self) -> str:
        """Persisted as verifier_report_json: the final report plus the trail."""
        return json.dumps({
            "final": json.loads(self.report.to_json()),
            "attempts": self.attempts,
            "fallback_used": self.fallback_used,
            "dropped": list(self.dropped),
        }, indent=2, sort_keys=True)


def build_header(context: GenerationContext) -> CvHeader:
    """Header comes from user_profile in code, never from the model."""
    profile = context.profile
    links = tuple(str(profile[f]) for f in
                  ("linkedin_url", "github_url", "portfolio_url", "website_url")
                  if profile.get(f))
    return CvHeader(name=str(profile.get("full_name") or ""),
                    email=profile.get("email"), phone=profile.get("phone"),
                    location=profile.get("location"), links=links)


def build_verbatim_model(context: GenerationContext, generated_at: str) -> tuple[CvModel, tuple[str, ...]]:
    """The deterministic fallback: fact statements verbatim, skills from
    covered capabilities, no drafted summary (dropped, gap reported).
    Grounded by construction; still verified like any draft."""
    view = context.renderable_grounding_view()
    facts_by_experience: dict[str, list[str]] = {}
    for fact_id, fact in sorted(view["facts"].items()):
        if fact["experience_id"]:
            facts_by_experience.setdefault(fact["experience_id"], []).append(fact_id)
    sections: dict[str, list[CvExperienceEntry]] = {
        "experiences": [], "projects": [], "education": []}
    for experience in context.experiences:
        fact_ids = facts_by_experience.get(experience.id)
        section = _SECTION_FOR_KIND[experience.kind]
        if not fact_ids and section == "experiences":
            # The container gate applies to employment entries only; a
            # confirmed education or project row renders skeleton-only.
            continue
        bullets = tuple(Bullet(text=view["facts"][fid]["statement"], fact_ids=(fid,))
                        for fid in (fact_ids or ()))
        sections[section].append(CvExperienceEntry(
            experience_id=experience.id, title=experience.title, org=experience.org,
            start_date=experience.start_date, end_date=experience.end_date,
            bullets=bullets))
    # Reverse-chronological employment order, as the verifier enforces it.
    sections["experiences"].sort(
        key=lambda e: (e.end_date or "9999-99", e.start_date or ""), reverse=True)
    skills = tuple(SkillItem(name=c.name, capability_ids=(c.id,))
                   for c in context.covered_capabilities())
    cv = CvModel(
        header=build_header(context), summary="", skills=skills,
        experiences=tuple(sections["experiences"]),
        projects=tuple(sections["projects"]), education=tuple(sections["education"]),
        meta=CvMeta(role_family_id=context.role_family_id,
                    strategy_version=context.strategy.strategy_version,
                    generated_at=generated_at))
    return cv, ("summary",)


class CvDraftingService:
    """Prompt -> model -> parse -> verify, with bounded named-failure retries
    and the verbatim fallback."""

    def __init__(self, model: ModelAdapter, prompt_template: str):
        self._model = model
        self._prompt_template = prompt_template

    def draft(self, context: GenerationContext, generated_at: str) -> DraftResult:
        verifier = GroundingVerifier(context)
        prompt = (self._prompt_template
                  .replace("{context_json}", context.snapshot_json())
                  .replace("{generated_at}", generated_at))
        failure = ""
        attempts = 0
        for _ in range(MAX_DRAFT_ATTEMPTS):
            attempts += 1
            raw = self._model.complete(prompt + failure)
            try:
                # The header is built in code from user_profile, and the meta
                # identity fields are stamped from the generation context,
                # whatever the model returned: contact fields and the strategy
                # audit trail can be neither omitted nor altered by model
                # output.
                cv = parse_cv_model(raw)
                cv = replace(cv, header=build_header(context),
                             meta=replace(cv.meta,
                                          role_family_id=context.role_family_id,
                                          strategy_version=context.strategy.strategy_version))
            except CvModelError as e:
                failure = ("\n\nYour previous output failed schema validation: "
                           f"{e}\nReturn only corrected JSON matching the schema exactly.")
                continue
            report = verifier.verify(cv)
            if report.passed:
                return DraftResult(cv=cv, report=report, attempts=attempts,
                                   fallback_used=False, dropped=())
            named = "; ".join(
                f"[{f.rule}] {f.element}: {f.message}" for f in report.findings[:20])
            failure = ("\n\nYour previous draft was rejected by the deterministic"
                       f" grounding verifier. Failures: {named}\nFix every failure:"
                       " use only numbers, dates, entities, and content words present"
                       " in the grounding view. Return only corrected JSON.")
        cv, dropped = build_verbatim_model(context, generated_at)
        report = verifier.verify(cv)
        return DraftResult(cv=cv, report=report, attempts=attempts,
                           fallback_used=True, dropped=dropped)
