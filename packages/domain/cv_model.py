"""The CV content model (spec: decisions/package-generation-design.md, "CV
content model", extended by decisions/cv-positioning-layer.md slice one):
typed structure with traceability as fields, never freestyle document text.
Serialized JSON per package version.

Section order is fixed by the template and asserted at render; drift is a
build failure.

Two canonical meanings this model carries, both relied on by the renderer, the
grounding verifier and the ATS projection:

- `headline` is the target-role line. It is typed in code from the role
  family the package targets and is never model-authored; absent means the
  package targets no named family.
- a null `end_date` on an entry MEANS the role is ongoing (the same convention
  domain/dates.py already holds). It is not an absent value and not an unknown
  one: it is the positive statement "this has not ended", which is why the
  renderer may turn it into "Present". No other null in this model carries a
  meaning, and none of them renders as text."""

import json
from dataclasses import dataclass, field

SECTION_ORDER = ("contact", "summary", "skills", "experience", "projects", "education")


class CvModelError(ValueError):
    pass


@dataclass(frozen=True)
class CvHeader:
    """From user_profile only."""

    name: str
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    links: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillItem:
    """Every skill item carries its capability_id list; the display name comes
    from the canonical capability row."""

    name: str
    capability_ids: tuple[str, ...]


@dataclass(frozen=True)
class Bullet:
    """Traceability is a field, not a comment: the fact_id list this bullet
    renders, never empty."""

    text: str
    fact_ids: tuple[str, ...]


@dataclass(frozen=True)
class CvExperienceEntry:
    """Carries its canonical experience_id; rendered skeleton fields are
    limited to the confirmed row's own columns. A null end_date states that
    the role is ongoing (module docstring), never that the value is unknown."""

    experience_id: str
    title: str
    org: str | None
    start_date: str | None
    end_date: str | None
    bullets: tuple[Bullet, ...]
    location: str | None = None


@dataclass(frozen=True)
class CvMeta:
    role_family_id: str
    strategy_version: int
    generated_at: str
    section_order: tuple[str, ...] = SECTION_ORDER


@dataclass(frozen=True)
class CvModel:
    header: CvHeader
    summary: str
    skills: tuple[SkillItem, ...]
    experiences: tuple[CvExperienceEntry, ...]
    meta: CvMeta
    projects: tuple[CvExperienceEntry, ...] = ()
    education: tuple[CvExperienceEntry, ...] = ()
    headline: str | None = None

    def to_json(self) -> str:
        return json.dumps({
            "headline": self.headline,
            "header": {
                "name": self.header.name, "email": self.header.email,
                "phone": self.header.phone, "location": self.header.location,
                "links": list(self.header.links),
            },
            "summary": self.summary,
            "skills": [{"name": s.name, "capability_ids": list(s.capability_ids)}
                       for s in self.skills],
            "experiences": [_entry_dict(e) for e in self.experiences],
            "projects": [_entry_dict(e) for e in self.projects],
            "education": [_entry_dict(e) for e in self.education],
            "meta": {
                "role_family_id": self.meta.role_family_id,
                "strategy_version": self.meta.strategy_version,
                "generated_at": self.meta.generated_at,
                "section_order": list(self.meta.section_order),
            },
        }, indent=2, sort_keys=True)

    def all_entries(self) -> tuple[CvExperienceEntry, ...]:
        return self.experiences + self.projects + self.education


