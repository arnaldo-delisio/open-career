"""The generation context's capability rows: computed evidence depth, never
the stored self-rating (OC-40)."""

from dataclasses import replace

from domain.entities import Evidence
from domain.traversal import STORY_NOTE_PREFIX
from tests.unit.test_grounding import make_context


def test_capability_rows_carry_evidence_depth_and_no_strength():
    view = make_context().renderable_grounding_view()
    assert view["capabilities"] == {
        "cap_1": {"name": "Python", "supporting_facts": 2, "supporting_stories": 0}}


def test_story_evidence_counts_as_a_supporting_story():
    context = make_context()
    selection = context.selection.selections[0]
    story = Evidence(id="ev_story", evidence_type="user_statement",
                     title="story: FDE", notes=f"{STORY_NOTE_PREFIX}exp_1")
    chain = replace(selection.chains[0], evidence=story)
    context = replace(context, selection=replace(
        context.selection, selections=(replace(selection, chains=(chain,)),)))
    assert context.renderable_grounding_view()["capabilities"]["cap_1"] == {
        "name": "Python", "supporting_facts": 2, "supporting_stories": 1}
