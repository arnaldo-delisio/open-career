"""Render path integration: real Chromium PDF, real pdftotext, ATS check end
to end (spec: decisions/package-generation-design.md, "Render path")."""

import pytest

from adapters.render.html_pdf import PlaywrightCvRenderer, SectionOrderError, render_html
from adapters.render.pdftext import PopplerPdfTextExtractor
from domain.ats_check import check_ats
from domain.grounding_spec import SPEC_VERSION
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


# -- the positioning layer, slice one (OC-41) ---------------------------------

def _variant(headline=None, links=(), end_date="2024-05", **overrides):
    cv = _cv()
    header = CvHeader(name=cv.header.name, email=cv.header.email,
                      phone=cv.header.phone, location=cv.header.location, links=links)
    experiences = tuple(CvExperienceEntry(**{**e.__dict__, "end_date": end_date})
                        for e in cv.experiences)
    base = dict(header=header, headline=headline, summary=cv.summary, skills=cv.skills,
                experiences=experiences, education=cv.education, meta=cv.meta)
    return CvModel(**{**base, **overrides})


@pytest.mark.parametrize("cv, expected, absent", [
    (_variant(headline="Forward Deployed AI Engineer"), ("Forward Deployed AI Engineer",), ()),
    (_variant(headline=None), (), ("Forward Deployed AI Engineer",)),
    (_variant(links=("https://github.com/example", "https://example.com")),
     ("https://github.com/example", "https://example.com"), ()),
    (_variant(links=()), (), ("github.com",)),
    (_variant(end_date=None), ("2022-03 - Present",), ()),
    (_variant(end_date="2024-05"), ("2022-03 - 2024-05",), ("Present",)),
    (_variant(summary="", skills=(), education=()), (), ("SUMMARY", "SKILLS", "EDUCATION")),
])
def test_rendered_pdf_matches_the_typed_projection(cv, expected, absent):
    """Golden regression over the real render and the real extractor: for every
    slice-one element, present and absent, the extracted stream equals the
    typed projection exactly (nothing rendered that is not modelled, nothing
    modelled that does not render), and the visible strings are the intended
    ones."""
    text = PopplerPdfTextExtractor().extract_layout(PlaywrightCvRenderer().render_pdf(cv))
    report = check_ats(text, cv)
    assert report.passed, report.to_json()
    # Case-insensitively: the headline and the section headings are
    # uppercased by the stylesheet, which the projection folds away.
    lowered = text.lower()
    for string in expected:
        assert string.lower() in lowered, string
    for string in absent:
        assert string.lower() not in lowered, string


def test_empty_sections_render_no_heading_and_still_carry_the_footer():
    cv = _variant(summary="", skills=(), experiences=(), education=())
    document = render_html(cv)
    assert "<h2>" not in document
    text = PopplerPdfTextExtractor().extract_layout(PlaywrightCvRenderer().render_pdf(cv))
    assert check_ats(text, cv).passed
    assert text.strip().endswith("Test Person")  # the deterministic footer


def test_each_role_is_one_block_the_paginator_is_asked_not_to_split():
    """Page placement is not deterministic, so the contract asserted here is
    the rendered one: every role's heading, dates and bullets sit inside a
    single break-inside: avoid block, with the heading kept with what follows."""
    document = render_html(_variant(headline="Forward Deployed AI Engineer"))
    role = document.split('<div class="role">')[1].split("</div>")[0]
    assert 'class="entry-head"' in role and 'class="entry-dates"' in role
    assert "<ul>" in role and role.count('<p class="entry-head"') == 1
    rule = document.split(".role {")[1].split("}")[0]
    assert "break-inside: avoid" in rule and "page-break-inside: avoid" in rule
    assert "break-after: avoid; page-break-after: avoid;" in document
    # The headline and the footer are not swept into a role block.
    assert 'class="headline"' in document.split('<div class="role">')[0]
    assert document.rindex('class="footer"') > document.rindex("</div>")


# -- projection versioning against real stored artifacts (OC-41 slice one) -----

