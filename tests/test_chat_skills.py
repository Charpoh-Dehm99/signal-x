"""Chart-analyst skill registry and routing tests."""

from __future__ import annotations

from signalx.chat.skills import (
    CHART_SKILLS,
    CORE_SKILLS,
    MAX_SKILLS,
    public_skills,
    select_skills,
    skill_prompt,
)


def test_catalog_is_large_and_unique():
    assert len(CHART_SKILLS) >= 30
    ids = [skill.id for skill in CHART_SKILLS]
    assert len(ids) == len(set(ids))
    assert len(public_skills()) == len(CHART_SKILLS)
    assert all(skill.name and skill.summary and skill.instructions for skill in CHART_SKILLS)


def test_core_skills_always_travel():
    selected = select_skills("hello")
    assert set(CORE_SKILLS).issubset(set(selected))
    assert "chart_patterns" in {skill.id for skill in selected}
    assert len(selected) == MAX_SKILLS


def test_question_routing_selects_specialists():
    flow = select_skills("is the flow confirming the move?")
    ids = {skill.id for skill in flow}
    assert "order_flow" in ids
    assert "large_prints" in ids
    assert "scenario_tree" in ids

    squeeze = select_skills("funding and OI squeeze risk?")
    ids = {skill.id for skill in squeeze}
    assert "derivatives_context" in ids
    assert "squeeze_expansion" in ids
    assert "funding_positioning" in ids


def test_default_analysis_adds_broad_lenses():
    ids = {skill.id for skill in select_skills("explain this chart")}
    assert "pump_anatomy" in ids
    assert "post_pump_short" in ids
    assert "breakout_retest" in ids
    assert "multi_tf_conflict" in ids


def test_prompt_contains_selected_instructions():
    selected = select_skills("where is invalidation?")
    prompt = skill_prompt(selected)
    assert "Invalidation map" in prompt
    assert "Always include invalidation" in prompt
    assert "Observation discipline" in prompt


def test_public_catalog_has_no_internal_prompt_text():
    rows = public_skills()
    assert rows
    assert "instructions" not in rows[0]
    assert "triggers" not in rows[0]
    assert rows[0]["id"] == "data_integrity"
