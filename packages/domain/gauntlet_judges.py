"""The Gauntlet judge set (spec: the scope's decisions/gauntlet-design.md,
"The judge set for this phase" and "Judge mechanics").

Three judges gate: Truth (semantic entailment, per bullet against its source
facts; runs on a second model family per ratification decision 2),
Consistency (cross-section and profile contradictions), Writing (generic
filler under a defined blocking boundary). Each is one ModelAdapter call with
a closed JSON schema validated in code (OC-32), bounded schema-validation
retry, and the deterministic finding backstop: a finding may accuse only an
element that exists, quoting a verbatim span of that element's own rendered
text; anything else is dropped as an invalid finding and counted as a judge
malfunction, never shown as a result.

Outcome mapping is deterministic: a judge FAILs exactly when it emits at least
one valid blocking finding; a declared TERMINAL_ABSTAIN stands; schema-retry
exhaustion, adapter failure, or all-invalid findings record
OPERATIONAL_ABSTAIN (a malfunction, not an adjudication)."""

import json
from dataclasses import dataclass

from domain.cv_model import CvModel
from domain.grounding_spec import normalize
from domain.ports import ModelAdapter

# Judge order is fixed: sequential, always all three, no fail-fast.
JUDGES = ("truth", "consistency", "writing")

PROMPT_FILES = {"truth": "gauntlet_truth.md",
                "consistency": "gauntlet_consistency.md",
                "writing": "gauntlet_writing.md"}

# Bumping any prompt bumps its version here and the suite version with it.
PROMPT_VERSIONS = {"truth": "2", "consistency": "1", "writing": "1"}

MAX_JUDGE_ATTEMPTS = 3  # initial call plus bounded retries with the failure named

# Per-judge outcomes (the closed five-token enum; NOT_RUN is stamped by the
# runner when stage zero fails).
PASS = "PASS"
FAIL = "FAIL"
TERMINAL_ABSTAIN = "TERMINAL_ABSTAIN"
OPERATIONAL_ABSTAIN = "OPERATIONAL_ABSTAIN"
NOT_RUN = "NOT_RUN"

SEVERITIES = ("blocking", "advisory")

_DECLARED_VERDICTS = (PASS, FAIL, TERMINAL_ABSTAIN)
_BASE_FINDING_KEYS = {"element_id", "severity", "quote", "message"}


class JudgeOutputError(ValueError):
    """One judge completion failed the closed output schema."""


@dataclass(frozen=True)
class JudgeFinding:
    element_id: str
    severity: str
    quote: str
    message: str
    fact_ids: tuple[str, ...] = ()          # Truth only
    second_element_id: str | None = None    # Consistency only
    second_quote: str | None = None         # Consistency only

    def to_dict(self) -> dict:
        d = {"element_id": self.element_id, "severity": self.severity,
             "quote": self.quote, "message": self.message}
        if self.fact_ids:
            d["fact_ids"] = list(self.fact_ids)
        if self.second_element_id is not None:
            d["second_element_id"] = self.second_element_id
            d["second_quote"] = self.second_quote
        return d


@dataclass(frozen=True)
class JudgeResult:
    judge: str
    outcome: str
    findings: tuple[JudgeFinding, ...]
    invalid_findings: tuple[str, ...]  # raw text preserved for debugging
    attempts: int
    # Ordered per-attempt transcript: every prompt actually sent (retry
    # feedback included) with its completion, or the adapter error where the
    # call itself failed. This is the canonical evidence the run persists.
    transcript: tuple[dict, ...] = ()
    # Observed model identity per call, in call order (never pre-stamped).
    models: tuple[str, ...] = ()
    note: str = ""

    @property
    def model(self) -> str:
        """One-line summary of the observed model set for this judge:
        the single observed identity, 'mixed' when calls disagree,
        'unreported' when nothing was observed."""
        distinct = set(self.models)
        if not distinct:
            return "unreported"
        if len(distinct) == 1:
            return next(iter(distinct))
        return "mixed"

    def report_dict(self) -> dict:
        return {
            "judge": self.judge,
            "prompt_version": PROMPT_VERSIONS[self.judge],
            "model": self.model,
            "models": list(self.models),
            "verdict": self.outcome,
            "findings": [f.to_dict() for f in self.findings],
            "invalid_findings": list(self.invalid_findings),
            "attempts": self.attempts,
            "note": self.note,
        }


