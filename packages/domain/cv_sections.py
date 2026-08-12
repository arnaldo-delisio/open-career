"""Canonical rendered text of a CV, section by section, in the fixed section
order. One builder feeds both the HTML renderer and the pdftotext section-
equivalence check, so the expected sequence and the rendered sequence cannot
drift apart silently; the check still runs against the actual PDF bytes.

Every text line passes the versioned pre-render normalization (em-dashes,
smart quotes, zero-width characters, NBSP: the mojibake incident class)."""

from dataclasses import dataclass

from domain.cv_model import CvExperienceEntry, CvModel, SECTION_ORDER
from domain.grounding_spec import normalize_chars


@dataclass(frozen=True)
class Block:
    """kind: 'line' (plain paragraph), 'head' (bold entry head), 'bullets'."""

    kind: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class Section:
    key: str
    heading: str | None  # None: the contact block renders without a heading
    blocks: tuple[Block, ...]


_HEADINGS = {
    "summary": "Summary",
    "skills": "Skills",
    "experience": "Experience",
    "projects": "Projects",
    "education": "Education",
}


def _entry_blocks(entry: CvExperienceEntry) -> tuple[Block, ...]:
    head = entry.title if not entry.org else f"{entry.title}, {entry.org}"
    dates = f"{entry.start_date or ''} - {entry.end_date or 'Present'}".strip(" -")
    blocks = [Block("head", (head,))]
    if dates:
        blocks.append(Block("line", (dates,)))
    if entry.bullets:
        blocks.append(Block("bullets", tuple(b.text for b in entry.bullets)))
    return tuple(blocks)


def cv_sections(cv: CvModel) -> tuple[Section, ...]:
    """Nonempty sections in SECTION_ORDER; an empty section (no summary, no
    entries) is skipped entirely, heading included."""
    sections: list[Section] = []
    contact_parts = [p for p in (cv.header.email, cv.header.phone, cv.header.location)
                     if p] + list(cv.header.links)
    contact_blocks = [Block("head", (cv.header.name,))]
    if contact_parts:
        contact_blocks.append(Block("line", (" | ".join(contact_parts),)))
    sections.append(Section("contact", None, tuple(contact_blocks)))

    for key in SECTION_ORDER[1:]:
        if key == "summary":
            blocks = (Block("line", (cv.summary,)),) if cv.summary.strip() else ()
        elif key == "skills":
            names = [s.name for s in cv.skills]
            blocks = (Block("line", (" | ".join(names),)),) if names else ()
        else:
            entries = {"experience": cv.experiences, "projects": cv.projects,
                       "education": cv.education}[key]
            blocks = tuple(b for e in entries for b in _entry_blocks(e))
        if blocks:
            sections.append(Section(key, _HEADINGS[key], blocks))
    return _normalized(tuple(sections))


def _normalized(sections: tuple[Section, ...]) -> tuple[Section, ...]:
    return tuple(
        Section(s.key, s.heading and normalize_chars(s.heading),
                tuple(Block(b.kind, tuple(normalize_chars(l) for l in b.lines))
                      for b in s.blocks))
        for s in sections)
