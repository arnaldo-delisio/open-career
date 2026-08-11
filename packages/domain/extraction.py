"""CV extraction: model proposes structure, code validates (OC-5, OC-32).

The model may only propose draft experiences and draft facts from CV text,
never values for canonical profile fields. Output is parsed then validated
against the closed schema below, in code, with one retry on validation failure
(the deterministic backstop the work loop requires for generated content).
"""

import json
from dataclasses import dataclass
from typing import get_args

from domain.entities import ExperienceKind, FactType
from domain.ports import ModelAdapter

_EXPERIENCE_KINDS = set(get_args(ExperienceKind))
_FACT_TYPES = set(get_args(FactType))

_EXPERIENCE_KEYS = {"kind", "title", "org", "start_date", "end_date", "summary"}
_FACT_KEYS = {"experience_index", "fact_type", "statement", "source_location"}


class ExtractionError(ValueError):
    pass


@dataclass(frozen=True)
class DraftExperience:
    kind: str
    title: str
    org: str | None
    start_date: str | None
    end_date: str | None
    summary: str | None


@dataclass(frozen=True)
class DraftFact:
    fact_type: str
    statement: str
    experience_index: int | None
    source_location: str | None


@dataclass(frozen=True)
class CvExtraction:
    experiences: tuple[DraftExperience, ...]
    facts: tuple[DraftFact, ...]


def _require_optional_str(obj: dict, key: str, where: str) -> str | None:
    value = obj.get(key)
    if value is not None and not isinstance(value, str):
        raise ExtractionError(f"{where}: '{key}' must be a string or null")
    return value


def parse_extraction(text: str) -> CvExtraction:
    """Parse and validate model output against the closed extraction schema.
    Any deviation raises ExtractionError."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0]
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as e:
        raise ExtractionError(f"output is not valid JSON: {e}") from e
    if not isinstance(obj, dict) or set(obj) != {"experiences", "facts"}:
        raise ExtractionError("top level must be an object with exactly 'experiences' and 'facts'")
    if not isinstance(obj["experiences"], list) or not isinstance(obj["facts"], list):
        raise ExtractionError("'experiences' and 'facts' must be arrays")

    experiences = []
    for i, e in enumerate(obj["experiences"]):
        where = f"experiences[{i}]"
        if not isinstance(e, dict) or not set(e) <= _EXPERIENCE_KEYS:
            raise ExtractionError(f"{where}: allowed keys are {sorted(_EXPERIENCE_KEYS)}")
        if e.get("kind") not in _EXPERIENCE_KINDS:
            raise ExtractionError(f"{where}: 'kind' must be one of {sorted(_EXPERIENCE_KINDS)}")
        if not isinstance(e.get("title"), str) or not e["title"].strip():
            raise ExtractionError(f"{where}: 'title' must be a non-empty string")
        experiences.append(DraftExperience(
            kind=e["kind"], title=e["title"],
            org=_require_optional_str(e, "org", where),
            start_date=_require_optional_str(e, "start_date", where),
            end_date=_require_optional_str(e, "end_date", where),
            summary=_require_optional_str(e, "summary", where),
        ))

    facts = []
    for i, f in enumerate(obj["facts"]):
        where = f"facts[{i}]"
        if not isinstance(f, dict) or not set(f) <= _FACT_KEYS:
            raise ExtractionError(f"{where}: allowed keys are {sorted(_FACT_KEYS)}")
        if f.get("fact_type") not in _FACT_TYPES:
            raise ExtractionError(f"{where}: 'fact_type' must be one of {sorted(_FACT_TYPES)}")
        if not isinstance(f.get("statement"), str) or not f["statement"].strip():
            raise ExtractionError(f"{where}: 'statement' must be a non-empty string")
        index = f.get("experience_index")
        if index is not None:
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(experiences):
                raise ExtractionError(f"{where}: 'experience_index' must be null or a valid experiences index")
        facts.append(DraftFact(
            fact_type=f["fact_type"], statement=f["statement"], experience_index=index,
            source_location=_require_optional_str(f, "source_location", where),
        ))

    return CvExtraction(experiences=tuple(experiences), facts=tuple(facts))


class CvExtractionService:
    """Build the prompt, call the model, validate in code; one retry on
    validation failure, carrying the validation error back to the model."""

    def __init__(self, model: ModelAdapter, prompt_template: str):
        self._model = model
        self._prompt_template = prompt_template

    def extract(self, cv_text: str) -> CvExtraction:
        prompt = self._prompt_template.replace("{cv_text}", cv_text)
        raw = self._model.complete(prompt)
        try:
            return parse_extraction(raw)
        except ExtractionError as first_error:
            retry_prompt = (
                f"{prompt}\n\nYour previous output failed schema validation: "
                f"{first_error}\nReturn only corrected JSON matching the schema exactly."
            )
            try:
                return parse_extraction(self._model.complete(retry_prompt))
            except ExtractionError as second_error:
                raise ExtractionError(
                    f"extraction failed validation twice: {first_error}; retry: {second_error}"
                ) from second_error