def _pdf_from_html(document: str) -> bytes:
    """A PDF from arbitrary HTML, so a test can render a document shaped like
    an older projection rather than asserting about one."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(document)
            return page.pdf(format="A4", print_background=False)
        finally:
            browser.close()


def test_an_actually_v3_shaped_package_is_named_unsupported_not_failed():
    """A genuine version 3 package: a stored content model with no headline
    key at all, a version 3 snapshot and verifier report, and a real PDF
    rendered without the headline and the footer. Every version-gated rule
    must name the mismatch and cap the run at attention, because re-checking
    those bytes against version 4 expectations would condemn an artifact that
    was valid when it shipped. Legacy parsing is asserted here too: the stored
    JSON predates the field and must still load."""
    import hashlib
    import json as json_mod
    import re

    from domain.cv_model import parse_cv_model
    from domain.gauntlet_invariants import (
        ATTENTION,
        FAIL,
        PASS,
        invariants_passed,
        run_invariants,
    )
    from tests.unit.test_gauntlet_invariants import make_case, make_cv, make_snapshot

    # The stored model as version 3 wrote it: no headline key, not a null one.
    legacy_json = json_mod.dumps(
        {k: v for k, v in json_mod.loads(make_cv().to_json()).items()
         if k != "headline"}, indent=2, sort_keys=True)
    assert "headline" not in json_mod.loads(legacy_json)
    cv = parse_cv_model(legacy_json)
    assert cv.headline is None

    # The artifact as version 3 rendered it: no headline block, no footer.
    v3_document = re.sub(r'<p class="(?:headline|footer)">.*?</p>', "", render_html(cv))
    pdf = _pdf_from_html(v3_document)
    text = PopplerPdfTextExtractor().extract_layout(pdf)
    # The name appears once, in the header, with no footer repeat, which is
    # exactly why the version 4 expectation cannot be applied to these bytes.
    assert text.count(cv.header.name) == 1
    assert PopplerPdfTextExtractor().extract_layout(
        _pdf_from_html(render_html(make_cv()))).count(cv.header.name) == 2

    snapshot = make_snapshot()
    snapshot["normalization_spec_version"] = "3"
    case = make_case(cv=cv, snapshot=snapshot)
    trail = json_mod.loads(case["verifier_report_json"])
    trail["final"]["spec_version"] = "3"
    case.update(artifact_bytes=pdf, artifact_hash=hashlib.sha256(pdf).hexdigest(),
                extracted_text=text, verifier_report_json=json_mod.dumps(trail),
                ats_report_json=json_mod.dumps({"spec_version": "3", "page_count": 1,
                                                "passed": True, "findings": []}))
    results = {r.rule: r for r in run_invariants(**case)}
    assert results["audit-integrity"].disposition == PASS
    assert results["regrounding"].disposition == ATTENTION
    assert "regrounding-unsupported" in results["regrounding"].detail
    assert results["artifact-recheck"].disposition == ATTENTION
    assert "artifact-recheck-unsupported" in results["artifact-recheck"].detail
    # Attention is not a failure: the judges still run on a legacy package.
    assert invariants_passed(run_invariants(**case))

    # The gates are the stored versions and nothing else: the same real bytes
    # claiming the shipped spec are genuine failures, not compatibility.
    trail["final"]["spec_version"] = SPEC_VERSION
    case.update(verifier_report_json=json_mod.dumps(trail),
                ats_report_json=json_mod.dumps({"spec_version": SPEC_VERSION,
                                                "page_count": 1, "passed": True,
                                                "findings": []}))
    case["snapshot"] = dict(snapshot, normalization_spec_version=SPEC_VERSION)
    case["snapshot_bytes"] = json_mod.dumps(case["snapshot"], indent=2,
                                            sort_keys=True).encode()
    case["input_context_hash"] = hashlib.sha256(case["snapshot_bytes"]).hexdigest()
    results = {r.rule: r for r in run_invariants(**case)}
    assert results["regrounding"].disposition == PASS  # the model itself grounds
    assert results["artifact-recheck"].disposition == FAIL
