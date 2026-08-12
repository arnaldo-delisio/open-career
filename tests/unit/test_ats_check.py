"""ATS check unit tests over synthetic extracted text (the real-PDF path is
covered by the render integration test)."""

from domain.ats_check import check_ats, expected_section_tokens
from domain.cv_model import Bullet, CvExperienceEntry, CvHeader, CvMeta, CvModel, SkillItem


def _cv():
    return CvModel(
        header=CvHeader(name="Test Person", email="t@example.com", phone="+39 333 1234",
                        location="Milan, Italy", links=("https://github.com/example",)),
        summary="Engineer automating deployment pipelines.",
        skills=(SkillItem(name="Python", capability_ids=("cap_1",)),),
        experiences=(CvExperienceEntry(
            experience_id="exp_1", title="Engineer", org="Acme",
            start_date="2022-03", end_date="2024-05",
            bullets=(Bullet(text="Reduced onboarding time by 40%", fact_ids=("fact_1",)),)),),
        meta=CvMeta(role_family_id="rf_1", strategy_version=1,
                    generated_at="2026-08-11T00:00:00Z"))


def _faithful_text(cv):
    lines = []
    for _key, tokens in expected_section_tokens(cv):
        lines.append(" ".join(tokens))
    # Contact literals must appear verbatim, not just tokenized.
    return f"{cv.header.name}\n{cv.header.email} | {cv.header.phone}\n" + "\n".join(lines[1:]) \
        + "\nMilan, Italy https://github.com/example"


def _text(cv):
    """Sequence-faithful synthetic extraction: sections in order with wrapped
    lines and a bullet glyph (the whitelisted transformations)."""
    parts = [f"{cv.header.name}\n{cv.header.email} | {cv.header.phone} | Milan,\nItaly"
             f" | https://github.com/example",
             "SUMMARY\nEngineer automating deployment\npipelines.",
             "SKILLS\nPython",
             "EXPERIENCE\nEngineer, Acme\n2022-03 - 2024-05\n• Reduced onboarding time by 40%"]
    return "\n".join(parts)


def test_faithful_extraction_passes():
    report = check_ats(_text(_cv()), _cv())
    assert report.passed, report.to_json()
    assert report.page_count == 1


def test_cid_glyphs_fail():
    assert not check_ats(_text(_cv()) + " (cid:114)", _cv()).passed


def test_missing_email_fails():
    text = _text(_cv()).replace("t@example.com", "t@example .com")
    report = check_ats(text, _cv())
    assert any(f.check == "contact-literal" for f in report.findings)


def test_omitted_body_text_fails():
    text = _text(_cv()).replace("Reduced onboarding time by 40%", "Reduced onboarding")
    report = check_ats(text, _cv())
    assert any(f.check == "section-sequence" for f in report.findings)


def test_injected_text_fails():
    text = _text(_cv()).replace("SKILLS\nPython", "SKILLS\nPython Kubernetes")
    report = check_ats(text, _cv())
    assert any(f.check == "section-sequence" for f in report.findings)


def test_duplicated_section_fails():
    text = _text(_cv()) + "\nEXPERIENCE\nEngineer, Acme"
    report = check_ats(text, _cv())
    assert any(f.check == "section-sequence" for f in report.findings)


def test_reordered_sections_fail():
    cv = _cv()
    parts = _text(cv).split("SUMMARY")
    text = parts[0] + "SKILLS\nPython\nSUMMARY" + parts[1].replace("SKILLS\nPython", "")
    report = check_ats(text, cv)
    assert any(f.check == "section-sequence" for f in report.findings)


def test_two_page_overflow_is_a_build_failure():
    """The prior repos' silently-shipped-two-page incident: overflow fails,
    it never ships."""
    report = check_ats(_text(_cv()) + "\f\f", _cv(), page_budget=1)
    assert any(f.check == "page-budget" for f in report.findings)
    over_hard_max = _text(_cv()) + "\f\f\f"
    report = check_ats(over_hard_max, _cv(), page_budget=5)  # budget capped at 2
    assert any(f.check == "page-budget" for f in report.findings)
