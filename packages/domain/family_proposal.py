"""Role-family proposals: the model proposes structure only (OC-5), code
validates against a closed schema, nothing persists unconfirmed (spec:
decisions/package-generation-design.md, "Role-family onboarding step")."""

import json
from dataclasses import dataclass

from domain.ports import ModelAdapter

_KEYS = {"name", "rationale", "target_seniority", "adjacent_titles",
         "search_vocabulary", "target_capability_names"}


class FamilyProposalError(ValueError):
    pass


@dataclass(frozen=True)
class DraftFamily:
    name: str
    rationale: str
    target_seniority: str | None
    adjacent_titles: tuple[str, ...]
    search_vocabulary: tuple[str, ...]
    target_capability_names: tuple[str, ...]


def _string_list(obj: dict, key: str, where: str) -> tuple[str, ...]:
    value = obj.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise FamilyProposalError(f"{where}: '{key}' must be a list of strings")
    return tuple(value)


def parse_family_proposals(text: str, known_capability_names: set[str]) -> tuple[DraftFamily, ...]:
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0]
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as e:
        raise FamilyProposalError(f"output is not valid JSON: {e}") from e
    if not isinstance(obj, list) or not obj:
        raise FamilyProposalError("top level must be a non-empty JSON array")
    drafts = []
    for i, f in enumerate(obj):
        where = f"proposal[{i}]"
        if not isinstance(f, dict) or not set(f) <= _KEYS:
            raise FamilyProposalError(f"{where}: allowed keys are {sorted(_KEYS)}")
        if not isinstance(f.get("name"), str) or not f["name"].strip():
            raise FamilyProposalError(f"{where}: 'name' must be a non-empty string")
        if not isinstance(f.get("rationale"), str) or not f["rationale"].strip():
            raise FamilyProposalError(f"{where}: 'rationale' must be a non-empty string")
        seniority = f.get("target_seniority")
        if seniority is not None and not isinstance(seniority, str):
            raise FamilyProposalError(f"{where}: 'target_seniority' must be a string or null")
        targets = _string_list(f, "target_capability_names", where)
        unknown = [t for t in targets if t not in known_capability_names]
        if unknown:
            raise FamilyProposalError(
                f"{where}: target_capability_names must name existing capabilities;"
                f" unknown: {unknown}")
        drafts.append(DraftFamily(
            name=f["name"].strip(), rationale=f["rationale"].strip(),
            target_seniority=seniority,
            adjacent_titles=_string_list(f, "adjacent_titles", where),
            search_vocabulary=_string_list(f, "search_vocabulary", where),
            target_capability_names=targets))
    return tuple(drafts)


class FamilyProposalService:
    """Build the state prompt, call the model, validate in code; one retry
    carrying the validation error back (the extraction-service pattern)."""

    def __init__(self, model: ModelAdapter, prompt_template: str):
        self._model = model
        self._prompt_template = prompt_template

    def propose(self, state_json: str, known_capability_names: set[str]) -> tuple[DraftFamily, ...]:
        prompt = self._prompt_template.replace("{state_json}", state_json)
        try:
            return parse_family_proposals(self._model.complete(prompt), known_capability_names)
        except FamilyProposalError as first_error:
            retry = (f"{prompt}\n\nYour previous output failed validation: {first_error}"
                     "\nReturn only corrected JSON matching the schema exactly.")
            return parse_family_proposals(self._model.complete(retry), known_capability_names)
