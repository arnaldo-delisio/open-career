"""The typed visual projection (spec: decisions/cv-positioning-layer.md, slice
one): headline, contact links, the one date transformation, role grouping, and
the footer. This builder is what the ATS whole-stream check compares against,
so a string that renders without appearing here is a bug both suites catch."""

from domain.cv_model import Bullet, CvExperienceEntry, CvHeader, CvMeta, CvModel, SkillItem
from domain.cv_sections import ONGOING_DISPLAY, cv_sections, display_dates


def _cv(**overrides):
    base = dict(
        header=CvHeader(name="Test Person", email="t@example.com", phone="+39 333 1234",
                        location="Milan, Italy",
                        links=("https://github.com/example", "https://example.com")),
        headline="Forward Deployed Engineer",
        summary="Engineer automating deployment pipelines.",
        skills=(SkillItem(name="Python", capability_ids=("cap_1",)),),
        experiences=(CvExperienceEntry(
            experience_id="exp_1", title="Engineer", org="Acme",
            start_date="2022-03", end_date="2024-05",
            bullets=(Bullet(text="Reduced onboarding time by 40%", fact_ids=("f",)),)),),
        meta=CvMeta(role_family_id="rf_1", strategy_version=1,
                    generated_at="2026-08-11T00:00:00Z"))
    return CvModel(**{**base, **overrides})


def _lines(cv, key):
    return [line for section in cv_sections(cv) if section.key == key
            for block in section.blocks for line in block.lines]


def _kinds(cv, key):
    return [block.kind for section in cv_sections(cv) if section.key == key
            for block in section.blocks]


def test_headline_renders_under_the_name():
    assert _lines(_cv(), "contact")[:2] == ["Test Person", "Forward Deployed Engineer"]
    assert _kinds(_cv(), "contact")[1] == "headline"


def test_absent_headline_omits_the_block_entirely():
    for absent in (None, "", "   "):
        assert _kinds(_cv(headline=absent), "contact") == ["head", "line"]


def test_links_render_in_the_contact_line_and_omit_cleanly():
    contact = _lines(_cv(), "contact")[2]
    assert contact.endswith("https://github.com/example | https://example.com")
    header = CvHeader(name="Test Person", email="t@example.com", links=())
    assert _lines(_cv(header=header), "contact")[-1] == "t@example.com"


def test_open_ended_role_renders_present_and_a_closed_one_its_end_date():
    open_entry = CvExperienceEntry(
        experience_id="exp_1", title="Engineer", org="Acme", start_date="2022-03",
        end_date=None, bullets=(Bullet(text="Shipped it", fact_ids=("f",)),))
    assert "2022-03 - Present" in _lines(_cv(experiences=(open_entry,)), "experience")
    assert "2022-03 - 2024-05" in _lines(_cv(), "experience")


def test_the_date_transformation_is_closed_to_a_dated_open_end():
    """The null that means ongoing is end_date on a dated row, and nothing
    else: an entry stating no start date states no span, so it never claims to
    be current."""
    assert display_dates("2022-03", None) == f"2022-03 - {ONGOING_DISPLAY}"
    assert display_dates(None, None) == ""
    assert display_dates("", "  ") == ""
    assert display_dates(None, "2024-05") == "2024-05"
    assert display_dates("2022-03", "2024-05") == "2022-03 - 2024-05"


def test_a_blank_end_label_is_not_the_null_that_means_ongoing():
    """"Present" is reserved for the null. An end_date that is present but
    says nothing (empty or whitespace) is not that statement, so it renders
    the start alone rather than claiming the role is current."""
    assert display_dates("2022-03", "") == "2022-03"
    assert display_dates("2022-03", "   ") == "2022-03"
    entry = CvExperienceEntry(
        experience_id="exp_1", title="Engineer", org="Acme", start_date="2022-03",
        end_date="", bullets=(Bullet(text="Shipped it", fact_ids=("f",)),))
    assert ONGOING_DISPLAY not in " ".join(_lines(_cv(experiences=(entry,)), "experience"))


def test_other_absent_values_render_nothing():
    entry = CvExperienceEntry(
        experience_id="exp_1", title="Engineer", org=None, start_date=None,
        end_date=None, bullets=(Bullet(text="Shipped it", fact_ids=("f",)),))
    lines = _lines(_cv(experiences=(entry,)), "experience")
    assert lines == ["Engineer", "Shipped it"]
    assert ONGOING_DISPLAY not in " ".join(lines)


def test_empty_sections_are_skipped_heading_included():
    keys = [s.key for s in cv_sections(_cv(summary="  ", skills=(), experiences=()))]
    assert keys == ["contact", "footer"]


def test_footer_is_the_last_section_and_carries_the_name():
    sections = cv_sections(_cv())
    assert sections[-1].key == "footer"
    assert sections[-1].blocks[0].lines == ("Test Person",)


def test_each_role_block_is_grouped_by_its_canonical_id():
    section = next(s for s in cv_sections(_cv()) if s.key == "experience")
    assert {b.group for b in section.blocks} == {"exp_1"}
    contact = next(s for s in cv_sections(_cv()) if s.key == "contact")
    assert {b.group for b in contact.blocks} == {None}