def not_run(judge: str) -> JudgeResult:
    return JudgeResult(judge=judge, outcome=NOT_RUN, findings=(),
                       invalid_findings=(), attempts=0,
                       note="stage zero failed; judge never ran")


# -- addressable elements -----------------------------------------------------

def element_texts(cv: CvModel, profile: dict) -> dict[str, str]:
    """The closed map of accusable element ids to their rendered text. Profile
    fields count as addressable elements (the Consistency Judge may cite
    them); every quote must be a verbatim span of its own element's text."""
    elements: dict[str, str] = {"summary": cv.summary}
    for section, entries in (("experiences", cv.experiences),
                             ("projects", cv.projects), ("education", cv.education)):
        for entry in entries:
            key = f"{section}[{entry.experience_id}]"
            elements[key] = " | ".join(
                str(v) for v in (entry.title, entry.org, entry.start_date,
                                 entry.end_date) if v)
            for i, bullet in enumerate(entry.bullets):
                elements[f"{key}.bullet[{i}]"] = bullet.text
    for skill in cv.skills:
        elements[f"skills[{skill.name}]"] = skill.name
    for name, value in profile.items():
        if value:
            elements[f"profile.{name}"] = str(value)
    return elements


def _bullet_fact_ids(cv: CvModel) -> dict[str, frozenset[str]]:
    result: dict[str, frozenset[str]] = {}
    for section, entries in (("experiences", cv.experiences),
                             ("projects", cv.projects), ("education", cv.education)):
        for entry in entries:
            for i, bullet in enumerate(entry.bullets):
                result[f"{section}[{entry.experience_id}].bullet[{i}]"] = \
                    frozenset(bullet.fact_ids)
    return result


# -- judge inputs -------------------------------------------------------------

def build_judge_payload(judge: str, cv: CvModel, snapshot: dict) -> dict:
    """The judge's input, assembled in code from the persisted snapshot and
    content model of one specific version, never the live database."""
    view = snapshot["renderable_grounding_view"]
    if judge == "truth":
        facts = view["facts"]
        entries = []
        for section, section_entries in (("experiences", cv.experiences),
                                         ("projects", cv.projects),
                                         ("education", cv.education)):
            for entry in section_entries:
                bullets = []
                for i, bullet in enumerate(entry.bullets):
                    bullets.append({
                        "element_id": f"{section}[{entry.experience_id}].bullet[{i}]",
                        "text": bullet.text,
                        "source_facts": [
                            {"fact_id": fid,
                             "statement": facts.get(fid, {}).get("statement"),
                             "attached_experience_id":
                                 facts.get(fid, {}).get("experience_id")}
                            for fid in bullet.fact_ids],
                    })
                entries.append({
                    "section": section,
                    "experience_id": entry.experience_id,
                    "title": entry.title, "org": entry.org,
                    "start_date": entry.start_date, "end_date": entry.end_date,
                    "bullets": bullets,
                })
        return {"entries": entries,
                "summary": {"element_id": "summary", "text": cv.summary,
                            "renderable_grounding_view": view}}
    if judge == "consistency":
        return {"content_model": json.loads(cv.to_json()),
                "profile": view["profile"]}
    if judge == "writing":
        elements = element_texts(cv, {})  # rendered text only, no profile
        return {"elements": [{"element_id": k, "text": v}
                             for k, v in elements.items() if v]}
    raise ValueError(f"unknown judge '{judge}'")


def build_judge_prompt(judge: str, template: str, cv: CvModel, snapshot: dict) -> str:
    return template.replace(
        "{payload_json}",
        json.dumps(build_judge_payload(judge, cv, snapshot), indent=2, sort_keys=True))


# -- output schema ------------------------------------------------------------