def _entry_dict(entry: CvExperienceEntry) -> dict:
    return {
        "experience_id": entry.experience_id, "title": entry.title, "org": entry.org,
        "start_date": entry.start_date, "end_date": entry.end_date,
        "location": entry.location,
        "bullets": [{"text": b.text, "fact_ids": list(b.fact_ids)} for b in entry.bullets],
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CvModelError(message)


def _parse_entry(obj: dict, where: str) -> CvExperienceEntry:
    _require(isinstance(obj, dict), f"{where}: must be an object")
    _require(isinstance(obj.get("experience_id"), str) and obj["experience_id"],
             f"{where}: experience_id is required")
    _require(isinstance(obj.get("title"), str) and obj["title"], f"{where}: title is required")
    # Optional skeleton fields are string-or-null, checked here rather than
    # discovered at render: the renderer reads these as labels, and a number
    # or an object arriving in a date field must fail schema validation with
    # the field named, never raise mid-render.
    for field in ("org", "start_date", "end_date", "location"):
        value = obj.get(field)
        _require(value is None or isinstance(value, str),
                 f"{where}.{field}: must be a string or null")
    bullets = []
    for i, b in enumerate(obj.get("bullets", [])):
        _require(isinstance(b, dict) and isinstance(b.get("text"), str) and b["text"].strip(),
                 f"{where}.bullets[{i}]: text is required")
        fact_ids = b.get("fact_ids")
        _require(isinstance(fact_ids, list) and fact_ids
                 and all(isinstance(f, str) for f in fact_ids),
                 f"{where}.bullets[{i}]: fact_ids must be a non-empty list of ids")
        bullets.append(Bullet(text=b["text"], fact_ids=tuple(fact_ids)))
    return CvExperienceEntry(
        experience_id=obj["experience_id"], title=obj["title"], org=obj.get("org"),
        start_date=obj.get("start_date"), end_date=obj.get("end_date"),
        location=obj.get("location"), bullets=tuple(bullets),
    )


def parse_cv_model(text: str) -> CvModel:
    """Parse and validate a serialized content model (model output or stored
    content_model_json) against the closed schema. Any deviation raises."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0]
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as e:
        raise CvModelError(f"content model is not valid JSON: {e}") from e
    _require(isinstance(obj, dict), "top level must be an object")
    header = obj.get("header")
    _require(isinstance(header, dict) and isinstance(header.get("name"), str)
             and header["name"].strip(), "header.name is required")
    links = header.get("links", [])
    _require(isinstance(links, list) and all(isinstance(l, str) for l in links),
             "header.links must be a list of strings")
    _require(isinstance(obj.get("summary"), str), "summary must be a string")
    headline = obj.get("headline")
    _require(headline is None or isinstance(headline, str),
             "headline must be a string or null")
    if headline is not None and not headline.strip():
        headline = None
    skills = []
    for i, s in enumerate(obj.get("skills", [])):
        _require(isinstance(s, dict) and isinstance(s.get("name"), str) and s["name"].strip(),
                 f"skills[{i}]: name is required")
        capability_ids = s.get("capability_ids")
        _require(isinstance(capability_ids, list) and capability_ids
                 and all(isinstance(c, str) for c in capability_ids),
                 f"skills[{i}]: capability_ids must be a non-empty list of ids")
        skills.append(SkillItem(name=s["name"], capability_ids=tuple(capability_ids)))
    meta = obj.get("meta")
    _require(isinstance(meta, dict), "meta is required")
    _require(isinstance(meta.get("role_family_id"), str), "meta.role_family_id is required")
    _require(isinstance(meta.get("strategy_version"), int), "meta.strategy_version is required")
    _require(isinstance(meta.get("generated_at"), str), "meta.generated_at is required")
    section_order = tuple(meta.get("section_order", SECTION_ORDER))
    _require(section_order == SECTION_ORDER,
             f"meta.section_order must be exactly {list(SECTION_ORDER)}")
    return CvModel(
        header=CvHeader(name=header["name"], email=header.get("email"),
                        phone=header.get("phone"), location=header.get("location"),
                        links=tuple(links)),
        summary=obj["summary"],
        headline=headline,
        skills=tuple(skills),
        experiences=tuple(_parse_entry(e, f"experiences[{i}]")
                          for i, e in enumerate(obj.get("experiences", []))),
        projects=tuple(_parse_entry(e, f"projects[{i}]")
                       for i, e in enumerate(obj.get("projects", []))),
        education=tuple(_parse_entry(e, f"education[{i}]")
                        for i, e in enumerate(obj.get("education", []))),
        meta=CvMeta(role_family_id=meta["role_family_id"],
                    strategy_version=meta["strategy_version"],
                    generated_at=meta["generated_at"], section_order=section_order),
    )
