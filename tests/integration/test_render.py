"""Render path integration: real Chromium PDF, real pdftotext, ATS check end
to end (spec: decisions/package-generation-design.md, "Render path")."""

import pytest

from adapters.render.html_pdf import PlaywrightCvRenderer, SectionOrderError, render_html
from adapters.render.pdftext import PopplerPdfTextExtractor
from domain.ats_check import check_ats
from domain.cv_model import (
    Bullet,
    CvExperienceEntry,
    CvHeader,
    CvMeta,
    CvModel,
    SECTION_ORDER,
    SkillItem,
)


def _cv(bullet_count=2):
    bullets = tuple(
        Bullet(text=f"Reduced onboarding time by 40% across {i + 2} rollouts",
               fact_ids=("fact_1",))
        for i in range(bullet_count))
    return CvModel(
        header=CvHeader(name="Test Person", email="t@example.com", phone="+39 333 1234",
                        location="Milan, Italy", links=("https://github.com/example",)),
        summary="Forward deployed engineer automating enterprise deployment pipelines"
                " with customer-facing ownership.",
        skills=(SkillItem(name="Python", capability_ids=("cap_1",)),
                SkillItem(name="Kubernetes", capability_ids=("cap_2",))),
        experiences=(CvExperienceEntry(
            experience_id="exp_1", title="Forward Deployed Engineer", org="Acme",
            start_date="2022-03", end_date="2024-05", bullets=bullets),),
        education=(CvExperienceEntry(
            experience_id="exp_edu", title="BSc Computer Science", org="Uni",
            start_date="2015", end_date="2018",
            bullets=(Bullet(text="Thesis on distributed systems", fact_ids=("fact_e",)),)),),
        meta=CvMeta(role_family_id="rf_1", strategy_version=1,
                    generated_at="2026-08-11T00:00:00Z"))


def test_render_extract_ats_roundtrip():
    pdf = PlaywrightCvRenderer().render_pdf(_cv())
    assert pdf.startswith(b"%PDF")
    text = PopplerPdfTextExtractor().extract_layout(pdf)
    report = check_ats(text, _cv())
    assert report.passed, report.to_json()
    assert report.page_count == 1
    # Ligature-prone and mojibake-prone content survives: no fi/fl mojibake,
    # contact literals intact.
    assert "t@example.com" in text and "(cid:" not in text


def test_overflow_fails_never_ships_second_page():
    cv = _cv(bullet_count=60)
    pdf = PlaywrightCvRenderer().render_pdf(cv)
    text = PopplerPdfTextExtractor().extract_layout(pdf)
    report = check_ats(text, cv)
    assert report.page_count > 1
    assert any(f.check == "page-budget" for f in report.findings)


def test_section_order_drift_is_a_build_failure():
    cv = _cv()
    drifted = CvModel(header=cv.header, summary=cv.summary, skills=cv.skills,
                      experiences=cv.experiences, education=cv.education,
                      meta=CvMeta(role_family_id="rf_1", strategy_version=1,
                                  generated_at="now",
                                  section_order=tuple(reversed(SECTION_ORDER))))
    with pytest.raises(SectionOrderError):
        render_html(drifted)


def test_pre_render_normalization_removes_mojibake_classes():
    cv = _cv()
    poisoned = CvModel(
        header=cv.header,
        summary="Forward deployed engineer — automating “enterprise”"
                " pipelines​ with customer-facing ownership.",
        skills=cv.skills, experiences=cv.experiences, education=cv.education, meta=cv.meta)
    document = render_html(poisoned)
    assert "—" not in document and "​" not in document
    assert "“" not in document and " " not in document
