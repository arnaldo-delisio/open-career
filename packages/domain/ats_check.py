"""The mandatory, separate pdftotext ATS-parseability check (spec:
decisions/package-generation-design.md, "pdftotext check"). Never folded into
render, never skipped; failure blocks the package.

Verified in code against the versioned normalization spec:
- no (cid:NNN) or replacement characters;
- email and phone survive as literal text;
- section-by-section normalized sequence equality (transformation whitelist:
  line wrapping, whitespace collapse, bullet glyphs), rejecting unmatched
  extra, missing, or repeated tokens, so omitted, duplicated, injected, or
  reordered body text fails, not just missing headings;
- page count against the render budget (default 1 page, hard maximum 2;
  overflow is a build failure, never a silently shipped second page)."""

import json
import re
from dataclasses import dataclass

from domain.cv_model import CvModel
from domain.cv_sections import cv_sections
from domain.grounding_spec import SPEC_VERSION, tokenize

DEFAULT_PAGE_BUDGET = 1
HARD_MAX_PAGES = 2

_CID = re.compile(r"\(cid:\d+\)")


@dataclass(frozen=True)
class AtsFinding:
    check: str
    message: str


@dataclass(frozen=True)
class AtsReport:
    spec_version: str
    page_count: int
    findings: tuple[AtsFinding, ...]

    @property
    def passed(self) -> bool:
        return not self.findings

    def to_json(self) -> str:
        return json.dumps({
            "spec_version": self.spec_version,
            "page_count": self.page_count,
            "passed": self.passed,
            "findings": [{"check": f.check, "message": f.message} for f in self.findings],
        }, indent=2, sort_keys=True)


def expected_section_tokens(cv: CvModel) -> list[tuple[str, list[str]]]:
    """Per section (canonical order): heading tokens plus every block's tokens,
    in rendered order."""
    result = []
    for section in cv_sections(cv):
        tokens: list[str] = []
        if section.heading:
            tokens.extend(tokenize(section.heading))
        for block in section.blocks:
            for line in block.lines:
                tokens.extend(tokenize(line))
        result.append((section.key, tokens))
    return result


def check_ats(extracted_text: str, cv: CvModel,
              page_budget: int = DEFAULT_PAGE_BUDGET) -> AtsReport:
    findings: list[AtsFinding] = []
    page_budget = min(page_budget, HARD_MAX_PAGES)

    if _CID.search(extracted_text):
        findings.append(AtsFinding("encoding", "extracted text contains (cid:NNN) glyphs"))
    if "�" in extracted_text:
        findings.append(AtsFinding("encoding", "extracted text contains replacement characters"))
    for label, value in (("email", cv.header.email), ("phone", cv.header.phone)):
        if value and value not in extracted_text:
            findings.append(AtsFinding(
                "contact-literal", f"{label} '{value}' did not survive extraction as literal text"))

    page_count = max(1, extracted_text.count("\f"))
    if page_count > page_budget:
        findings.append(AtsFinding(
            "page-budget",
            f"{page_count} pages exceeds the render budget of {page_budget}"
            f" (hard maximum {HARD_MAX_PAGES}); overflow is a build failure"))

    # Section-by-section normalized sequence equality. The whole extracted
    # token stream must equal the concatenated expected sections exactly:
    # equality of the whole in canonical order is equality per section, and
    # rejects unmatched extra, missing, or repeated tokens anywhere.
    expected = expected_section_tokens(cv)
    extracted = tokenize(extracted_text)
    offset = 0
    for key, tokens in expected:
        slice_ = extracted[offset:offset + len(tokens)]
        if slice_ != tokens:
            diverges = next((i for i, (a, b) in enumerate(zip(slice_, tokens)) if a != b),
                            min(len(slice_), len(tokens)))
            findings.append(AtsFinding(
                "section-sequence",
                f"section '{key}' diverges at token {diverges}: expected"
                f" {tokens[diverges:diverges + 5]}, extracted {slice_[diverges:diverges + 5]}"))
            break
        offset += len(tokens)
    else:
        if extracted[offset:]:
            findings.append(AtsFinding(
                "section-sequence",
                f"unexpected extra tokens after the last section: {extracted[offset:offset + 5]}"))

    return AtsReport(spec_version=SPEC_VERSION, page_count=page_count,
                     findings=tuple(findings))
