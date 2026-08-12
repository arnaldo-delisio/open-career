"""Render path: content model -> single-column ATS-safe HTML -> headless
Chromium PDF via Playwright (spec: decisions/package-generation-design.md,
"Render path"). The pipeline shape is career-ops's (MIT, attribution per
OC-17/OC-30: ideas only, code written fresh).

The template lives in templates/cv/ and carries zero personal data (OC-26);
every value arrives from the content model. Pre-render text normalization
(mojibake classes) happens in domain.cv_sections."""

import html
import string
from pathlib import Path

from domain.cv_model import CvModel, SECTION_ORDER
from domain.cv_sections import cv_sections
from domain.ports import CvRenderer

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEMPLATE = _REPO_ROOT / "templates" / "cv" / "single_column.html"


class SectionOrderError(RuntimeError):
    """Template/model section-order drift is a build failure (career-ops
    section-order guard)."""


def render_html(cv: CvModel, template_path: Path = DEFAULT_TEMPLATE) -> str:
    if cv.meta.section_order != SECTION_ORDER:
        raise SectionOrderError(
            f"section order {list(cv.meta.section_order)} != {list(SECTION_ORDER)}")
    sections = cv_sections(cv)
    rendered_keys = [s.key for s in sections]
    canonical = [k for k in SECTION_ORDER if k in rendered_keys]
    if rendered_keys != canonical:
        raise SectionOrderError(f"rendered sections {rendered_keys} out of canonical order")

    parts: list[str] = []
    for section in sections:
        if section.heading:
            parts.append(f"<h2>{html.escape(section.heading)}</h2>")
        for block in section.blocks:
            if block.kind == "head":
                css = "name" if section.key == "contact" else "entry-head"
                parts.extend(f'<p class="{css}">{html.escape(line)}</p>' for line in block.lines)
            elif block.kind == "bullets":
                items = "".join(f"<li>{html.escape(line)}</li>" for line in block.lines)
                parts.append(f"<ul>{items}</ul>")
            else:
                css = ' class="contact"' if section.key == "contact" else ""
                parts.extend(f"<p{css}>{html.escape(line)}</p>" for line in block.lines)
    template = string.Template(template_path.read_text())
    return template.substitute(body="\n".join(parts))


class PlaywrightCvRenderer(CvRenderer):
    def __init__(self, template_path: Path = DEFAULT_TEMPLATE):
        self._template_path = template_path

    def render_pdf(self, cv: CvModel) -> bytes:
        document = render_html(cv, self._template_path)
        from playwright.sync_api import sync_playwright  # deferred: heavy import

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.set_content(document)
                return page.pdf(format="A4", print_background=False)
            finally:
                browser.close()