def _parse_output(judge: str, raw: str) -> tuple[str, list[dict]]:
    body = raw.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0]
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as e:
        raise JudgeOutputError(f"output is not valid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise JudgeOutputError("top level must be an object")
    unknown = set(obj) - {"verdict", "findings", "note"}
    if unknown:
        raise JudgeOutputError(f"unknown keys {sorted(unknown)}")
    verdict = obj.get("verdict")
    if verdict not in _DECLARED_VERDICTS:
        raise JudgeOutputError(f"verdict must be one of {list(_DECLARED_VERDICTS)}")
    findings = obj.get("findings", [])
    if not isinstance(findings, list) or any(not isinstance(f, dict) for f in findings):
        raise JudgeOutputError("findings must be a list of objects")
    allowed = _BASE_FINDING_KEYS | ({"fact_ids"} if judge == "truth" else set()) \
        | ({"second_element_id", "second_quote"} if judge == "consistency" else set())
    if "note" in obj and not isinstance(obj["note"], str):
        raise JudgeOutputError("note must be a string")
    for i, finding in enumerate(findings):
        unknown = set(finding) - allowed
        if unknown:
            raise JudgeOutputError(f"findings[{i}]: unknown keys {sorted(unknown)}")
        # Every field a judge can return is type-checked HERE, before any
        # grammar validation dereferences it: a malformed model response is a
        # schema error (bounded retry), never an escaping TypeError.
        for key in ("element_id", "severity", "quote", "message"):
            if not isinstance(finding.get(key), str) or not finding[key].strip():
                raise JudgeOutputError(f"findings[{i}]: '{key}' must be a non-empty string")
        if finding["severity"] not in SEVERITIES:
            raise JudgeOutputError(f"findings[{i}]: severity must be one of {list(SEVERITIES)}")
        if "fact_ids" in finding:
            fact_ids = finding["fact_ids"]
            if not isinstance(fact_ids, list) or any(
                    not isinstance(f, str) or not f.strip() for f in fact_ids):
                raise JudgeOutputError(
                    f"findings[{i}]: fact_ids must be a list of non-empty strings")
        for key in ("second_element_id", "second_quote"):
            if key in finding and (not isinstance(finding[key], str)
                                   or not finding[key].strip()):
                raise JudgeOutputError(
                    f"findings[{i}]: '{key}' must be a non-empty string")
    if verdict == FAIL and not findings:
        raise JudgeOutputError("a FAIL verdict requires at least one finding")
    return verdict, findings


# -- the finding validator (the deterministic backstop) -----------------------

def _is_span(quote: str, element_text: str) -> bool:
    return bool(normalize(quote)) and normalize(quote) in normalize(element_text)


def _raw(finding) -> str:
    """The raw preserved form of a rejected finding. Never raises: a shape the
    JSON encoder cannot serialize is still recorded, so an invalid finding can
    never become an escaping exception."""
    try:
        return json.dumps(finding, sort_keys=True)
    except (TypeError, ValueError):
        return repr(finding)


def _validate_one(judge: str, finding: dict, elements: dict[str, str],
                  bullet_facts: dict[str, frozenset[str]]) -> JudgeFinding | None:
    """One finding against the evidence grammar; None means invalid. Every
    field is read defensively (the parser type-checks first, but this
    validator never assumes it ran)."""
    element_id = finding.get("element_id")
    quote = finding.get("quote")
    if not isinstance(element_id, str) or not isinstance(quote, str):
        return None
    text = elements.get(element_id)
    if text is None or not _is_span(quote, text):
        return None
    fact_ids: tuple[str, ...] = ()
    second_element_id = second_quote = None
    if judge == "truth":
        claimed = finding.get("fact_ids")
        if element_id == "summary":
            if claimed:
                return None
        else:
            expected = bullet_facts.get(element_id)
            if (expected is None or not isinstance(claimed, list)
                    or any(not isinstance(f, str) for f in claimed)
                    or frozenset(claimed) != expected):
                return None
            fact_ids = tuple(claimed)
    elif judge == "consistency":
        second_element_id = finding.get("second_element_id")
        second_quote = finding.get("second_quote")
        if not isinstance(second_element_id, str):
            return None
        second_text = elements.get(second_element_id)
        if (second_text is None or second_element_id == element_id
                or not isinstance(second_quote, str)
                or not _is_span(second_quote, second_text)):
            return None
    severity = finding.get("severity")
    message = finding.get("message")
    if severity not in SEVERITIES or not isinstance(message, str):
        return None
    return JudgeFinding(
        element_id=element_id, severity=severity, quote=quote, message=message,
        fact_ids=fact_ids, second_element_id=second_element_id,
        second_quote=second_quote)


def validate_findings(judge: str, findings: list[dict], cv: CvModel,
                      profile: dict) -> tuple[tuple[JudgeFinding, ...], tuple[str, ...]]:
    """The per-finding evidence grammar: one accused element id that exists,
    a verbatim span of that element's own rendered text (normalized under the
    grounding spec), Truth fact-id pairing exactly matching the bullet's
    traceability list (the summary's source is the renderable view), and the
    Consistency two-element rule. Failures are dropped and preserved raw, and
    a finding whose validation raises at all is a dropped finding too: a
    malformed model response never escapes as an exception, which would leave
    no run record and a dangling reservation."""
    elements = element_texts(cv, profile)
    bullet_facts = _bullet_fact_ids(cv)
    valid: list[JudgeFinding] = []
    invalid: list[str] = []
    for finding in findings:
        try:
            entry = _validate_one(judge, finding, elements, bullet_facts)
        except Exception:
            entry = None
        if entry is None:
            invalid.append(_raw(finding))
        else:
            valid.append(entry)
    return tuple(valid), tuple(invalid)


# -- one judge run ------------------------------------------------------------

def run_judge(judge: str, adapter: ModelAdapter, prompt: str, cv: CvModel,
              profile: dict) -> JudgeResult:
    """One judge, one prompt, bounded schema-validation retry, deterministic
    outcome mapping. Adapter failures of any kind are OPERATIONAL_ABSTAIN,
    never a silent pass and never a synthesized verdict."""
    failure = ""
    attempts = 0
    transcript: list[dict] = []
    models: list[str] = []

    def result(outcome, findings=(), invalid=(), note=""):
        return JudgeResult(judge=judge, outcome=outcome, findings=tuple(findings),
                           invalid_findings=tuple(invalid), attempts=attempts,
                           transcript=tuple(transcript), models=tuple(models),
                           note=note)

    while attempts < MAX_JUDGE_ATTEMPTS:
        attempts += 1
        sent = prompt + failure
        try:
            raw, metadata = adapter.complete_with_meta(sent)
        except Exception as e:
            transcript.append({"prompt": sent,
                               "error": f"{type(e).__name__}: {e}"})
            return result(OPERATIONAL_ABSTAIN,
                          note=f"adapter failure: {type(e).__name__}: {e}")
        transcript.append({"prompt": sent, "completion": raw})
        models.append(str(metadata.get("model") or "unreported"))
        try:
            declared, findings = _parse_output(judge, raw)
        except JudgeOutputError as e:
            failure = ("\n\nYour previous output failed schema validation:"
                       f" {e}\nReturn only corrected JSON matching the schema exactly.")
            continue
        try:
            valid, invalid = validate_findings(judge, findings, cv, profile)
        except Exception as e:
            # Defense in depth: the validator already converts a per-finding
            # failure into a dropped finding, so reaching here means the
            # validator itself malfunctioned. That is an operational
            # abstention with a run record, never an escaping exception.
            return result(OPERATIONAL_ABSTAIN,
                          note="finding validation raised unexpectedly:"
                               f" {type(e).__name__}: {e}")
        if findings and not valid:
            return result(OPERATIONAL_ABSTAIN, invalid=invalid,
                          note="every finding failed the evidence grammar"
                               " (judge malfunction, not an adjudication)")
        if any(f.severity == "blocking" for f in valid):
            outcome = FAIL
        elif declared == TERMINAL_ABSTAIN:
            outcome = TERMINAL_ABSTAIN
        else:
            outcome = PASS
        return result(outcome, findings=valid, invalid=invalid)
    return result(OPERATIONAL_ABSTAIN, note="schema-validation retries exhausted")
