"""INDEPENDENT regression pass, render side: ligature/font-extraction fixture
through the real Chromium render and real pdftotext (no mojibake), and the
two-page overflow hard build failure on a real render. Spec:
decisions/package-generation-design.md, "Render path" and "pdftotext check"."""

from adapters.render.html_pdf import PlaywrightCvRenderer
from adapters.render.pdftext import PopplerPdfTextExtractor
from domain.ats_check import check_ats
from domain.cv_model import (
    Bullet,
    CvExperienceEntry,
    CvHeader,
    CvMeta,
    CvModel,
    SkillItem,
)

# fi/fl/ffi-heavy content, plus precomposed ligature characters (the
# mojibake-in-sent-letters class) that pre-render normalization must flatten.
LIGATURE_SUMMARY = ("Efficient offline workflow profiling for financial traffic"
                    " classification across office infrastructure.")
LIGATURE_BULLETS = (
    "Refined the configuration of affiliate fulfillment workflows for staffing"
    " offices, filing certified efficiency findings",
    "Certiﬁed proﬁling workﬂows for ofﬁcial afﬁliate conﬁguration files",
)


def _cv(bullet_texts=LIGATURE_BULLETS, bullet_count=None):
    if bullet_count is not None:
        bullet_texts = tuple(
            f"Refined affiliate workflow configuration number {i + 1} for"
            " official traffic profiling" for i in range(bullet_count))
    bullets = tuple(Bullet(text=t, fact_ids=("ifact_1",)) for t in bullet_texts)
    return CvModel(
        header=CvHeader(name="Casey Sample", email="casey@sample.dev",
                        phone="+1 555 0100", location="Lisbon, Portugal",
                        links=("https://github.com/casey-sample",)),
        summary=LIGATURE_SUMMARY,
        skills=(SkillItem(name="Workflow Profiling", capability_ids=("icap_1",)),),
        experiences=(CvExperienceEntry(
            experience_id="iexp_1", title="Certification Officer",
            org="Affiliated Offices", start_date="2020-01", end_date="2023-06",
            bullets=bullets),),
        meta=CvMeta(role_family_id="irf_1", strategy_version=1,
                    generated_at="2026-08-12T00:00:00Z"))


def test_ligature_heavy_text_survives_render_and_extraction():
    """fi/fl/ffi-heavy words through the real render + pdftotext: no
    (cid:NNN), no replacement characters, no ligature codepoints, and the
    extracted body matches the expected section token stream exactly."""
    cv = _cv()
    pdf = PlaywrightCvRenderer().render_pdf(cv)
    assert pdf.startswith(b"%PDF")
    text = PopplerPdfTextExtractor().extract_layout(pdf)
    assert "(cid:" not in text
    assert "�" not in text
    for codepoint in ("ﬁ", "ﬂ", "ﬃ", "ﬄ"):  # fi fl ffi ffl
        assert codepoint not in text
    lowered = text.lower()
    for word in ("efficient", "workflow", "profiling", "affiliate", "official",
                 "configuration", "certified", "files"):
        assert word in lowered, f"'{word}' lost in extraction"
    report = check_ats(text, cv)
    assert report.passed, report.to_json()
    assert report.page_count == 1


def test_two_page_overflow_real_render_is_a_build_failure():
    """Overflow on the real render path is rejected, never a silently shipped
    second page (the prior repos' two-page incident)."""
    cv = _cv(bullet_count=70)
    pdf = PlaywrightCvRenderer().render_pdf(cv)
    text = PopplerPdfTextExtractor().extract_layout(pdf)
    report = check_ats(text, cv)
    assert report.page_count >= 2
    assert not report.passed
    assert any(f.check == "page-budget" for f in report.findings)
