"""Canonical rendered text of a CV, section by section, in the fixed section
order. One builder feeds both the HTML renderer and the pdftotext section-
equivalence check, so the expected sequence and the rendered sequence cannot
drift apart silently; the check still runs against the actual PDF bytes.

Every visual string the document carries is projected here, headline, contact
links, the ongoing-role label and the footer included (OC-41 slice one): a
string that reaches the page without passing through this builder breaks
whole-stream equality in domain/ats_check.py, which is the point. Nothing is
ever whitelisted away there to make room for a string this builder does not
know about.

Every text line passes the versioned pre-render normalization (em-dashes,
smart quotes, zero-width characters, NBSP: the mojibake incident class)."""

from dataclasses import dataclass

from domain.cv_model import CvExperienceEntry, CvModel, SECTION_ORDER
from domain.grounding_spec import normalize_chars

# The single display transformation of a canonical null (content model: a null
# end_date MEANS ongoing). It exists in exactly one function, below.
ONGOING_DISPLAY = "Present"

# The rendered document is the content sections plus a trailing footer block.
# The footer is not a content section (meta.section_order stays the closed
# content-model set), but it is a real visual string, so it is projected and
# ordered like one instead of being excused from the ATS check.
DOCUMENT_SECTION_ORDER = SECTION_ORDER + ("footer",)


@dataclass(frozen=True)
class Block:
    """kind: 'line' (plain paragraph), 'head' (bold entry head), 'bullets',
    'headline' (the target-role line), 'footer'.

    group: the canonical id of the entry this block belongs to, or None. The
    renderer wraps consecutive blocks sharing a group in one semantic role
    block so a role's heading, dates and bullets stay together."""

    kind: str
    lines: tuple[str, ...]
    group: str | None = None


@dataclass(frozen=True)
class Section:
    key: str
    heading: str | None  # None: the contact and footer blocks render unheaded
    blocks: tuple[Block, ...]


_HEADINGS = {
    "summary": "Summary",
    "skills": "Skills",
    "experience": "Experience",
    "projects": "Projects",
    "education": "Education",
}


def display_dates(start_date: str | None, end_date: str | None) -> str:
    """The one deterministic date transformation, used by the renderer and by
    the ATS projection through the same call.

    A dated role with a null end_date renders "<start> - Present", because a
    null end_date is the canonical statement that the role is ongoing (content
    model), not an absent value. This is the only place an absent canonical
    value becomes text and it is closed twice over: to one field, and to the
    null itself. The ongoing branch tests `end_date is None`, never
    truthiness, so an empty or whitespace end label (a stored value that says
    nothing, rather than a null that says "not ended") renders no span and
    never claims the role is current. A row with no start date states no span
    at all and renders whatever single label it has.
    """
    start = (start_date or "").strip()
    end = (end_date or "").strip()
    if start and end_date is None:
        return f"{start} - {ONGOING_DISPLAY}"
    if start and end:
        return f"{start} - {end}"
    return start or end


def _entry_blocks(entry: CvExperienceEntry) -> tuple[Block, ...]:
    head = entry.title if not entry.org else f"{entry.title}, {entry.org}"
    dates = display_dates(entry.start_date, entry.end_date)
    group = entry.experience_id
    blocks = [Block("head", (head,), group)]
    if dates:
        blocks.append(Block("line", (dates,), group))
    if entry.bullets:
        blocks.append(Block("bullets", tuple(b.text for b in entry.bullets), group))
    return tuple(blocks)


def cv_sections(cv: CvModel) -> tuple[Section, ...]:
    """Nonempty sections in DOCUMENT_SECTION_ORDER; an empty section (no
    summary, no entries) is skipped entirely, heading included. The contact
    section carries the name, the optional headline, and the contact line
    (email, phone, location and the profile links, each omitted when absent)."""
    sections: list[Section] = []
    contact_parts = [p for p in (cv.header.email, cv.header.phone, cv.header.location)
                     if p] + [link for link in cv.header.links if link]
    contact_blocks = [Block("head", (cv.header.name,))]
    if cv.headline and cv.headline.strip():
        contact_blocks.append(Block("headline", (cv.headline,)))
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

    # Footer: the name, already on the page and already grounded, so the
    # footer adds no string the projection does not otherwise carry.
    if cv.header.name.strip():
        sections.append(Section("footer", None, (Block("footer", (cv.header.name,)),)))
    return _normalized(tuple(sections))


def _normalized(sections: tuple[Section, ...]) -> tuple[Section, ...]:
    return tuple(
        Section(s.key, s.heading and normalize_chars(s.heading),
                tuple(Block(b.kind, tuple(normalize_chars(l) for l in b.lines), b.group)
                      for b in s.blocks))
        for s in sections)
